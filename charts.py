"""Render candlestick charts and annotate H1 with trade levels."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from matplotlib.patches import Rectangle

import config
import research
from models import Suggestion

FIGSIZE = (12, 8)
ANNOTATED_FIGSIZE = (16, 8)
DPI = 120
FONT_SIZE = 11
RATIONALE_WRAP_WIDTH = 38

_STYLE = mpf.make_mpf_style(
  base_mpf_style="charles",
  gridstyle=":",
  y_on_right=False,
  rc={
    "font.size": FONT_SIZE,
    "axes.titlesize": FONT_SIZE + 2,
    "axes.labelsize": FONT_SIZE,
  },
)


def _ensure_charts_dir() -> Path:
  config.CHARTS_DIR.mkdir(parents=True, exist_ok=True)
  return config.CHARTS_DIR


def _to_mpf_df(bars: list[dict]) -> pd.DataFrame:
  df = research.to_dataframe(bars)
  return df.rename(
    columns={
      "open": "Open",
      "high": "High",
      "low": "Low",
      "close": "Close",
      "volume": "Volume",
    }
  )


def _swing_levels(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
  """Recent swing high/low for light HTF reference lines."""
  window = df.tail(lookback)
  return float(window["High"].max()), float(window["Low"].min())


def render_charts(
  data: dict[str, list[dict]],
  cycle_id: str | None = None,
) -> dict[str, str]:
  """Render clean candlestick PNGs per timeframe. Returns {tf: path}."""
  out_dir = _ensure_charts_dir()
  cycle_id = cycle_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  paths: dict[str, str] = {}

  for tf in ("W1", "D1", "H4", "H1"):
    bars = data.get(tf)
    if not bars:
      raise ValueError(f"Missing OHLC data for {tf}")

    df = _to_mpf_df(bars)
    swing_high, swing_low = _swing_levels(df)
    path = out_dir / f"{cycle_id}_{tf}.png"

    hlines = dict(
      hlines=[swing_high, swing_low],
      colors=["#888888", "#888888"],
      linestyle="--",
      linewidths=0.8,
      alpha=0.6,
    )

    mpf.plot(
      df,
      type="candle",
      style=_STYLE,
      volume=True,
      title=f"ETH-USD {tf}",
      figsize=FIGSIZE,
      savefig=dict(fname=str(path), dpi=DPI, bbox_inches="tight"),
      hlines=hlines,
    )
    plt.close("all")
    paths[tf] = str(path)

  return paths


def _nearest_index(df: pd.DataFrame, ts: str) -> pd.Timestamp:
  target = pd.Timestamp(ts)
  if target.tzinfo is None:
    target = target.tz_localize("UTC")
  idx = df.index.get_indexer([target], method="nearest")[0]
  return df.index[idx]


def _wrap_caption(header: str, body: str, width: int = RATIONALE_WRAP_WIDTH) -> str:
  """Word-wrap rationale for the side panel."""
  wrapped = textwrap.fill(body.strip(), width=width, break_long_words=False, break_on_hyphens=False)
  return f"{header}\n\n{wrapped}"


def _build_annotated_figure(df: pd.DataFrame, title: str) -> tuple:
  """Chart on the left, empty rationale panel on the right."""
  fig = plt.figure(figsize=ANNOTATED_FIGSIZE)
  gs = fig.add_gridspec(
    2,
    2,
    width_ratios=[2.8, 1],
    height_ratios=[4, 1],
    wspace=0.06,
    hspace=0.08,
  )
  ax_price = fig.add_subplot(gs[0, 0])
  ax_vol = fig.add_subplot(gs[1, 0], sharex=ax_price)
  ax_text = fig.add_subplot(gs[:, 1])

  mpf.plot(
    df,
    type="candle",
    style=_STYLE,
    ax=ax_price,
    volume=ax_vol,
    warn_too_much_data=1000,
  )
  ax_price.set_title(title, fontsize=FONT_SIZE + 2, fontweight="bold")
  ax_text.axis("off")
  ax_text.set_facecolor("#f7f7f7")
  return fig, ax_price, ax_text


def _draw_rationale_panel(ax_text, header: str, rationale: str) -> None:
  """Render rationale in the panel beside the chart (not on top of candles)."""
  text = _wrap_caption(header, rationale)
  ax_text.text(
    0.04,
    0.98,
    text,
    transform=ax_text.transAxes,
    fontsize=FONT_SIZE,
    color="#111111",
    va="top",
    ha="left",
    linespacing=1.4,
    bbox=dict(boxstyle="round,pad=0.6", facecolor="#f7f7f7", edgecolor="#cccccc"),
  )


def _draw_price_line(ax, price: float, label: str, color: str, linestyle: str) -> None:
  ax.axhline(price, color=color, linestyle=linestyle, linewidth=1.8, alpha=0.95)
  xlim = ax.get_xlim()
  ax.text(
    xlim[1],
    price,
    f"  {label} {price:,.2f}",
    color=color,
    fontsize=FONT_SIZE,
    fontweight="bold",
    va="center",
    ha="left",
    clip_on=False,
  )


def annotate_chart(
  h1_path: str,
  suggestion: Suggestion,
  cycle_id: str,
  h1_bars: list[dict] | None = None,
) -> str:
  """
  Draw trade markup on the H1 chart; rationale sits in a panel beside the chart.
  Re-plots from h1_bars for correct price alignment (h1_path used for naming only).
  """
  out_dir = _ensure_charts_dir()
  annotated_path = out_dir / f"{cycle_id}_H1_annotated.png"

  if h1_bars is None:
    h1_bars = research.get_ohlc("H1")
  df = _to_mpf_df(h1_bars)

  title = "ETH-USD H1 — Trade Idea" if suggestion.action != "no_trade" else "ETH-USD H1 — No Trade"
  fig, ax, ax_text = _build_annotated_figure(df, title)

  if suggestion.action == "no_trade":
    _draw_rationale_panel(ax_text, "NO TRADE", suggestion.rationale)
    fig.savefig(annotated_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return str(annotated_path)

  # --- trade markup on chart only ---
  # Order block zone
  if suggestion.order_block:
    ob = suggestion.order_block
    x0 = _nearest_index(df, ob["start_ts"])
    x1 = _nearest_index(df, ob["end_ts"])
    if x0 > x1:
      x0, x1 = x1, x0
    x0_num = mdates.date2num(x0)
    x1_num = mdates.date2num(x1)
    width = max(x1_num - x0_num, 0.02)
    rect = Rectangle(
      (x0_num, float(ob["low"])),
      width,
      float(ob["high"]) - float(ob["low"]),
      facecolor="#FFD700",
      edgecolor="#B8860B",
      alpha=0.35,
      linewidth=1.5,
      zorder=2,
    )
    ax.add_patch(rect)
    ax.text(
      x0_num,
      float(ob["high"]),
      " OB",
      color="#B8860B",
      fontsize=FONT_SIZE,
      fontweight="bold",
      va="bottom",
    )

  if suggestion.entry is not None:
    _draw_price_line(ax, suggestion.entry, "Entry", "#00AA00", "--")
  if suggestion.stop_loss is not None:
    _draw_price_line(ax, suggestion.stop_loss, "SL", "#CC0000", "-")
  for i, tp in enumerate(suggestion.take_profits[:3], start=1):
    _draw_price_line(ax, tp, f"TP{i}", "#0066CC", ":")

  rr = f"{suggestion.risk_reward:.2f}" if suggestion.risk_reward is not None else "n/a"
  header = f"{suggestion.action.upper()}  |  R/R {rr}"
  _draw_rationale_panel(ax_text, header, suggestion.rationale)

  fig.savefig(annotated_path, dpi=DPI, bbox_inches="tight")
  plt.close(fig)
  return str(annotated_path)


def _fake_suggestion(h1_bars: list[dict]) -> Suggestion:
  """Build a plausible fake long setup from recent H1 structure."""
  df = research.to_dataframe(h1_bars)
  recent = df.tail(20)
  ob_low = float(recent["low"].min())
  ob_high = ob_low + (float(recent["high"].max()) - ob_low) * 0.4
  entry = ob_high
  stop_loss = ob_low * 0.9975
  range_size = entry - stop_loss
  take_profits = [
    entry + range_size * 1.5,
    entry + range_size * 2.5,
    entry + range_size * 3.5,
  ]
  start_ts = recent.index[5].strftime("%Y-%m-%dT%H:%M:%SZ")
  end_ts = recent.index[-2].strftime("%Y-%m-%dT%H:%M:%SZ")

  return Suggestion(
    action="spot_buy",
    size=0.5,
    entry=round(entry, 2),
    stop_loss=round(stop_loss, 2),
    take_profits=[round(tp, 2) for tp in take_profits],
    risk_reward=2.1,
    rationale="Fake H1 bullish OB retest in discount — markup test",
    order_block={
      "low": round(ob_low, 2),
      "high": round(ob_high, 2),
      "start_ts": start_ts,
      "end_ts": end_ts,
    },
  )


if __name__ == "__main__":
  cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  print(f"Fetching live OHLC...")
  data = research.get_all_timeframes()

  print(f"Rendering charts...")
  paths = render_charts(data, cycle_id=cycle_id)
  for tf, path in paths.items():
    print(f"  {tf}: {path}")

  fake = _fake_suggestion(data["H1"])
  print(f"\nFake suggestion: {fake.action} entry={fake.entry} sl={fake.stop_loss} tps={fake.take_profits}")

  annotated = annotate_chart(paths["H1"], fake, cycle_id, h1_bars=data["H1"])
  print(f"\nAnnotated H1 chart: {annotated}")
