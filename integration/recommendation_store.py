"""推荐列表 SQLite 持久化存储"""

import json
import logging
import sqlite3
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .config import DB_PATH, RECOMMENDATION_TTL_DAYS, ensure_dirs
from .models import Recommendation, HourlyCheck, RotationEvent, RecommendationStatus

logger = logging.getLogger(__name__)

# Recommendation dataclass 的有效字段名
_REC_FIELDS = {f.name for f in fields(Recommendation)}


def _row_to_rec(row: sqlite3.Row) -> Recommendation:
    """将 SQLite 行转为 Recommendation，过滤多余字段"""
    d = {k: v for k, v in dict(row).items() if k in _REC_FIELDS}
    return Recommendation(**d)


class RecommendationStore:
    """推荐列表 SQLite 数据库管理"""

    def __init__(self, db_path: Optional[Path] = None):
        ensure_dirs()
        self.db_path = Path(db_path or DB_PATH)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """初始化数据库表"""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    scan_score REAL DEFAULT 0,
                    scan_rr_ratio REAL,
                    scan_stop_loss REAL,
                    scan_date TEXT,
                    ai_signal TEXT DEFAULT '',
                    ai_confidence TEXT DEFAULT '',
                    ai_entry_price REAL,
                    ai_stop_loss REAL,
                    ai_take_profit REAL,
                    ai_score REAL DEFAULT 0,
                    ai_report TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    added_at TEXT DEFAULT (datetime('now')),
                    activated_at TEXT,
                    executed_at TEXT,
                    pnl REAL,
                    notes TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS hourly_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    checked_at TEXT DEFAULT (datetime('now')),
                    current_price REAL,
                    signal TEXT DEFAULT '',
                    market_regime TEXT DEFAULT '',
                    notes TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS rotation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sold_ticker TEXT,
                    bought_ticker TEXT,
                    sold_score REAL,
                    bought_score REAL,
                    reason TEXT,
                    timestamp TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_recs_ticker ON recommendations(ticker);
                CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status);
                CREATE INDEX IF NOT EXISTS idx_hourly_ticker ON hourly_checks(ticker);
            """)

    # ---- 推荐列表 CRUD ----

    def add_recommendation(self, rec: Recommendation) -> int:
        """添加一条推荐，返回 ID。如果同一 ticker 已有 pending/active 则跳过"""
        existing = self.get_by_ticker(rec.ticker)
        if existing and existing.status in (RecommendationStatus.PENDING.value,
                                             RecommendationStatus.ACTIVE.value):
            logger.info(f"{rec.ticker} 已在推荐列表中 (状态={existing.status})，跳过")
            return existing.id or 0

        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO recommendations
                    (ticker, scan_score, scan_rr_ratio, scan_stop_loss, scan_date,
                     ai_signal, ai_confidence, ai_entry_price, ai_stop_loss,
                     ai_take_profit, ai_score, ai_report, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec.ticker, rec.scan_score, rec.scan_rr_ratio, rec.scan_stop_loss,
                rec.scan_date, rec.ai_signal, rec.ai_confidence, rec.ai_entry_price,
                rec.ai_stop_loss, rec.ai_take_profit, rec.ai_score, rec.ai_report,
                rec.status or "pending",
            ))
            rec_id = cursor.lastrowid or 0
            logger.info(f"添加推荐: {rec.ticker} (id={rec_id}, signal={rec.ai_signal})")
            return rec_id

    def get_by_ticker(self, ticker: str) -> Optional[Recommendation]:
        """按股票代码查推荐"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE ticker=? AND status IN ('pending','active') ORDER BY id DESC LIMIT 1",
                (ticker.upper(),)
            ).fetchone()
            if row:
                return _row_to_rec(row)
        return None

    def get_pending(self, limit: int = 25) -> List[Recommendation]:
        """获取待入场的推荐，按评分降序"""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM recommendations
                WHERE status='pending'
                ORDER BY ai_score DESC, scan_score DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [_row_to_rec(r) for r in rows]

    def get_active(self) -> List[Recommendation]:
        """获取已买入的持仓"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE status='active' ORDER BY ai_score DESC"
            ).fetchall()
        return [_row_to_rec(r) for r in rows]

    def get_count_by_status(self, status: str) -> int:
        """统计某状态的推荐数"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM recommendations WHERE status=?", (status,)
            ).fetchone()
            return row["cnt"] if row else 0

    def update_status(self, rec_id: int, status: str, **extra):
        """更新推荐状态和可选额外字段"""
        now = datetime.now().isoformat()
        set_clauses = ["status=?"]
        params: list = [status]

        if status == "active":
            set_clauses.append("activated_at=?")
            params.append(now)
        elif status in ("executed", "closed"):
            set_clauses.append("executed_at=?")
            params.append(now)

        for key, val in extra.items():
            set_clauses.append(f"{key}=?")
            params.append(val)

        params.append(rec_id)
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE recommendations SET {', '.join(set_clauses)} WHERE id=?",
                params
            )
            logger.info(f"更新推荐 #{rec_id}: status={status} {extra}")

    def expire_old_pending(self):
        """过期处理：超过 TTL 仍未入场的 pending 推荐"""
        cutoff = (datetime.now() - timedelta(days=RECOMMENDATION_TTL_DAYS)).isoformat()
        with self._get_conn() as conn:
            result = conn.execute("""
                UPDATE recommendations SET status='expired'
                WHERE status='pending' AND added_at < ?
            """, (cutoff,))
            if result.rowcount:
                logger.info(f"过期处理了 {result.rowcount} 条推荐")

    def sweep_duplicates(self):
        """清理同一 ticker 的重复 pending 记录（保留最新）"""
        with self._get_conn() as conn:
            conn.execute("""
                DELETE FROM recommendations
                WHERE status='pending'
                AND id NOT IN (
                    SELECT MAX(id) FROM recommendations WHERE status='pending' GROUP BY ticker
                )
            """)

    # ---- 每小时检查 ----

    def log_hourly_check(self, check: HourlyCheck):
        """记录一次每小时检查"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO hourly_checks (ticker, current_price, signal, market_regime, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (check.ticker, check.current_price, check.signal,
                  check.market_regime, check.notes))

    def get_latest_check(self, ticker: str) -> Optional[HourlyCheck]:
        """获取某股票最近一次检查"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM hourly_checks WHERE ticker=? ORDER BY checked_at DESC LIMIT 1",
                (ticker.upper(),)
            ).fetchone()
            if row:
                d = dict(row)
                return HourlyCheck(
                    ticker=d["ticker"],
                    current_price=d["current_price"] or 0,
                    signal=d["signal"] or "",
                    market_regime=d["market_regime"] or "",
                    notes=d["notes"] or "",
                    checked_at=d["checked_at"],
                )
        return None

    # ---- 换仓日志 ----

    def log_rotation(self, event: RotationEvent):
        """记录一次换仓"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO rotation_log (sold_ticker, bought_ticker, sold_score, bought_score, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (event.sold_ticker, event.bought_ticker, event.sold_score,
                  event.bought_score, event.reason))
            logger.info(f"换仓: {event.sold_ticker} → {event.bought_ticker} ({event.reason})")

    def get_today_trade_count(self) -> int:
        """今日交易次数（换仓记录数）"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM rotation_log WHERE timestamp >= ?", (today,)
            ).fetchone()
            return row["cnt"] if row else 0

    def get_recommendation_by_id(self, rec_id: int) -> Optional[Recommendation]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            if row:
                return _row_to_rec(row)
        return None

    def get_summary(self) -> dict:
        """获取推荐列表摘要"""
        with self._get_conn() as conn:
            pending = conn.execute("SELECT COUNT(*) as n FROM recommendations WHERE status='pending'").fetchone()["n"]
            active = conn.execute("SELECT COUNT(*) as n FROM recommendations WHERE status='active'").fetchone()["n"]
            today = datetime.now().strftime("%Y-%m-%d")
            trades_today = conn.execute(
                "SELECT COUNT(*) as n FROM rotation_log WHERE timestamp >= ?", (today,)
            ).fetchone()["n"]
        return {
            "pending": pending,
            "active": active,
            "trades_today": trades_today,
            "db_path": str(self.db_path),
        }
