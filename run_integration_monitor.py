#!/usr/bin/env python3
"""每小时仓位监控入口

用法:
    python run_integration_monitor.py                      # 单次检查
    python run_integration_monitor.py --loop               # 持续循环（每60分钟）
    python run_integration_monitor.py --interval 30        # 自定义间隔（分钟）
    python run_integration_monitor.py --once --dry-run     # 仅查看不执行
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from integration.pipeline import IntegrationPipeline
from integration.config import MONITOR_INTERVAL_MINUTES


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path.home() / ".tradingagents" / "monitor.log"),
        ],
    )


def is_market_hours() -> bool:
    now = datetime.utcnow()
    if now.weekday() >= 5:
        return False
    hour_est = (now.hour - 4) % 24
    return 9 <= hour_est <= 16


def main():
    parser = argparse.ArgumentParser(description="每小时仓位监控")
    parser.add_argument("--loop", action="store_true", help="持续循环模式")
    parser.add_argument("--interval", type=int, default=MONITOR_INTERVAL_MINUTES,
                        help="检查间隔（分钟）")
    parser.add_argument("--once", action="store_true", help="仅运行一次")
    parser.add_argument("--dry-run", action="store_true", help="仅查看，不交易")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("monitor")

    pipeline = IntegrationPipeline()

    if args.dry_run:
        logger.info("--- 干运行模式 ---")
        from integration.recommendation_store import RecommendationStore
        store = RecommendationStore()
        summary = store.get_summary()
        pending = store.get_pending(10)
        active = store.get_active()
        logger.info(f"推荐列表: pending={summary['pending']}, active={summary['active']}")
        if pending:
            logger.info("待入场 Top 10:")
            for r in pending:
                logger.info(f"  {r.ticker}: score={r.ai_score:.0f}, signal={r.ai_signal}, "
                            f"confidence={r.ai_confidence}")
        if active:
            logger.info(f"当前持仓 ({len(active)} 只):")
            for r in active:
                logger.info(f"  {r.ticker}: active since {r.activated_at}")
        return

    def run_once():
        if not is_market_hours():
            logger.info("非交易时段")
            return
        try:
            stats = pipeline.hourly_monitor()
            logger.info(f"检查完成: 下单={stats['orders_placed']}, "
                        f"换仓={stats['rotations']}, 检查={stats['checks_performed']}")
        except Exception as e:
            logger.error(f"监控出错: {e}", exc_info=True)

    if args.loop:
        logger.info(f"启动持续监控（间隔 {args.interval} 分钟）")
        while True:
            run_once()
            time.sleep(args.interval * 60)
    else:
        run_once()


if __name__ == "__main__":
    main()
