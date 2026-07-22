"""三引擎集成主流水线

架构：
  stock-screener → daily_stock_analysis (AI分析) → 本模块 (提取BUY) → Alpaca (监控执行)

daily_batch: 从 daily_stock_analysis 结果中提取 BUY 推荐 → 填充推荐列表
hourly_monitor: 每小时检查推荐列表 → 择机入场 → 换仓（仅模拟盘）
"""

import json
import logging
import re
import urllib.request
from datetime import datetime
from typing import List, Optional

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
            # 匹配摘要行: "🟢 Apple Inc.(AAPL): 买入 | 评分 75 | 强烈看多"
            # 或: "⚪ Apple Inc.(AAPL): 持有观察 | 评分 59 | 看多"
            m = re.match(
                r".*\((\w+)\)[：:]\s*(买入|持有|卖出|减仓|观望|持有观察)\s*\|\s*评分\s*(\d+)",
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

    # ==================== 每小时监控（保持不变）====================

    def hourly_monitor(self) -> dict:
        """每小时监控推荐列表，择机入场 + 换仓"""
        self._init_overseer()
        stats = {"orders_placed": 0, "rotations": 0, "checks_performed": 0}

        if not self._is_market_hours():
            logger.info("非交易时段")
            return stats

        positions = self.overseer.get_current_positions() if self.overseer else []
        logger.info(f"当前持仓: {len(positions)} 只")

        pending = self.store.get_pending(MAX_RECOMMENDATIONS)

        # 检查入场时机
        for rec in pending:
            stats["checks_performed"] += 1
            try:
                self._check_entry_timing(rec, positions)
            except Exception as e:
                logger.error(f"检查入场 {rec.ticker}: {e}")

        # 评估出场
        active = self.store.get_active()
        for rec in active:
            stats["checks_performed"] += 1
            try:
                should_exit, reason = self._evaluate_exit(rec, positions)
                if should_exit:
                    self._execute_exit(rec, reason)
                    stats["orders_placed"] += 1
            except Exception as e:
                logger.error(f"评估出场 {rec.ticker}: {e}")

        # 换仓检测
        if self.overseer:
            candidates = [r for r in pending if r.is_buy_signal]
            rotation = self.overseer.find_best_rotation(positions, candidates)
            if rotation:
                self._execute_rotation(rotation, positions)
                stats["rotations"] += 1
                stats["orders_placed"] += 2

        return stats

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
            from tradingagents.agents.schemas import TradeIntent, ExecutableAction

            alpaca = AlpacaUtils()
            amount = self.config.get("default_trade_amount", 1000)
            intent = TradeIntent(
                symbol=rec.ticker, action=ExecutableAction.BUY,
                current_position="NEUTRAL", target_position="LONG",
                position_transition="OPEN_LONG", confidence=rec.ai_confidence,
                order_intent=None, planned_actions=[], risk_controls={},
                execution_constraints={},
                rationale_summary=f"daily_stock_analysis 分析 BUY, 评分{rec.ai_score:.0f}",
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
