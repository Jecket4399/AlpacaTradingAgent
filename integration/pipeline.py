"""三引擎集成主流水线

架构：
  stock-screener → daily_stock_analysis (初次筛选) → 本模块 (提取BUY)
  → TradingAgentsGraph (每小时AI决策) → Alpaca (执行)

daily_batch: 从 daily_stock_analysis 结果中提取 BUY → 推荐列表
hourly_monitor: 每小时对每只待决策股票跑轻量 AI 分析 → BUY/SELL → 执行
"""

import json
import logging
import re
import time
import urllib.request
from datetime import datetime
from typing import List, Optional, Tuple

from .config import (
    MAX_RECOMMENDATIONS, ENTRY_PRICE_TOLERANCE_PCT, MAX_DAILY_TRADES,
    RECOMMENDATION_TTL_DAYS, MONITOR_INTERVAL_MINUTES,
)
from .models import (
    Recommendation, HourlyCheck, RecommendationStatus,
)
from .recommendation_store import RecommendationStore
from .portfolio_overseer import PortfolioOverseer

logger = logging.getLogger(__name__)

# daily_stock_analysis 的 GitHub Actions artifact 或报告 URL
DSA_ARTIFACT_URL = (
    "https://raw.githubusercontent.com/Jecket4399/daily-stock-analysis/"
    "main/reports/report_latest.md"
)


