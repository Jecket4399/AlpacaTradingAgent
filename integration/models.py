"""数据模型定义"""

from datetime import datetime
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field


class RecommendationStatus(str, Enum):
    PENDING = "pending"        # 待入场（AI 分析通过，等合适时机）
    ACTIVE = "active"          # 已买入持有中
    EXECUTED = "executed"      # 已卖出（获利或止损）
    REJECTED = "rejected"      # 被风控拒绝
    EXPIRED = "expired"        # 超过有效期未入场
    SKIPPED = "skipped"        # AI 分析不通过


class AISignal(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass
class ScanResult:
    """stock-screener 扫描结果中的单只股票"""
    ticker: str
    rank: int                     # 排名 (1-25)
    score: float                  # Minervini 评分 (0-125)
    phase: int                    # 阶段 (2=上升趋势)
    stop_loss: Optional[float]    # 建议止损价
    rr_ratio: Optional[float]     # 盈亏比
    rs_slope: Optional[float]     # 相对强度
    entry_quality: Optional[str]  # 入场质量
    reasons: list[str] = field(default_factory=list)


@dataclass
class AIAnalysisResult:
    """TradingAgentsGraph 分析一只股票后的结果"""
    ticker: str
    signal: str                   # BUY / HOLD / SELL
    confidence: str               # high / medium / low
    composite_score: float        # 综合评分 (0-100)
    entry_price: Optional[float]  # 建议入场价
    stop_loss: Optional[float]    # AI 计算的止损
    take_profit: Optional[float]  # 目标价
    rationale: str = ""           # 决策理由摘要
    raw_report: dict = field(default_factory=dict)


@dataclass
class Recommendation:
    """推荐列表中的一只股票（数据库中的一行）"""
    id: Optional[int] = None
    ticker: str = ""
    scan_score: float = 0.0
    scan_rr_ratio: Optional[float] = None
    scan_stop_loss: Optional[float] = None
    scan_date: Optional[str] = None
    ai_signal: str = ""
    ai_confidence: str = ""
    ai_entry_price: Optional[float] = None
    ai_stop_loss: Optional[float] = None
    ai_take_profit: Optional[float] = None
    ai_score: float = 0.0
    ai_report: str = "{}"
    status: str = "pending"
    added_at: Optional[str] = None
    activated_at: Optional[str] = None
    executed_at: Optional[str] = None
    pnl: Optional[float] = None
    notes: str = ""

    @property
    def is_buy_signal(self) -> bool:
        return self.ai_signal == "BUY" and self.ai_confidence in ("high", "medium")

    @property
    def days_since_added(self) -> int:
        if not self.added_at:
            return 0
        added = datetime.fromisoformat(self.added_at)
        return (datetime.now() - added).days


@dataclass
class PositionSnapshot:
    """当前持仓快照"""
    ticker: str
    qty: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    cost_basis: float
    current_price: float
    days_held: int
    recommendation_id: Optional[int] = None

    @property
    def is_underperforming(self) -> bool:
        """持有超过5天且浮盈低于2%，视为表现不佳"""
        return self.days_held > 5 and self.unrealized_pnl_pct < 2.0

    @property
    def is_losing(self) -> bool:
        """浮亏超过安全网阈值（默认10%），仅作为括号订单失效时的兜底"""
        from .config import SAFETY_NET_STOP_PCT
        return self.unrealized_pnl_pct < -SAFETY_NET_STOP_PCT


@dataclass
class RotationEvent:
    """换仓事件"""
    sold_ticker: str
    bought_ticker: str
    sold_score: float
    bought_score: float
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class HourlyCheck:
    """每小时检查记录"""
    ticker: str
    current_price: float
    signal: str         # still_valid / conditions_changed / entry_good
    market_regime: str
    notes: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())
