"""Render candlestick charts and annotate H1 with trade levels."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.transforms import blended_transform_factory
import mplfinance as mpf
import pandas as pd
from matplotlib.patches import Rectangle

import config
import research
from models import Suggestion
from patterns.market_context import MarketContext

FIGSIZE = (12, 8)
ANNOTATED_FIGSIZE = (16, 8)
DPI = 120
FONT_SIZE = 11
RATIONALE_WRAP_WIDTH = 38
# Telegram rejects extreme PNG dimensions; keep saved charts within these bounds.
TELEGRAM_MAX_CHART_WIDTH = 4096
TELEGRAM_MAX_CHART_HEIGHT = 4096

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

  for tf in research.STRATEGY_TIMEFRAMES:
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
  """Draw a horizontal level with a label pinned inside the chart (Telegram-safe)."""
  ax.axhline(price, color=color, linestyle=linestyle, linewidth=1.8, alpha=0.95)
  transform = blended_transform_factory(ax.transAxes, ax.transData)
  ax.text(
    0.99,
    price,
    f" {label} {price:,.2f}",
    color=color,
    fontsize=FONT_SIZE,
    fontweight="bold",
    va="center",
    ha="right",
    transform=transform,
    clip_on=True,
  )


def _save_figure(fig, path: Path) -> str:
  """Save PNG and clamp dimensions so Telegram accepts the photo."""
  path.parent.mkdir(parents=True, exist_ok=True)
  fig.savefig(path, dpi=DPI, bbox_inches="tight", pad_inches=0.15)
  plt.close(fig)
  _ensure_telegram_safe_image(path)
  return str(path)


def _ensure_telegram_safe_image(path: Path) -> None:
  """Downscale charts that exceed Telegram or sanity pixel limits."""
  try:
    from PIL import Image
  except ImportError:
    return

  with Image.open(path) as im:
    width, height = im.size
    too_large = (
      width > TELEGRAM_MAX_CHART_WIDTH
      or height > TELEGRAM_MAX_CHART_HEIGHT
      or width * height > 25_000_000
    )
    if not too_large:
      return
    resized = im.copy()
    resized.thumbnail(
      (TELEGRAM_MAX_CHART_WIDTH, TELEGRAM_MAX_CHART_HEIGHT),
      Image.Resampling.LANCZOS,
    )
    resized.save(path, optimize=True)


def _draw_detected_overlays(
  ax,
  df: pd.DataFrame,
  market_context: MarketContext | None,
) -> None:
  """Draw programmatic 24h range and order blocks on the H1 chart."""
  if market_context is None:
    return

  if market_context.range_24h:
    r = market_context.range_24h
    _draw_price_line(ax, r.high, "24h High", "#7B68EE", ":")
    _draw_price_line(ax, r.low, "24h Low", "#7B68EE", ":")

  for ob in market_context.order_blocks[-3:]:
    try:
      x0 = _nearest_index(df, ob.start_ts)
      x1 = _nearest_index(df, ob.end_ts)
      if x0 > x1:
        x0, x1 = x1, x0
      x0_num = mdates.date2num(x0)
      width = max(mdates.date2num(x1) - x0_num, 0.02)
      color = "#90EE90" if ob.direction == "bullish" else "#FFB6C1"
      edge = "#228B22" if ob.direction == "bullish" else "#CD5C5C"
      rect = Rectangle(
        (x0_num, float(ob.low)),
        width,
        float(ob.high) - float(ob.low),
        facecolor=color,
        edgecolor=edge,
        alpha=0.25,
        linewidth=1.0,
        zorder=1,
      )
      ax.add_patch(rect)
    except (KeyError, ValueError, IndexError):
      continue


def annotate_chart(
  h1_path: str,
  suggestion: Suggestion,
  cycle_id: str,
  h1_bars: list[dict] | None = None,
  market_context: MarketContext | None = None,
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

  _draw_detected_overlays(ax, df, market_context)

  if suggestion.action == "no_trade":
    _draw_rationale_panel(ax_text, "NO TRADE", suggestion.rationale)
    return _save_figure(fig, annotated_path)

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

  return _save_figure(fig, annotated_path)


def render_research_chart(
  bars: list[dict],
  events: list,
  stats: dict,
  timeframe: str = "W1",
  cycle_id: str | None = None,
  years: int = 4,
) -> str:
  """
  Render a single-TF research chart with SFP markers and stats panel.
  `events` should be patterns.sfp.SFPEvent instances.
  """
  from patterns.sfp import SFPEvent

  out_dir = _ensure_charts_dir()
  cycle_id = cycle_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  path = out_dir / f"{cycle_id}_research_{timeframe}.png"

  df = _to_mpf_df(bars)
  if df.empty:
    raise ValueError("No bars to render")

  date_start = df.index[0].strftime("%Y-%m")
  date_end = df.index[-1].strftime("%Y-%m")
  title = f"ETH-USD {timeframe} — SFP Study ({years}y)"
  fig, ax_price, ax_text = _build_annotated_figure(df, title)

  outcome_colors = {
    "reversal": "#00AA00",
    "invalidation": "#CC0000",
    "neutral": "#888888",
    "pending": "#AAAAAA",
  }

  # mplfinance uses integer bar indices on custom axes, not matplotlib dates.
  for event in events:
    if not isinstance(event, SFPEvent):
      continue
    bar_idx = event.bar_idx
    if bar_idx < 0 or bar_idx >= len(df):
      continue
    x = float(bar_idx)
    color = outcome_colors.get(event.outcome_a, "#888888")
    marker = "v" if event.direction == "bearish" else "^"
    row = df.iloc[bar_idx]
    y = float(row["High"]) if event.direction == "bearish" else float(row["Low"])
    offset = 1.02 if event.direction == "bearish" else 0.98
    ax_price.scatter(
      [x],
      [y * offset],
      marker=marker,
      s=120,
      c=color,
      edgecolors="black",
      linewidths=0.5,
      zorder=5,
    )
    tick_len = 0.45
    ax_price.hlines(
      event.swept_level,
      x - tick_len,
      x + tick_len,
      colors=color,
      linewidth=1.2,
      alpha=0.8,
      zorder=4,
    )

  reversal_pct = stats.get("reversal_pct", 0)
  total = stats.get("total_sfps", 0)
  rev = stats.get("reversals", 0)
  inv = stats.get("invalidations", 0)
  neu = stats.get("neutral", 0)
  pend = stats.get("pending", 0)
  b_pct = stats.get("outcome_b_pct", 0)
  c_pct = stats.get("outcome_c_pct", 0)

  tf_label = "W-FRI weekly" if timeframe == "W1" else f"{timeframe} Coinbase"
  panel = (
    f"{timeframe} SFP Results\n\n"
    f"Period: {date_start} to {date_end}\n"
    f"Coinbase ETH-USD ({tf_label})\n\n"
    f"Headline (Outcome A):\n"
    f"  {reversal_pct}% reversal\n"
    f"  ({rev} rev / {inv} inv)\n"
    f"  n={total} SFPs scored\n\n"
    f"Also logged:\n"
    f"  Neutral: {neu}\n"
    f"  Pending: {pend}\n"
    f"  Outcome B (>=5% move): {b_pct}%\n"
    f"  Outcome C (structure break): {c_pct}%\n\n"
    f"Green=reversal  Red=invalidation\n"
    f"Gray=neutral/pending"
  )
  _draw_rationale_panel(ax_text, "Research", panel)

  return _save_figure(fig, path)


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
