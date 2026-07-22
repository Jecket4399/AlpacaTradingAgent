#!/usr/bin/env python3
"""每日同步：从 daily_stock_analysis 结果中提取 BUY 推荐 → 放入监控列表

用法:
    python run_integration_daily.py                        # 从 daily_stock_analysis 拉取
    python run_integration_daily.py --dry-run              # 仅查看，不写入
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from integration.pipeline import IntegrationPipeline


def main():
    import io, sys
    # 修复 Windows GBK 编码问题
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description="从 daily_stock_analysis 分析结果同步 BUY 推荐"
    )
    parser.add_argument("--source-url", type=str,
                        help="daily_stock_analysis 报告 URL 或本地文件路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅查看会添加哪些推荐，不实际写入")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    pipeline = IntegrationPipeline()

    if args.dry_run:
        from integration.pipeline import DSA_JSON_URL
        url = args.source_url or DSA_JSON_URL
        signals = pipeline._fetch_dsa_results(url)
        if not signals:
            print("⚠️  未获取到 daily_stock_analysis 分析结果")
            print("   提示：确保 daily_stock_analysis 最近一次运行已完成")
            return

        buy_signals = [s for s in signals if s.get("action") == "BUY"]
        print(f"\n从 {len(signals)} 只股票中筛选出 {len(buy_signals)} 只 BUY:\n")
        for s in buy_signals:
            print(f"  {s['ticker']:6s}  评分={s['score']:3d}  置信度={s['confidence']}")
        if not buy_signals:
            print("  (无 BUY 推荐)")
        return

    stats = pipeline.daily_sync(source_url=args.source_url)
    print(f"\n同步完成: 发现 {stats['found']} 只, BUY {stats['buy']} 只, "
          f"新增推荐 {stats['added']} 只")

    summary = pipeline.store.get_summary()
    print(f"推荐列表: {summary['pending']} 只待入场, {summary['active']} 只持仓中")


if __name__ == "__main__":
    main()
