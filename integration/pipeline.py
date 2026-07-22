"""三引擎集成主流水线

daily_batch: 每天拉取 stock-screener Top25 → AI分析 → 填充推荐列表
hourly_monitor: 每小时检查推荐列表 → 择机入场 → 换仓
"""

import json
import logging
import sys
import time
from datetime import datetime
from typing import List, Optional

from .config import (
    SCAN_OUTPUT_URL, TOP_N_CANDIDATES, ANALYSIS_BATCH_SIZE, ANALYSIS_BATCH_DELAY,
    MAX_RECOMMENDATIONS, MIN_AI_CONFIDENCE, ENTRY_PRICE_TOLERANCE_PCT,
    MAX_DAILY_TRADES, RECOMMENDATION_TTL_DAYS,
)
from .models import (
    ScanResult, AIAnalysisResult, Recommendation, HourlyCheck,
    RecommendationStatus,
)
from .scan_parser import fetch_and_parse
from .recommendation_store import RecommendationStore
from .portfolio_overseer import PortfolioOverseer

logger = logging.getLogger(__name__)


class IntegrationPipeline:
    """三引擎集成主编排器"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.store = RecommendationStore()
        self.overseer: Optional[PortfolioOverseer] = None  # 延迟初始化，需 alpaca_utils

    def _init_overseer(self):
        """初始化仓位监管器"""
        if self.overseer:
            return
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            alpaca = AlpacaUtils()
            self.overseer = PortfolioOverseer(self.store, alpaca)
        except Exception as e:
            logger.warning(f"无法初始化 AlpacaUtils: {e}，仓位监控不可用")
            self.overseer = PortfolioOverseer(self.store, None)

    def _get_graph(self):
        """获取 TradingAgentsGraph 实例"""
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG.copy()
        cfg["llm_provider"] = self.config.get("llm_provider", "deepseek")
        cfg["deep_think_llm"] = self.config.get("deep_llm", "deepseek-chat")
        cfg["quick_think_llm"] = self.config.get("quick_llm", "deepseek-chat")
        cfg["max_debate_rounds"] = self.config.get("debate_rounds", 3)
        # 批量分析时关掉并行代理，避免 API 限速
        cfg["parallel_analysts"] = self.config.get("parallel_analysts", False)
        cfg.update(self.config.get("extra_config", {}))

        return TradingAgentsGraph(config=cfg)

    # ==================== 每日批量分析 ====================

    def daily_batch(self, scan_url: Optional[str] = None, top_n: Optional[int] = None) -> dict:
        """每日主流程：拉取扫描结果 → AI 分析 → 填充推荐列表

        返回:
            {"analyzed": N, "added": N, "skipped": N, "errors": N}
        """
        url = scan_url or SCAN_OUTPUT_URL
        n = top_n or TOP_N_CANDIDATES

        logger.info("=" * 60)
        logger.info(f"每日批量分析开始 ({datetime.now().isoformat()})")
        logger.info("=" * 60)

        # 第1步：拉取全市场筛选结果
        scan_results = fetch_and_parse(url, n)
        if not scan_results:
            logger.error("未获取到扫描结果，终止")
            return {"analyzed": 0, "added": 0, "skipped": 0, "errors": 0}

        logger.info(f"第1步完成: 获取到 {len(scan_results)} 只候选股")

        # 第2步：逐批 AI 深度分析
        graph = self._get_graph()
        today = datetime.now().strftime("%Y-%m-%d")

        stats = {"analyzed": 0, "added": 0, "skipped": 0, "errors": 0}

        for batch_start in range(0, len(scan_results), ANALYSIS_BATCH_SIZE):
            batch = scan_results[batch_start:batch_start + ANALYSIS_BATCH_SIZE]

            for sr in batch:
                stats["analyzed"] += 1
                try:
                    result = self._analyze_one(sr, graph, today)
                    if result:
                        rec = self._to_recommendation(sr, result, today)
                        rec_id = self.store.add_recommendation(rec)
                        if rec_id:
                            stats["added"] += 1
                        else:
                            stats["skipped"] += 1  # 已存在
                    else:
                        stats["skipped"] += 1
                except Exception as e:
                    logger.error(f"分析 {sr.ticker} 时出错: {e}", exc_info=True)
                    stats["errors"] += 1

            # 批次间延迟，避免 API 限速
            if batch_start + ANALYSIS_BATCH_SIZE < len(scan_results):
                time.sleep(ANALYSIS_BATCH_DELAY)

        # 第3步：清理
        self.store.expire_old_pending()
        self.store.sweep_duplicates()

        # 第4步：输出摘要
        summary = self.store.get_summary()
        logger.info(f"每日分析完成: 分析了 {stats['analyzed']} 只, "
                     f"新增 {stats['added']} 只, 跳过 {stats['skipped']} 只, 错误 {stats['errors']} 只")
        logger.info(f"推荐列表: pending={summary['pending']}, active={summary['active']}")

        return stats

    def _analyze_one(self, sr: ScanResult, graph, date_str: str) -> Optional[AIAnalysisResult]:
        """对单只股票做 AI 深度分析"""
        logger.info(f"分析 {sr.ticker} (#{sr.rank}, scan_score={sr.score})...")

        try:
            final_state, signal = graph.propagate(sr.ticker, date_str)
        except Exception as e:
            logger.error(f"TradingAgentsGraph.propagate({sr.ticker}) 失败: {e}")
            return None

        # 提取 TradeIntent
        trade_intent = final_state.get("final_trade_intent") if final_state else None
        confidence = "medium"
        entry_price = None
        stop_loss = None
        take_profit = None
        rationale = ""

        if trade_intent:
            if hasattr(trade_intent, 'confidence'):
                confidence = str(trade_intent.confidence).lower()
            if hasattr(trade_intent, 'rationale_summary'):
                rationale = str(trade_intent.rationale_summary)[:500]
            # 从 risk_controls 提取止损止盈
            risk_ctrls = getattr(trade_intent, 'risk_controls', None)
            if risk_ctrls:
                stop_loss = getattr(risk_ctrls, 'stop_loss', None)
                take_profit = getattr(risk_ctrls, 'take_profit', None)

        # 综合评分: 扫描分(40%) + AI信号强度(60%)
        signal_score = {"BUY": 85, "HOLD": 50, "SELL": 20}.get(signal, 50)
        composite = sr.score / 125 * 40 + signal_score * 0.6

        result = AIAnalysisResult(
            ticker=sr.ticker,
            signal=signal,
            confidence=confidence,
            composite_score=round(composite, 1),
            entry_price=entry_price,
            stop_loss=stop_loss or sr.stop_loss,
            take_profit=take_profit,
            rationale=rationale,
            raw_report={"signal": signal, "confidence": confidence},
        )

        logger.info(f"  → {signal} (置信度={confidence}, 综合分={composite:.1f})")
        return result

    def _to_recommendation(self, sr: ScanResult, ai: AIAnalysisResult, today: str) -> Recommendation:
        """将分析结果转为推荐记录"""
        status = "pending"
        # 非 BUY 或低置信度 → 跳过
        if ai.signal != "BUY" or ai.confidence not in ("high", "medium"):
            status = "skipped"

        return Recommendation(
            ticker=sr.ticker,
            scan_score=sr.score,
            scan_rr_ratio=sr.rr_ratio,
            scan_stop_loss=sr.stop_loss,
            scan_date=today,
            ai_signal=ai.signal,
            ai_confidence=ai.confidence,
            ai_entry_price=ai.entry_price,
            ai_stop_loss=ai.stop_loss,
            ai_take_profit=ai.take_profit,
            ai_score=ai.composite_score,
            ai_report=json.dumps(ai.raw_report, ensure_ascii=False),
            status=status,
        )

    # ==================== 每小时监控 ====================

    def hourly_monitor(self) -> dict:
        """每小时监控：检查推荐列表 → 择机入场 → 换仓

        仅在美股交易时段运行。
        返回: {"orders_placed": N, "rotations": N, "checks_performed": N}
        """
        self._init_overseer()
        stats = {"orders_placed": 0, "rotations": 0, "checks_performed": 0}

        # 第1步：检查是否在交易时段
        if not self._is_market_hours():
            logger.info("非交易时段，跳过监控")
            return stats

        # 第2步：获取当前持仓
        positions = self.overseer.get_current_positions() if self.overseer else []
        logger.info(f"当前持仓: {len(positions)} 只")

        # 第3步：获取推荐列表
        pending = self.store.get_pending(MAX_RECOMMENDATIONS)
        active = self.store.get_active()

        # 第4步：对 pending 推荐逐只检查入场时机
        for rec in pending:
            stats["checks_performed"] += 1
            try:
                self._check_entry_timing(rec, positions)
            except Exception as e:
                logger.error(f"检查 {rec.ticker} 入场时机出错: {e}")

        # 第5步：对现有持仓评估是否需要卖出或换仓
        for rec in active:
            stats["checks_performed"] += 1
            try:
                should_exit, reason = self._evaluate_exit(rec, positions)
                if should_exit:
                    self._execute_exit(rec, reason)
                    stats["orders_placed"] += 1
            except Exception as e:
                logger.error(f"评估 {rec.ticker} 出场出错: {e}")

        # 第6步：换仓检测
        if self.overseer:
            candidates = [r for r in pending if r.is_buy_signal]
            rotation = self.overseer.find_best_rotation(positions, candidates)
            if rotation:
                logger.info(f"换仓: {rotation.sold_ticker} → {rotation.bought_ticker}")
                self._execute_rotation(rotation, positions)
                stats["rotations"] += 1
                stats["orders_placed"] += 2  # 一卖一买

        return stats

    def _is_market_hours(self) -> bool:
        """判断当前是否美股交易时段（简化版）"""
        now_utc = datetime.utcnow()
        weekday = now_utc.weekday()  # 0=周一, 6=周日
        if weekday >= 5:
            return False
        hour_est = (now_utc.hour - 4) % 24  # 粗转美东（忽略夏令时细节）
        return 9 <= hour_est <= 16

    def _check_entry_timing(self, rec: Recommendation, positions: List):
        """检查一只推荐股是否适合现在入场"""
        ticker = rec.ticker

        # 检查是否已在持仓中
        if any(p.ticker == ticker for p in positions):
            return

        # 检查仓位容量
        if self.overseer and not self.overseer.can_open_new_position(positions):
            logger.info(f"仓位已满 ({len(positions)}/{MAX_RECOMMENDATIONS})，跳过 {ticker}")
            return

        # 获取当前价格
        current_price = self._get_current_price(ticker)
        if not current_price:
            self.store.log_hourly_check(HourlyCheck(
                ticker=ticker, current_price=0,
                signal="price_unavailable", market_regime="unknown",
                notes="无法获取当前价格",
            ))
            return

        # 检查是否在入场价范围内
        entry_price = rec.ai_entry_price or current_price
        deviation_pct = abs(current_price - entry_price) / entry_price * 100

        if deviation_pct > ENTRY_PRICE_TOLERANCE_PCT:
            self.store.log_hourly_check(HourlyCheck(
                ticker=ticker, current_price=current_price,
                signal="price_deviation",
                market_regime="unknown",
                notes=f"价格偏离 {deviation_pct:.1f}% (> {ENTRY_PRICE_TOLERANCE_PCT}%)",
            ))
            return

        # 执行入场
        self._execute_entry(rec, current_price)

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """获取当前价格"""
        try:
            if self.overseer and self.overseer.alpaca:
                quote = self.overseer.alpaca.get_latest_quote(ticker)
                if hasattr(quote, 'bid_price'):
                    return float(quote.bid_price)
                if isinstance(quote, dict):
                    return float(quote.get("bid_price", quote.get("last_price", 0)))
        except Exception:
            pass

        # 兜底：yfinance
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            price = t.fast_info.get("lastPrice") or t.info.get("regularMarketPrice")
            if price:
                return float(price)
        except Exception:
            pass

        return None

    def _execute_entry(self, rec: Recommendation, current_price: float):
        """执行入场交易"""
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            from tradingagents.agents.schemas import TradeIntent, ExecutableAction

            alpaca = AlpacaUtils()
            dollar_amount = self.config.get("default_trade_amount", 1000)

            trade_intent = TradeIntent(
                symbol=rec.ticker,
                action=ExecutableAction.BUY,
                current_position="NEUTRAL",
                target_position="LONG",
                position_transition="OPEN_LONG",
                confidence=rec.ai_confidence,
                order_intent=None,
                planned_actions=[],
                risk_controls={},
                execution_constraints={},
                rationale_summary=(
                    f"扫描评分 {rec.scan_score:.0f}/125, AI综合分 {rec.ai_score:.0f}/100, "
                    f"信号 {rec.ai_signal}, 入场价 ~${current_price:.2f}"
                ),
            )

            alpaca.execute_trade_intent(
                symbol=rec.ticker,
                current_position="NEUTRAL",
                trade_intent=trade_intent,
                dollar_amount=dollar_amount,
                allow_shorts=False,
            )

            self.store.update_status(rec.id or 0, "active", notes=f"入场价 ${current_price:.2f}")
            logger.info(f"✅ 入场: {rec.ticker} @ ${current_price:.2f}, 金额 ${dollar_amount}")

        except Exception as e:
            logger.error(f"入场 {rec.ticker} 失败: {e}")
            self.store.log_hourly_check(HourlyCheck(
                ticker=rec.ticker, current_price=current_price,
                signal="entry_failed", market_regime="unknown",
                notes=str(e)[:200],
            ))

    def _evaluate_exit(self, rec: Recommendation, positions: List) -> tuple:
        """评估是否应该卖出持仓

        返回 (should_exit: bool, reason: str)
        """
        ticker = rec.ticker
        pos = next((p for p in positions if p.ticker == ticker), None)
        if not pos:
            return False, ""

        # 止损检查
        if pos.is_losing:
            return True, f"止损触发 ({pos.unrealized_pnl_pct:.1f}%)"

        # 表现不佳检查
        if pos.is_underperforming:
            # 重新用 AI 快速评估
            try:
                graph = self._get_graph()
                _, signal = graph.propagate(ticker, datetime.now().strftime("%Y-%m-%d"))
                if signal == "SELL":
                    return True, f"AI 重新评估建议卖出 ({pos.unrealized_pnl_pct:.1f}%)"
            except Exception:
                pass

        return False, ""

    def _execute_exit(self, rec: Recommendation, reason: str):
        """卖出持仓"""
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            alpaca = AlpacaUtils()
            alpaca.close_position(rec.ticker, percentage=100.0)
            self.store.update_status(rec.id or 0, "executed", notes=reason)
            logger.info(f"✅ 卖出: {rec.ticker} (原因: {reason})")
        except Exception as e:
            logger.error(f"卖出 {rec.ticker} 失败: {e}")

    def _execute_rotation(self, event, positions: List):
        """执行换仓：先卖后买"""
        # 卖出
        sell_rec = self.store.get_by_ticker(event.sold_ticker)
        if sell_rec:
            self._execute_exit(sell_rec, event.reason)

        # 买入
        buy_rec = self.store.get_by_ticker(event.bought_ticker)
        if buy_rec:
            current_price = self._get_current_price(event.bought_ticker)
            if current_price:
                self._execute_entry(buy_rec, current_price)

        # 记录
        self.store.log_rotation(event)
