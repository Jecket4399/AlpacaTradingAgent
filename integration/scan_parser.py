"""解析 stock-screener 的扫描输出文件"""

import re
import logging
from typing import List, Optional
from .models import ScanResult

logger = logging.getLogger(__name__)


def parse_scan_file(content: str) -> List[ScanResult]:
    """从 latest_optimized_scan.txt 文本中提取 Top N 买入信号

    解析格式：
        BUY #1: SUN | Score: 112.0/125
        Phase: 2
        Stop Loss: $67.45
        Risk/Reward: 3.0:1
        RS: 0.676
        Key Reasons:
          • Strong Stage 2...
    """
    results: List[ScanResult] = []
    lines = content.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 匹配买入信号标题行
        m = re.match(r".*BUY\s+#(\d+):\s+(\w+)\s*\|\s*Score:\s*([\d.]+)/125", line)
        if not m:
            i += 1
            continue

        rank = int(m.group(1))
        ticker = m.group(2)
        score = float(m.group(3))

        # 读后续行获取详细信息
        phase = 0
        stop_loss: Optional[float] = None
        rr_ratio: Optional[float] = None
        rs_slope: Optional[float] = None
        entry_quality: Optional[str] = None
        reasons: List[str] = []

        for j in range(1, 20):  # 最多往下读20行
            if i + j >= len(lines):
                break
            next_line = lines[i + j].strip()

            # 遇到下一个买入信号或分隔线就停止
            if re.match(r".*(BUY|SELL)\s+#\d+:", next_line):
                break
            if next_line.startswith("==="):
                break

            m_phase = re.match(r"Phase:\s*(\d+)", next_line)
            if m_phase:
                phase = int(m_phase.group(1))

            m_stop = re.match(r"Stop Loss:\s*\$?([\d.]+)", next_line)
            if m_stop:
                stop_loss = float(m_stop.group(1))

            m_rr = re.match(r".*Risk/Reward:\s*([\d.]+):1", next_line)
            if m_rr:
                rr_ratio = float(m_rr.group(1))

            m_rs = re.match(r".*RS:\s*([\d.-]+)", next_line)
            if m_rs:
                rs_slope = float(m_rs.group(1))

            m_entry = re.match(r".*Entry Quality:\s*(\w+)", next_line)
            if m_entry:
                entry_quality = m_entry.group(1)

            m_reason = re.match(r"\s*[•o]\s+(.+)", next_line)
            if m_reason:
                reasons.append(m_reason.group(1).strip())

        results.append(ScanResult(
            ticker=ticker,
            rank=rank,
            score=score,
            phase=phase,
            stop_loss=stop_loss,
            rr_ratio=rr_ratio,
            rs_slope=rs_slope,
            entry_quality=entry_quality,
            reasons=reasons,
        ))
        i += 1

    logger.info(f"从扫描文件解析到 {len(results)} 只买入信号股票")
    return results


def parse_top_n(content: str, n: int = 25) -> List[ScanResult]:
    """解析并返回 Top N"""
    all_results = parse_scan_file(content)
    return all_results[:n]


def fetch_and_parse(url: str, top_n: int = 25) -> List[ScanResult]:
    """从 URL 拉取最新扫描结果并解析"""
    import urllib.request

    logger.info(f"拉取扫描结果: {url}")
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "integration-pipeline/1.0")
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        logger.error(f"拉取扫描结果失败: {e}")
        return []

    return parse_top_n(content, top_n)
