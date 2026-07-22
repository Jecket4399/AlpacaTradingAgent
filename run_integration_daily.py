#!/usr/bin/env python3
"""每日批量分析入口

用法:
    python run_integration_daily.py                    # 使用默认配置
    python run_integration_daily.py --top 20           # 只分析 Top 20
    python run_integration_daily.py --scan-url <URL>   # 指定扫描文件地址
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from integration.pipeline import IntegrationPipeline
from integration.config import TOP_N_CANDIDATES, SCAN_OUTPUT_URL


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path.home() / ".tradingagents" / "daily_pipeline.log"),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="每日全市场筛选 + AI 分析")
    parser.add_argument("--top", type=int, default=TOP_N_CANDIDATES, help="分析 Top N 只")
    parser.add_argument("--scan-url", type=str, default=SCAN_OUTPUT_URL,
                        help="stock-screener 扫描结果 URL")
    parser.add_argument("--llm", type=str, default="deepseek", help="LLM 提供商")
    parser.add_argument("--dry-run", action="store_true", help="仅打印会分析的股票，不真正执行")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("daily_pipeline")

    if args.dry_run:
        from integration.scan_parser import fetch_and_parse
        results = fetch_and_parse(args.scan_url, args.top)
        print(f"\n会分析以下 {len(results)} 只股票:\n")
        for sr in results:
            print(f"  #{sr.rank:2d}  {sr.ticker:6s}  Score={sr.score:.0f}/125  "
                  f"RR={sr.rr_ratio or '?'}:1  RS={sr.rs_slope or '?'}")
        return

    logger.info("开始每日批量分析...")
    pipeline = IntegrationPipeline(config={"llm_provider": args.llm})
    stats = pipeline.daily_batch(scan_url=args.scan_url, top_n=args.top)

    logger.info(f"完成: 分析={stats['analyzed']}, 新增={stats['added']}, "
                f"跳过={stats['skipped']}, 错误={stats['errors']}")

    summary = pipeline.store.get_summary()
    logger.info(f"当前推荐列表: pending={summary['pending']}, active={summary['active']}")


if __name__ == "__main__":
    main()
