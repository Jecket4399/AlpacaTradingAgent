"""集成层统一配置"""

import os
from pathlib import Path

# ---- 路径 ----
BASE_DIR = Path(os.getenv("INTEGRATION_BASE_DIR", Path(__file__).parent.parent))
SCAN_OUTPUT_URL = os.getenv(
    "INTEGRATION_SCAN_URL",
    "https://raw.githubusercontent.com/Jecket4399/stock-screener/main/data/daily_scans/latest_optimized_scan.txt",
)
LOCAL_SCAN_PATH = Path(os.getenv("INTEGRATION_LOCAL_SCAN_PATH", "")) if os.getenv("INTEGRATION_LOCAL_SCAN_PATH") else None
DB_PATH = Path(os.getenv("INTEGRATION_DB_PATH", Path.home() / ".tradingagents" / "recommendations.db"))

# ---- 每日分析 ----
TOP_N_CANDIDATES = int(os.getenv("INTEGRATION_TOP_N", "25"))
ANALYSIS_BATCH_SIZE = int(os.getenv("INTEGRATION_BATCH_SIZE", "3"))  # 并行分析数
ANALYSIS_BATCH_DELAY = float(os.getenv("INTEGRATION_BATCH_DELAY", "5.0"))  # 批次间延迟（秒）

# ---- 推荐列表 ----
MAX_RECOMMENDATIONS = int(os.getenv("INTEGRATION_MAX_RECOMMENDATIONS", "25"))
RECOMMENDATION_TTL_DAYS = int(os.getenv("INTEGRATION_RECOMMENDATION_TTL", "7"))  # 超过7天未入场则过期
MIN_AI_CONFIDENCE = os.getenv("INTEGRATION_MIN_CONFIDENCE", "medium")  # 最低置信度要求

# ---- 每小时监控 ----
MARKET_OPEN_EST = 9   # 美东时间开盘 (手工设，避开夏令时混乱)
MARKET_CLOSE_EST = 16
MONITOR_INTERVAL_MINUTES = int(os.getenv("INTEGRATION_MONITOR_INTERVAL", "60"))
ENTRY_PRICE_TOLERANCE_PCT = float(os.getenv("INTEGRATION_ENTRY_TOLERANCE", "2.0"))  # 入场价容忍度（上下2%内视为可入场）

# ---- 仓位管理 ----
MAX_POSITIONS = int(os.getenv("INTEGRATION_MAX_POSITIONS", "10"))
MAX_POSITION_PCT = float(os.getenv("INTEGRATION_MAX_POSITION_PCT", "15.0"))  # 单只最大仓位15%
MIN_HOLDING_DAYS = int(os.getenv("INTEGRATION_MIN_HOLDING_DAYS", "3"))  # 最小持有天数，防频繁换仓
UNDERPERFORM_THRESHOLD_PCT = float(os.getenv("INTEGRATION_UNDERPERFORM_PCT", "2.0"))  # 持有5天浮盈<此值→评估卖出
UNDERPERFORM_DAYS = int(os.getenv("INTEGRATION_UNDERPERFORM_DAYS", "5"))

# ---- 换仓 ----
ROTATION_HYSTERESIS = float(os.getenv("INTEGRATION_ROTATION_HYSTERESIS", "1.2"))  # 候选分 > 持仓分 × 1.2 才换
MAX_DAILY_TRADES = int(os.getenv("INTEGRATION_MAX_DAILY_TRADES", "5"))  # 每天最多交易次数（买入+卖出）

# ---- 通知 ----
NOTIFY_TELEGRAM_TOKEN = os.getenv("INTEGRATION_TELEGRAM_TOKEN", "")
NOTIFY_TELEGRAM_CHAT_ID = os.getenv("INTEGRATION_TELEGRAM_CHAT_ID", "")


def ensure_dirs():
    """确保所有必需目录存在"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
