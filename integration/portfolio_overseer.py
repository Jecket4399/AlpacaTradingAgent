"""仓位监控与换仓决策

评估现有持仓质量，检测弱持仓，决定是否换仓。
复用 AlpacaTradingAgent 的组合管理和风控模块。
"""

import logging
from datetime import datetime
from typing import List, Optional

from .config import (
    MAX_POSITIONS, ROTATION_HYSTERESIS, MAX_DAILY_TRADES,
    UNDERPERFORM_THRESHOLD_PCT, UNDERPERFORM_DAYS, MIN_HOLDING_DAYS,
)
from .models import PositionSnapshot, Recommendation, RotationEvent, RecommendationStatus
from .recommendation_store import RecommendationStore

logger = logging.getLogger(__name__)


class PortfolioOverseer:
    """仓位监管器：评估持仓 + 换仓决策"""

    def __init__(self, store: RecommendationStore, alpaca_utils=None):
        self.store = store
        self.alpaca = alpaca_utils  # AlpacaUtils 实例，用于获取实时持仓
        self._daily_rotation_count = 0

    # ---- 持仓快照 ----

    def get_current_positions(self) -> List[PositionSnapshot]:
        """从 Alpaca 获取当前持仓并匹配推荐列表"""
        if not self.alpaca:
            logger.warning("AlpacaUtils 未注入，无法获取持仓")
            return []

        try:
            raw_positions = self.alpaca.get_positions_data()
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []

        snapshots: List[PositionSnapshot] = []
        for pos in (raw_positions or []):
            ticker = pos.symbol if hasattr(pos, 'symbol') else pos.get('symbol', '')
            qty = float(pos.qty if hasattr(pos, 'qty') else pos.get('qty', 0))
            if qty == 0:
                continue

            market_value = float(pos.market_value if hasattr(pos, 'market_value')
                                 else pos.get('market_value', 0))
            unrealized_pl = float(pos.unrealized_pl if hasattr(pos, 'unrealized_pl')
                                  else pos.get('unrealized_pl', 0))
            cost_basis = float(pos.cost_basis if hasattr(pos, 'cost_basis')
                               else pos.get('cost_basis', 0))
            current_price = float(pos.current_price if hasattr(pos, 'current_price')
                                  else pos.get('current_price', 0))

            unrealized_pct = (unrealized_pl / abs(cost_basis) * 100) if cost_basis else 0

            # 尝试匹配推荐记录
            rec = self.store.get_by_ticker(ticker)
            days_held = 0
            if rec and rec.activated_at:
                days_held = (datetime.now() - datetime.fromisoformat(rec.activated_at)).days

            snapshots.append(PositionSnapshot(
                ticker=ticker,
                qty=qty,
                market_value=market_value,
                unrealized_pnl=unrealized_pl,
                unrealized_pnl_pct=unrealized_pct,
                cost_basis=cost_basis,
                current_price=current_price,
                days_held=days_held,
                recommendation_id=rec.id if rec else None,
            ))

        return snapshots

    # ---- 弱持仓检测 ----

    def find_underperformers(self, positions: List[PositionSnapshot]) -> List[PositionSnapshot]:
        """找出表现不佳的持仓：
        条件：持有天数 >= UNDERPERFORM_DAYS 且浮盈 < UNDERPERFORM_THRESHOLD_PCT
        或者：浮亏超过 5%
        """
        weak: List[PositionSnapshot] = []
        for pos in positions:
            if pos.days_held < MIN_HOLDING_DAYS:
                continue  # 持有不足最短天数，不评估
            if pos.is_losing:
                weak.append(pos)
                logger.info(f"弱持仓(浮亏): {pos.ticker} {pos.unrealized_pnl_pct:.1f}%")
            elif pos.is_underperforming:
                weak.append(pos)
                logger.info(f"弱持仓(表现差): {pos.ticker} {pos.unrealized_pnl_pct:.1f}% (持有{pos.days_held}天)")
        return weak

    def find_stop_loss_candidates(self, positions: List[PositionSnapshot]) -> List[PositionSnapshot]:
        """找出触及止损线的持仓（浮亏 >= 5%）"""
        return [p for p in positions if p.is_losing]

    # ---- 换仓决策 ----

    def should_rotate(
        self,
        current_position: PositionSnapshot,
        candidate: Recommendation,
        positions: List[PositionSnapshot],
    ) -> Optional[RotationEvent]:
        """判断是否应该换仓

        条件：
        1. 候选评分 > 当前持仓评分 × ROTATION_HYSTERESIS
        2. 持有 >= MIN_HOLDING_DAYS
        3. 持仓表现不佳
        4. 今日交易次数未超上限
        """
        # 最小持有期
        if current_position.days_held < MIN_HOLDING_DAYS:
            return None

        # 候选必须是 BUY 信号且未在持仓中
        if not candidate.is_buy_signal:
            return None
        if any(p.ticker == candidate.ticker for p in positions):
            return None

        # 评分阈值
        current_score = candidate.ai_score  # 候选评分
        # 获取当前持仓的原始 AI 评分
        existing_rec = self.store.get_recommendation_by_id(current_position.recommendation_id or 0)
        if not existing_rec:
            return None
        holding_score = existing_rec.ai_score or 0

        if current_score <= holding_score * ROTATION_HYSTERESIS:
            return None

        # 今日换仓次数
        today_count = self.store.get_today_trade_count()
        if today_count >= MAX_DAILY_TRADES:
            logger.info(f"今日已换仓 {today_count} 次，达上限")
            return None

        # 当前持仓表现不佳 或 止损触发
        if current_position.is_underperforming or current_position.is_losing:
            reason = (
                f"止损换仓 ({current_position.unrealized_pnl_pct:.1f}%)"
                if current_position.is_losing
                else f"弱仓换优 ({current_position.ticker}={holding_score:.0f}分 → {candidate.ticker}={current_score:.0f}分)"
            )
            return RotationEvent(
                sold_ticker=current_position.ticker,
                bought_ticker=candidate.ticker,
                sold_score=holding_score,
                bought_score=current_score,
                reason=reason,
            )

        return None

    def find_best_rotation(
        self,
        positions: List[PositionSnapshot],
        candidates: List[Recommendation],
    ) -> Optional[RotationEvent]:
        """在持仓和候选列表中找最佳换仓机会

        遍历弱持仓，尝试匹配最佳候选。
        优先换掉评分最低的弱持仓，买入评分最高的候选。
        """
        weak_positions = self.find_underperformers(positions)
        if not weak_positions:
            return None

        # 按评分升序（最差的在前）
        weak_positions.sort(key=lambda p: p.unrealized_pnl_pct)

        # 按候选评分降序（最好的在前）
        sorted_candidates = sorted(candidates, key=lambda c: c.ai_score, reverse=True)

        for weak in weak_positions:
            for candidate in sorted_candidates:
                rotation = self.should_rotate(weak, candidate, positions)
                if rotation:
                    return rotation

        return None

    # ---- 仓位容量检查 ----

    def can_open_new_position(self, positions: List[PositionSnapshot]) -> bool:
        """检查是否还能开新仓"""
        return len(positions) < MAX_POSITIONS

    def get_available_slots(self, positions: List[PositionSnapshot]) -> int:
        """剩余可开仓位"""
        return max(0, MAX_POSITIONS - len(positions))