class IntegrationPipeline:
    """主编排器：从 daily_stock_analysis 结果提取 BUY，监控执行"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.store = RecommendationStore()
        self.overseer: Optional[PortfolioOverseer] = None

    def _init_overseer(self):
        """延迟初始化仓位监管器（需要 Alpaca）"""
        if self.overseer:
            return
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            self.overseer = PortfolioOverseer(self.store, AlpacaUtils())
        except Exception as e:
            logger.warning(f"无法连接 Alpaca: {e}，仓位监控不可用")
            self.overseer = PortfolioOverseer(self.store, None)

    # ==================== 每日：提取 BUY 推荐 ====================

    def daily_sync(self, source_url: Optional[str] = None) -> dict:
        """从 daily_stock_analysis 的最新分析结果中提取 BUY 推荐

        daily_stock_analysis 每天 18:00 CST 自动分析，
        GitHub Actions 将报告上传为 artifact。
        本方法从 artifact 或公开报告中解析 BUY/HOLD/SELL 信号。

        返回: {"found": N, "buy": N, "added": N}
        """
        logger.info("=" * 60)
        logger.info(f"每日同步：从 daily_stock_analysis 提取 BUY 推荐 ({datetime.now()})")
        logger.info("=" * 60)

        # 第1步：尝试从 daily_stock_analysis repo 拉取最新报告
        signals = self._fetch_dsa_results(source_url)
        if not signals:
            logger.warning("未获取到 daily_stock_analysis 分析结果，本次跳过")
            return {"found": 0, "buy": 0, "added": 0}

        logger.info(f"获取到 {len(signals)} 只股票的分析结果")
        buy_count = sum(1 for s in signals if s.get("action") == "BUY")
        logger.info(f"其中 BUY 信号: {buy_count} 只")

        # 第2步：只把 BUY 的放入推荐列表
        today = datetime.now().strftime("%Y-%m-%d")
        added = 0
        for sig in signals:
            if sig.get("action") != "BUY":
                continue

            rec = Recommendation(
                ticker=sig.get("ticker", ""),
                scan_score=float(sig.get("score", 0)),
                scan_date=today,
                ai_signal="BUY",
                ai_confidence=sig.get("confidence", "medium"),
                ai_entry_price=_safe_float(sig.get("entry_price")),
                ai_stop_loss=_safe_float(sig.get("stop_loss")),
                ai_take_profit=_safe_float(sig.get("take_profit")),
                ai_score=float(sig.get("score", 0)),
                ai_report=json.dumps(sig, ensure_ascii=False),
                status="pending",
            )
            rec_id = self.store.add_recommendation(rec)
            if rec_id:
                added += 1

        # 第3步：清理
        self.store.expire_old_pending()
        self.store.sweep_duplicates()

        summary = self.store.get_summary()
        logger.info(f"同步完成: 发现={len(signals)}, BUY={buy_count}, 新增推荐={added}")
        logger.info(f"推荐列表: pending={summary['pending']}, active={summary['active']}")

        return {"found": len(signals), "buy": buy_count, "added": added}

    def _fetch_dsa_results(self, url: Optional[str] = None) -> List[dict]:
        """从 daily_stock_analysis 获取最新分析结果

        尝试顺序：
        1. GitHub Actions 最近一次运行的 artifact（通过 gh CLI）
        2. 仓库中的公开报告文件
        3. 兜底：返回空
        """
        results = []

        # 方式1：通过 gh CLI 下载最近一次运行的分析报告
        try:
            results = self._fetch_via_gh_cli()
            if results:
                return results
        except Exception as e:
            logger.debug(f"gh CLI 方式失败: {e}")

        # 方式2：从仓库公开 URL 读取
        target_url = url or DSA_ARTIFACT_URL
        try:
            req = urllib.request.Request(target_url)
            req.add_header("User-Agent", "integration-pipeline/1.0")
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8")
            results = self._parse_dsa_report(content)
        except Exception as e:
            logger.warning(f"从 URL 获取分析结果失败: {e}")

        return results

    def _fetch_via_gh_cli(self) -> List[dict]:
        """通过 gh CLI 下载 daily_stock_analysis 最近一次 artifact"""
        import subprocess, tempfile, zipfile, os
        from pathlib import Path

        repo = "Jecket4399/daily-stock-analysis"
        # 获取最近一次完成的 workflow run
        result = subprocess.run(
            ["gh", "run", "list", "-R", repo, "--workflow", "00-daily-analysis.yml",
             "--status", "success", "--limit", "1", "--json", "databaseId"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []

        runs = json.loads(result.stdout)
        if not runs:
            return []

        run_id = runs[0]["databaseId"]
        logger.info(f"找到 daily_stock_analysis 最近运行: {run_id}")

        # 下载 artifact
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["gh", "run", "download", str(run_id), "-R", repo,
                 "-n", "analysis-reports-*", "-D", tmpdir],
                capture_output=True, timeout=30,
            )
            # 找 reports 目录下的 md 文件
            reports_dir = Path(tmpdir) / "reports"
            if not reports_dir.exists():
                reports_dir = Path(tmpdir)
            for md_file in reports_dir.rglob("report_*.md"):
                content = md_file.read_text(encoding="utf-8")
                return self._parse_dsa_report(content)

        return []

    def _parse_dsa_report(self, content: str) -> List[dict]:
        """解析 daily_stock_analysis 的 Markdown 报告，提取每只股票的决策信号

        报告格式示例：
            🟢 Apple Inc.(AAPL): 买入 | 评分 75 | 强烈看多
            🟡 Microsoft(MSFT): 持有观察 | 评分 59 | 看多
            🔴 Tesla(TSLA): 卖出 | 评分 35 | 看空

        提取: ticker, action(BUY/HOLD/SELL), score, trend
        """
        results = []

        for line in content.split("\n"):
            # 匹配摘要行格式:
            #   🟡 **Apple Inc.(AAPL)**: 持有 | 评分 59 | 强烈看多
            #   🟠 **Tesla, Inc.(TSLA)**: 减仓 | 评分 35 | 看空
            #   🟢 **XXX(CODE)**: 买入 | 评分 75 | ...
            m = re.match(
                r".*\((\w+)\)[*\s]*[：:]\s*(买入|持有|持有观察|卖出|减仓|观望)\s*\|\s*评分\s*(\d+)",
                line
            )
            if not m:
                continue

            ticker = m.group(1)
            action_cn = m.group(2)
            score = int(m.group(3))

            # 中文 → 英文信号
            action_map = {
                "买入": "BUY", "持有": "HOLD", "持有观察": "HOLD",
                "卖出": "SELL", "减仓": "SELL", "观望": "HOLD",
            }
            action = action_map.get(action_cn, "HOLD")

            # 置信度：评分 >= 70 → high, >=50 → medium, <50 → low
            confidence = "high" if score >= 70 else ("medium" if score >= 50 else "low")

            results.append({
                "ticker": ticker,
                "action": action,
                "score": score,
                "confidence": confidence,
                "trend": "",
            })

        logger.info(f"从报告解析到 {len(results)} 条信号")
        return results

    # ==================== 每小时监控（完整 AI 决策链）====================

    def hourly_monitor(self) -> dict:
        """每小时监控：对推荐列表中的每只股票，跑完整 TradingAgentsGraph 分析链来决定买卖。

        完整决策链：
          5 分析师(并行) → 证据计分板 → 牛熊辩论 → Research Manager
          → Trader → 风险三人辩论 → Risk Judge → TradeIntent → 下单
        """
        self._init_overseer()
        stats = {"analyzed": 0, "buy_signals": 0, "sell_signals": 0,
                 "entries": 0, "exits": 0, "rotations": 0}

        if not self._is_market_hours():
            logger.info("非交易时段，跳过")
            return stats

        # 第1步：获取当前持仓
        positions = self.overseer.get_current_positions() if self.overseer else []
        logger.info(f"当前持仓: {len(positions)} 只")

        today = datetime.now().strftime("%Y-%m-%d")

        # 第2步：对每只 pending BUY 推荐，跑完整分析链决定是否买入
        pending = self.store.get_pending(MAX_RECOMMENDATIONS)
        logger.info(f"待决策股票: {len(pending)} 只 pending + {self.store.get_count_by_status('active')} 只持仓")

        for rec in pending:
            # 跳过已在持仓中的
            if any(p.ticker == rec.ticker for p in positions):
                continue
            # 仓位满了就停
            if self.overseer and not self.overseer.can_open_new_position(positions):
                logger.info(f"仓位已满，停止分析")
                break

            stats["analyzed"] += 1
            signal, trade_intent = self._run_full_analysis(rec.ticker, today)

            if signal == "BUY":
                stats["buy_signals"] += 1
                executed = self._execute_full_intent(rec, trade_intent)
                if executed:
                    stats["entries"] += 1
                    # 更新持仓列表
                    positions = self.overseer.get_current_positions() if self.overseer else []
            elif signal == "SELL":
                stats["sell_signals"] += 1

            # 记录检查
            self.store.log_hourly_check(HourlyCheck(
                ticker=rec.ticker, current_price=0,
                signal=signal, market_regime="unknown",
                notes=f"AI完整分析: {signal}",
            ))

            # API 限速保护
            time.sleep(3)

        # 第3步：对现有持仓，跑完整分析链决定是否卖出
        active = self.store.get_active()
        for rec in active:
            pos = next((p for p in positions if p.ticker == rec.ticker), None)
            if not pos:
                continue
            if pos.days_held < self.config.get("min_holding_days", 1):
                continue

            stats["analyzed"] += 1
            signal, trade_intent = self._run_full_analysis(rec.ticker, today)

            if signal == "SELL":
                stats["sell_signals"] += 1
                self._execute_exit(rec, f"AI分析建议卖出")
                stats["exits"] += 1
            elif pos.is_losing:
                # 止损兜底：浮亏超5%强制卖出，不等AI
                self._execute_exit(rec, f"止损兜底 ({pos.unrealized_pnl_pct:.1f}%)")
                stats["exits"] += 1

            self.store.log_hourly_check(HourlyCheck(
                ticker=rec.ticker, current_price=pos.current_price,
                signal=signal, market_regime="unknown",
                notes=f"持仓评估: {signal}",
            ))
            time.sleep(3)

        # 第4步：换仓检测
        if self.overseer and stats["entries"] == 0:
            candidates = [r for r in pending if r.is_buy_signal]
            rotation = self.overseer.find_best_rotation(positions, candidates)
            if rotation:
                self._execute_rotation(rotation, positions)
                stats["rotations"] += 1

        logger.info(f"监控完成: 分析={stats['analyzed']}, "
                     f"BUY={stats['buy_signals']}, SELL={stats['sell_signals']}, "
                     f"入场={stats['entries']}, 出场={stats['exits']}, 换仓={stats['rotations']}")
        return stats

    def _run_full_analysis(self, ticker: str, date_str: str) -> Tuple[str, Optional[dict]]:
        """跑完整的 TradingAgentsGraph 分析链：5分析师 → 辩论 → 裁决

        返回: (signal: BUY/HOLD/SELL, trade_intent: dict or None)
        """
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG

            cfg = DEFAULT_CONFIG.copy()
            cfg["llm_provider"] = self.config.get("llm_provider", "deepseek")
            cfg["deep_think_llm"] = self.config.get("deep_llm", "deepseek-chat")
            cfg["quick_think_llm"] = self.config.get("quick_llm", "deepseek-chat")
            cfg["max_debate_rounds"] = self.config.get("debate_rounds", 3)
            cfg["max_risk_discuss_rounds"] = self.config.get("risk_rounds", 2)
            cfg["allow_shorts"] = False  # 投资模式: BUY/HOLD/SELL
            cfg.update(self.config.get("extra_config", {}))

            graph = TradingAgentsGraph(
                selected_analysts=["market", "social", "news", "fundamentals", "macro"],
                config=cfg,
            )

            logger.info(f"  完整分析 {ticker} (5分析师→辩论→裁决)...")
            start = time.time()
            final_state, signal = graph.propagate(ticker, date_str)
            elapsed = time.time() - start

            trade_intent = final_state.get("final_trade_intent") if final_state else None
            logger.info(f"  结果: {signal} (耗时 {elapsed:.0f}s)")

            return signal, trade_intent

        except Exception as e:
            logger.error(f"  分析 {ticker} 失败: {e}", exc_info=True)
            return "HOLD", None

    def _execute_full_intent(self, rec: Recommendation, trade_intent) -> bool:
        """执行完整的 TradeIntent（由 Risk Judge 产出的下单指令）"""
        if not trade_intent:
            logger.info(f"  {rec.ticker}: 无 TradeIntent，跳过执行")
            return False

        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils

            alpaca = AlpacaUtils()
            amount = self.config.get("default_trade_amount", 1000)

            result = alpaca.execute_trade_intent(
                symbol=rec.ticker,
                current_position="NEUTRAL",
                trade_intent=trade_intent,
                dollar_amount=amount,
                allow_shorts=False,
            )

            if result and not result.get("safety_blocked") and not result.get("error"):
                self.store.update_status(rec.id or 0, "active",
                    notes=f"AI决策入场, signal={getattr(trade_intent, 'action', '?')}")
                logger.info(f"  ✅ {rec.ticker}: AI决策入场成功")
                return True
            elif result and result.get("safety_blocked"):
                logger.info(f"  ⚠️ {rec.ticker}: 安全层拦截")
            else:
                logger.info(f"  ⚠️ {rec.ticker}: {result.get('error', '未知错误') if result else '无结果'}")
            return False

        except Exception as e:
            logger.error(f"  ❌ {rec.ticker}: 执行失败 - {e}")
            return False

    def _is_market_hours(self) -> bool:
        now = datetime.utcnow()
        if now.weekday() >= 5:
            return False
        hour_est = (now.hour - 4) % 24
        return 9 <= hour_est <= 16

    def _check_entry_timing(self, rec: Recommendation, positions: List):
        """检查入场时机"""
        if any(p.ticker == rec.ticker for p in positions):
            return
        if self.overseer and not self.overseer.can_open_new_position(positions):
            logger.info(f"仓位已满，跳过 {rec.ticker}")
            return

        current_price = self._get_current_price(rec.ticker)
        if not current_price:
            return

        entry_price = rec.ai_entry_price or current_price
        deviation = abs(current_price - entry_price) / entry_price * 100
        if deviation > ENTRY_PRICE_TOLERANCE_PCT:
            self.store.log_hourly_check(HourlyCheck(
                ticker=rec.ticker, current_price=current_price,
                signal="price_deviation", market_regime="unknown",
                notes=f"价格偏离 {deviation:.1f}%",
            ))
            return

        self._execute_entry(rec, current_price)

    def _get_current_price(self, ticker: str) -> Optional[float]:
        try:
            if self.overseer and self.overseer.alpaca:
                quote = self.overseer.alpaca.get_latest_quote(ticker)
                if hasattr(quote, 'bid_price'):
                    return float(quote.bid_price)
                if isinstance(quote, dict):
                    return float(quote.get("bid_price", quote.get("last_price", 0)))
        except Exception:
            pass
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            return float(t.fast_info.get("lastPrice") or t.info.get("regularMarketPrice", 0))
        except Exception:
            pass
        return None

    def _execute_entry(self, rec: Recommendation, current_price: float):
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            from tradingagents.agents.schemas import (
                TradeIntent, ExecutableAction, OrderIntent,
                RiskControls, ExecutionConstraints,
            )

            alpaca = AlpacaUtils()
            amount = self.config.get("default_trade_amount", 1000)
            conf = rec.ai_confidence or "medium"

            intent = TradeIntent(
                symbol=rec.ticker,
                trading_mode="investment",
                action=ExecutableAction.BUY,
                current_position="NEUTRAL",
                target_position="LONG",
                position_transition="OPEN_LONG",
                confidence=conf,
                order_intent=OrderIntent(
                    order_type="market",
                    order_class="simple",
                    side="buy",
                    sizing_basis="configured_notional",
                    notional_usd=float(amount),
                ),
                risk_controls=RiskControls(
                    mode="execution_allowed",
                    stop_loss_price=rec.ai_stop_loss,
                    take_profit_price=rec.ai_take_profit,
                ),
                execution_constraints=ExecutionConstraints(
                    allow_shorts=False,
                    asset_class="equity",
                    requires_open_market=True,
                ),
                rationale_summary=f"daily_stock_analysis BUY, score={rec.ai_score:.0f}",
            )
            alpaca.execute_trade_intent(rec.ticker, "NEUTRAL", intent, amount, allow_shorts=False)
            self.store.update_status(rec.id or 0, "active", notes=f"入场 ${current_price:.2f}")
            logger.info(f"✅ 入场: {rec.ticker} @ ${current_price:.2f}")
        except Exception as e:
            logger.error(f"入场 {rec.ticker} 失败: {e}")

    def _evaluate_exit(self, rec: Recommendation, positions: List) -> tuple:
        ticker = rec.ticker
        pos = next((p for p in positions if p.ticker == ticker), None)
        if not pos:
            return False, ""
        if pos.is_losing:
            return True, f"止损 ({pos.unrealized_pnl_pct:.1f}%)"
        if pos.is_underperforming:
            return True, f"表现不佳 ({pos.unrealized_pnl_pct:.1f}%, 持有{pos.days_held}天)"
        return False, ""

    def _execute_exit(self, rec: Recommendation, reason: str):
        try:
            from tradingagents.dataflows.alpaca_utils import AlpacaUtils
            AlpacaUtils().close_position(rec.ticker, percentage=100.0)
            self.store.update_status(rec.id or 0, "executed", notes=reason)
            logger.info(f"✅ 卖出: {rec.ticker} ({reason})")
        except Exception as e:
            logger.error(f"卖出 {rec.ticker} 失败: {e}")

    def _execute_rotation(self, event, positions: List):
        sell_rec = self.store.get_by_ticker(event.sold_ticker)
        if sell_rec:
            self._execute_exit(sell_rec, event.reason)
        buy_rec = self.store.get_by_ticker(event.bought_ticker)
        if buy_rec:
            price = self._get_current_price(event.bought_ticker)
            if price:
                self._execute_entry(buy_rec, price)
        self.store.log_rotation(event)


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
