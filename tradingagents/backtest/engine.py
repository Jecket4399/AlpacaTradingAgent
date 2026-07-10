"""Walk-forward backtesting engine built on backtrader.

The engine replays a series of dated BUY/SELL/HOLD signals against
historical OHLCV bars. Orders are submitted when a bar closes and fill at
the *next* bar's open (backtrader's default, cheat-on-open disabled), so a
decision made on day t can never benefit from day t's own price — the same
information-set discipline required to avoid lookahead bias in walk-forward
evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import backtrader as bt
import pandas as pd

from .metrics import (
    CRYPTO_DAYS_PER_YEAR,
    TRADING_DAYS_PER_YEAR,
    summarize_performance,
)
from .signals import load_recorded_signals


class _SignalReplayStrategy(bt.Strategy):
    """Executes an externally supplied {date: action} map, nothing else."""

    params = (
        ("signals", None),  # dict[str iso-date, "BUY"|"SELL"|"HOLD"]
        ("allow_shorts", False),
        ("position_pct", 0.95),  # fraction of portfolio value per full position
    )

    def __init__(self):
        self.equity_dates: List[pd.Timestamp] = []
        self.equity_values: List[float] = []
        self.closed_trade_pnls: List[float] = []
        self.executed_orders: List[dict] = []
        self.rejected_orders: List[dict] = []
        # Signals sorted by date so ones falling on non-trading days (weekends,
        # halts) apply on the next available bar instead of silently dropping.
        self._pending = sorted((self.p.signals or {}).items())
        self._cursor = 0

    def _consume_signals_up_to(self, bar_date: str) -> Optional[str]:
        action = None
        while self._cursor < len(self._pending) and self._pending[self._cursor][0] <= bar_date:
            action = self._pending[self._cursor][1]
            self._cursor += 1
        return action

    def next(self):
        bar_date = self.data.datetime.date(0)
        self.equity_dates.append(pd.Timestamp(bar_date))
        self.equity_values.append(float(self.broker.getvalue()))

        action = self._consume_signals_up_to(bar_date.isoformat())
        if action == "BUY":
            self.order_target_percent(target=self.p.position_pct)
        elif action == "SELL":
            target = -self.p.position_pct if self.p.allow_shorts else 0.0
            self.order_target_percent(target=target)
        # HOLD / None: keep the current position untouched.

    def notify_order(self, order):
        if order.status == order.Completed:
            self.executed_orders.append(
                {
                    "date": self.data.datetime.date(0).isoformat(),
                    "side": "buy" if order.isbuy() else "sell",
                    "size": float(order.executed.size),
                    "price": float(order.executed.price),
                }
            )
        elif order.status in (order.Margin, order.Rejected):
            # Sizing uses the signal bar's close but fills at the next open;
            # a large overnight gap can make the order unaffordable. Surface
            # that instead of letting the trade vanish silently.
            self.rejected_orders.append(
                {
                    "date": self.data.datetime.date(0).isoformat(),
                    "side": "buy" if order.isbuy() else "sell",
                    "status": "margin" if order.status == order.Margin else "rejected",
                }
            )

    def notify_trade(self, trade):
        if trade.isclosed:
            self.closed_trade_pnls.append(float(trade.pnlcomm))


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trade_pnls: List[float]
    orders: List[dict]
    metrics: dict
    signals_used: int
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    rejected_orders: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "metrics": dict(self.metrics),
            "signals_used": self.signals_used,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "num_orders": len(self.orders),
            "rejected_orders": list(self.rejected_orders),
            "equity_curve": {
                ts.date().isoformat(): value
                for ts, value in self.equity_curve.items()
            },
        }


@dataclass
class WalkForwardResult:
    windows: List[dict] = field(default_factory=list)
    full_period: Optional[BacktestResult] = None

    def to_dict(self) -> dict:
        return {
            "windows": list(self.windows),
            "full_period": self.full_period.to_dict() if self.full_period else None,
        }


def normalize_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    """Coerce an OHLCV frame into the shape backtrader's PandasData expects.

    Accepts the ['timestamp', open, high, low, close, volume] layout produced
    by AlpacaUtils.get_stock_data (any column casing) or a frame already
    indexed by datetime. Raises ValueError on anything unusable.
    """
    if prices is None or len(prices) == 0:
        raise ValueError("No price data supplied for backtest.")

    frame = prices.copy()
    frame.columns = [str(c).lower() for c in frame.columns]

    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame = frame.set_index("timestamp")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Price data needs a 'timestamp' column or a DatetimeIndex.")

    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)

    required = {"open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Price data is missing columns: {sorted(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    frame = frame[["open", "high", "low", "close", "volume"]].astype(float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame


def run_backtest(
    prices: pd.DataFrame,
    signals: Dict[str, str],
    initial_cash: float = 100_000.0,
    commission: float = 0.001,
    allow_shorts: bool = False,
    position_pct: float = 0.95,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> BacktestResult:
    """Replay dated signals over price history and measure performance."""
    frame = normalize_price_frame(prices)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.broker.setcash(float(initial_cash))
    cerebro.broker.setcommission(commission=float(commission))
    cerebro.addstrategy(
        _SignalReplayStrategy,
        signals=dict(signals or {}),
        allow_shorts=allow_shorts,
        position_pct=position_pct,
    )
    strategy = cerebro.run()[0]

    equity_curve = pd.Series(
        strategy.equity_values, index=strategy.equity_dates, dtype=float
    )
    metrics = summarize_performance(
        equity_curve,
        strategy.closed_trade_pnls,
        periods_per_year=periods_per_year,
    )
    return BacktestResult(
        equity_curve=equity_curve,
        trade_pnls=list(strategy.closed_trade_pnls),
        orders=list(strategy.executed_orders),
        metrics=metrics,
        signals_used=len(signals or {}),
        start_date=frame.index[0].date().isoformat(),
        end_date=frame.index[-1].date().isoformat(),
        rejected_orders=list(strategy.rejected_orders),
    )


def run_walk_forward(
    prices: pd.DataFrame,
    signals: Dict[str, str],
    window_bars: int = 63,
    min_window_bars: int = 5,
    **backtest_kwargs,
) -> WalkForwardResult:
    """Evaluate signals over consecutive non-overlapping out-of-sample windows.

    LLM decision replay has no parameters to re-fit between folds, so the
    walk-forward's purpose here is robustness: instead of one full-period
    number, each ~quarterly window (63 trading bars by default) restarts with
    fresh capital and reports its own metrics, exposing regime dependence a
    single lucky backtest would hide. A trailing window shorter than
    `min_window_bars` is folded into its predecessor.
    """
    frame = normalize_price_frame(prices)
    if window_bars < 2:
        raise ValueError("window_bars must be at least 2.")

    boundaries = list(range(0, len(frame), window_bars))
    windows: List[dict] = []
    for start in boundaries:
        end = start + window_bars
        # Absorb a too-short trailing remainder into the final window.
        if len(frame) - end < min_window_bars:
            end = len(frame)
        chunk = frame.iloc[start:end]
        if len(chunk) < 2:
            break
        result = run_backtest(chunk, signals, **backtest_kwargs)
        windows.append(
            {
                "start_date": result.start_date,
                "end_date": result.end_date,
                "bars": len(chunk),
                "metrics": result.metrics,
            }
        )
        if end == len(frame):
            break

    full = run_backtest(frame, signals, **backtest_kwargs)
    return WalkForwardResult(windows=windows, full_period=full)


def run_recorded_backtest(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    eval_results_dir: str = "eval_results",
    price_loader: Optional[Callable[[str, str, Optional[str]], pd.DataFrame]] = None,
    **backtest_kwargs,
) -> BacktestResult:
    """Backtest the signals this deployment has already produced for `symbol`.

    Pulls decisions from the persisted run logs (zero LLM cost) and prices
    from Alpaca (with its existing yfinance fallback). `price_loader` exists
    for tests and alternative data sources.
    """
    signals = load_recorded_signals(symbol, eval_results_dir=eval_results_dir)
    if not signals:
        raise ValueError(
            f"No recorded completed runs with final signals found for {symbol} "
            f"under {eval_results_dir}/."
        )

    first_signal = min(signals)
    start = start_date or first_signal
    if start > first_signal:
        # Signals before the price window would misleadingly fire on its
        # first bar; drop them so the backtest matches the requested range.
        signals = {d: a for d, a in signals.items() if d >= start}
        if not signals:
            raise ValueError(
                f"All recorded signals for {symbol} predate start_date={start}."
            )

    if price_loader is None:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        price_loader = AlpacaUtils.get_stock_data

    prices = price_loader(symbol, start, end_date)
    backtest_kwargs.setdefault(
        "periods_per_year",
        CRYPTO_DAYS_PER_YEAR if "/" in symbol else TRADING_DAYS_PER_YEAR,
    )
    return run_backtest(prices, signals, **backtest_kwargs)


def run_recorded_walk_forward(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    eval_results_dir: str = "eval_results",
    price_loader: Optional[Callable[[str, str, Optional[str]], pd.DataFrame]] = None,
    window_bars: int = 63,
    **backtest_kwargs,
) -> WalkForwardResult:
    """Walk-forward evaluation of this deployment's recorded decisions."""
    signals = load_recorded_signals(symbol, eval_results_dir=eval_results_dir)
    if not signals:
        raise ValueError(
            f"No recorded completed runs with final signals found for {symbol} "
            f"under {eval_results_dir}/."
        )

    start = start_date or min(signals)
    signals = {d: a for d, a in signals.items() if d >= start}
    if not signals:
        raise ValueError(
            f"All recorded signals for {symbol} predate start_date={start}."
        )

    if price_loader is None:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        price_loader = AlpacaUtils.get_stock_data

    prices = price_loader(symbol, start, end_date)
    backtest_kwargs.setdefault(
        "periods_per_year",
        CRYPTO_DAYS_PER_YEAR if "/" in symbol else TRADING_DAYS_PER_YEAR,
    )
    return run_walk_forward(prices, signals, window_bars=window_bars, **backtest_kwargs)
