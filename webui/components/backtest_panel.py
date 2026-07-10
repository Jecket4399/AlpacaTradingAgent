"""
webui/components/backtest_panel.py - Walk-forward backtest dashboard panel

Replays the decisions already recorded under eval_results/ against
historical prices (zero LLM cost) and reports the TradingAgents-paper
metric set: cumulative/annualized return, Sharpe ratio, max drawdown,
plus win rate, per walk-forward window and for the full period.
"""

import dash_bootstrap_components as dbc
from dash import dcc, html


def create_backtest_panel():
    """Create the backtest panel card for the web UI."""
    controls = dbc.Row(
        [
            dbc.Col(
                [
                    dbc.Label("Symbol", html_for="backtest-symbol-input", className="small"),
                    dbc.Input(
                        id="backtest-symbol-input",
                        type="text",
                        placeholder="e.g. AAPL or BTC/USD",
                        debounce=True,
                    ),
                ],
                md=3,
            ),
            dbc.Col(
                [
                    dbc.Label("Start date (optional)", html_for="backtest-start-date", className="small"),
                    dbc.Input(id="backtest-start-date", type="date"),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label("End date (optional)", html_for="backtest-end-date", className="small"),
                    dbc.Input(id="backtest-end-date", type="date"),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label("Window (bars)", html_for="backtest-window-bars", className="small"),
                    dbc.Input(
                        id="backtest-window-bars",
                        type="number",
                        value=63,
                        min=2,
                        step=1,
                    ),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label(" ", className="small d-block"),
                    dbc.Switch(
                        id="backtest-allow-shorts",
                        label="Allow shorts",
                        value=False,
                    ),
                ],
                md=1,
            ),
            dbc.Col(
                [
                    dbc.Label(" ", className="small d-block"),
                    dbc.Button(
                        [html.I(className="fas fa-flask me-2"), "Run Backtest"],
                        id="backtest-run-btn",
                        color="primary",
                        className="w-100",
                    ),
                ],
                md=2,
            ),
        ],
        className="g-2 align-items-end mb-3",
    )

    return dbc.Card(
        dbc.CardBody(
            [
                html.H4("Walk-Forward Backtest", className="mb-1"),
                html.Div(
                    "Replays this deployment's recorded agent decisions on historical "
                    "prices — signals execute at the next bar's open (no lookahead).",
                    className="text-muted small mb-3",
                ),
                controls,
                dcc.Loading(
                    id="backtest-loading",
                    type="default",
                    children=html.Div(
                        [
                            html.Div(id="backtest-status", className="mb-2"),
                            html.Div(id="backtest-metrics", className="mb-3"),
                            html.Div(
                                dcc.Graph(
                                    id="backtest-equity-graph",
                                    config={"displayModeBar": False, "responsive": True},
                                    style={"height": "320px", "width": "100%"},
                                ),
                                id="backtest-graph-container",
                                style={"display": "none"},
                            ),
                            html.Div(id="backtest-windows-table"),
                        ]
                    ),
                ),
            ]
        ),
        className="mb-4",
    )
