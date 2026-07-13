"""Render candlestick charts and annotate M5 with trade levels."""

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
from patterns.htf_structure import HTFZone
from patterns.key_levels import KeyLevel
from patterns.market_context import MarketContext
from patterns.order_block import fib_zone_bounds

FIGSIZE = (24, 10)
OUTPUT_FIGSIZE = (24, 10)
ANNOTATED_FIGSIZE = (24, 8)
DPI = 144
FONT_SIZE = 12
RATIONALE_WRAP_WIDTH = 56
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


def _visible_key_levels(levels: list[KeyLevel], df: pd.DataFrame, padding_pct: float = 0.12) -> list[KeyLevel]:
  """Key levels near the visible candle range (avoid cluttering far HTF lines)."""
  if df.empty or not levels:
    return levels
  lo = float(df["Low"].min())
  hi = float(df["High"].max())
  pad = max((hi - lo) * padding_pct, hi * 0.02)
  return [lv for lv in levels if lo - pad <= lv.price <= hi + pad]


def _render_candlestick_figure(
  df: pd.DataFrame,
  title: str,
  figsize: tuple[float, float] = FIGSIZE,
) -> tuple:
  """Candlestick + volume axes for overlay drawing."""
  fig, axes = mpf.plot(
    df,
    type="candle",
    style=_STYLE,
    volume=True,
    title=title,
    figsize=figsize,
    returnfig=True,
    warn_too_much_data=1000,
  )
  ax_price = axes[0]
  return fig, ax_price


def _draw_swing_hlines(ax, df: pd.DataFrame) -> None:
  swing_high, swing_low = _swing_levels(df)
  ax.axhline(swing_high, color="#888888", linestyle="--", linewidth=0.8, alpha=0.6)
  ax.axhline(swing_low, color="#888888", linestyle="--", linewidth=0.8, alpha=0.6)


_LIGHT_LEVEL_COLORS = frozenset({"#fffcbc", "#ffffff", "#D4AF37", "#E8E8E8"})


def _bar_index(df: pd.DataFrame, ts: str) -> int:
  """Nearest mplfinance bar index for a UTC timestamp."""
  target = pd.Timestamp(ts)
  if target.tzinfo is None:
    target = target.tz_localize("UTC")
  idx = int(df.index.get_indexer([target], method="nearest")[0])
  return max(0, min(idx, len(df) - 1))


def _bar_x_range(df: pd.DataFrame, start_ts: str, end_ts: str | None) -> tuple[float, float]:
  """Left/right bar indices for a zone (mplfinance uses integer x-axis)."""
  x0 = float(_bar_index(df, start_ts))
  x1 = float(_bar_index(df, end_ts)) if end_ts else float(len(df) - 1)
  if x0 > x1:
    x0, x1 = x1, x0
  return x0, max(x1, x0 + 0.8)


def _level_label_style(line_color: str) -> dict:
  """High-contrast label styling for light SpacemanBTC line colors."""
  color_lower = line_color.lower()
  if color_lower in _LIGHT_LEVEL_COLORS or color_lower == "#ffffff":
    return {
      "color": "#1a1a1a",
      "bbox": dict(
        boxstyle="round,pad=0.25",
        facecolor=line_color,
        edgecolor="#888888",
        alpha=0.92,
      ),
    }
  return {"color": line_color, "bbox": None}


def _plan_key_level_labels(
  levels: list[KeyLevel],
  y_lo: float,
  y_hi: float,
  *,
  min_gap_frac: float = 0.028,
  max_nudge_frac: float = 0.08,
) -> list[tuple[KeyLevel, float, str]]:
  """
  Assign label y-offsets and left/right sides so nearby levels do not overlap.

  Returns (level, label_y, side) tuples. Horizontal lines stay at true price;
  labels may shift slightly vertically when many levels cluster together.
  """
  if not levels:
    return []

  span = max(y_hi - y_lo, y_hi * 0.01, 1.0)
  min_gap = span * min_gap_frac
  max_nudge = span * max_nudge_frac
  cluster_gap = span * 0.10

  def _fits(y: float, placed: list[float]) -> bool:
    return all(abs(y - py) >= min_gap for py in placed)

  def _nudge_toward_free(y: float, placed: list[float]) -> float:
    if not placed or _fits(y, placed):
      return y
    best = y
    best_penalty = float("inf")
    for step in range(1, 8):
      for candidate in (y - step * min_gap, y + step * min_gap):
        if abs(candidate - y) > max_nudge:
          continue
        penalty = abs(candidate - y) + sum(
          max(0.0, min_gap - abs(candidate - py)) for py in placed
        )
        if penalty < best_penalty and _fits(candidate, placed):
          best_penalty = penalty
          best = candidate
    if best_penalty < float("inf"):
      return best
    return y - min_gap * len(placed)

  right_ys: list[float] = []
  left_ys: list[float] = []
  planned: list[tuple[KeyLevel, float, str]] = []

  for lv in sorted(levels, key=lambda item: item.price, reverse=True):
    if planned and abs(lv.price - planned[-1][0].price) < cluster_gap:
      side = "left" if planned[-1][2] == "right" else "right"
    else:
      side = "right"

    label_y = lv.price
    placed = left_ys if side == "left" else right_ys
    if not _fits(label_y, placed):
      label_y = _nudge_toward_free(label_y, placed)
      if not _fits(label_y, placed):
        alt_side = "left" if side == "right" else "right"
        alt_placed = left_ys if alt_side == "left" else right_ys
        alt_y = _nudge_toward_free(lv.price, alt_placed)
        if _fits(alt_y, alt_placed):
          side, label_y = alt_side, alt_y
          placed = alt_placed

    if side == "right":
      right_ys.append(label_y)
    else:
      left_ys.append(label_y)
    planned.append((lv, label_y, side))

  return planned


def _draw_key_levels(ax, levels: list[KeyLevel], df: pd.DataFrame | None = None) -> None:
  """SpacemanBTC-style horizontal levels with staggered edge labels."""
  if not levels:
    return

  if df is not None and not df.empty:
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    pad = max((hi - lo) * 0.08, hi * 0.01)
    y_lo, y_hi = lo - pad, hi + pad
  else:
    y_lo, y_hi = ax.get_ylim()

  transform = blended_transform_factory(ax.transAxes, ax.transData)
  for lv, label_y, side in _plan_key_level_labels(levels, y_lo, y_hi):
    ax.axhline(lv.price, color=lv.color, linestyle="-", linewidth=1.0, alpha=0.9)
    style = _level_label_style(lv.color)
    text = f"{lv.label} {lv.price:,.2f}"
    if side == "left":
      x, ha, label_text = 0.01, "left", f"{text} "
    else:
      x, ha, label_text = 0.99, "right", f" {text}"
    ax.text(
      x,
      label_y,
      label_text,
      fontsize=FONT_SIZE - 1,
      fontweight="bold",
      va="center",
      ha=ha,
      transform=transform,
      clip_on=True,
      color=style["color"],
      bbox=style["bbox"],
    )


def _draw_htf_zones(ax, df: pd.DataFrame, zones: list[HTFZone]) -> None:
  """IMG-style H4 OB/breaker boxes projected onto the chart timeframe."""
  if not zones or df.empty:
    return
  for zone in zones:
    try:
      x0, x1 = _bar_x_range(df, zone.start_ts, zone.end_ts)
    except (KeyError, ValueError, IndexError):
      continue
    width = max(x1 - x0 + 0.6, 0.8)
    left = x0 - 0.3
    if zone.zone_type == "breaker":
      face = "#FFB6C1" if zone.direction == "bearish" else "#90EE90"
      edge = "#CC0000" if zone.direction == "bearish" else "#228B22"
      label = "BRKR"
    else:
      face = "#FFB6C1" if zone.direction == "bearish" else "#90EE90"
      edge = "#CD5C5C" if zone.direction == "bearish" else "#228B22"
      label = "OB"
    height = float(zone.high) - float(zone.low)
    rect = Rectangle(
      (left, float(zone.low)),
      width,
      height,
      facecolor=face,
      edgecolor=edge,
      alpha=0.28,
      linewidth=1.2,
      zorder=1,
    )
    ax.add_patch(rect)
    mid = float(zone.low) + height / 2
    ax.hlines(
      mid,
      left,
      left + width,
      colors=edge,
      linewidth=0.8,
      alpha=0.5,
      zorder=2,
    )
    ax.text(
      left,
      float(zone.high),
      f" H4 {label}",
      color=edge,
      fontsize=FONT_SIZE - 1,
      fontweight="bold",
      va="bottom",
      clip_on=True,
    )


def _draw_fib_zone(
  ax,
  df: pd.DataFrame,
  low: float,
  high: float,
  direction: str,
  start_ts: str | None = None,
  end_ts: str | None = None,
) -> None:
  """Shade 0.25–0.50 entry band inside an OB."""
  z_low, z_high = fib_zone_bounds(direction, low, high)  # type: ignore[arg-type]
  if start_ts and end_ts:
    x0, x1 = _bar_x_range(df, start_ts, end_ts)
  else:
    x0 = float(max(0, len(df) - 30))
    x1 = float(len(df) - 1)
  left = x0 - 0.3
  width = max(x1 - x0 + 0.6, 0.8)
  rect = Rectangle(
    (left, z_low),
    width,
    z_high - z_low,
    facecolor="#FFD700",
    edgecolor="#B8860B",
    alpha=0.35,
    linewidth=1.2,
    zorder=3,
  )
  ax.add_patch(rect)
  ax.text(
    left,
    z_high,
    " Fib 0.25–0.50",
    color="#B8860B",
    fontsize=FONT_SIZE - 1,
    fontweight="bold",
    va="bottom",
    clip_on=True,
  )


def _save_chart_figure(fig, path: Path) -> str:
  path.parent.mkdir(parents=True, exist_ok=True)
  fig.savefig(path, dpi=DPI, bbox_inches="tight")
  plt.close(fig)
  _ensure_telegram_safe_image(path)
  return str(path)


def render_marked_charts(
  data: dict[str, list[dict]],
  key_levels: list[KeyLevel],
  htf_zones: list[HTFZone],
  cycle_id: str | None = None,
  market_context: MarketContext | None = None,
) -> dict[str, str]:
  """Render macro-marked candlestick PNGs per strategy timeframe."""
  out_dir = _ensure_charts_dir()
  cycle_id = cycle_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  paths: dict[str, str] = {}
  visible_levels_cache: dict[str, list[KeyLevel]] = {}

  for tf in research.STRATEGY_TIMEFRAMES:
    bars = data.get(tf)
    if not bars:
      raise ValueError(f"Missing OHLC data for {tf}")
    df = _to_mpf_df(bars)
    visible_levels_cache[tf] = _visible_key_levels(key_levels, df)
    path = out_dir / f"{cycle_id}_{tf}_marked.png"
    fig, ax = _render_candlestick_figure(df, f"ETH-USD {tf} — Key Levels + H4 Structure")
    _draw_htf_zones(ax, df, htf_zones)
    _draw_swing_hlines(ax, df)
    _draw_key_levels(ax, visible_levels_cache[tf], df)
    if tf == "M5" and market_context is not None:
      _draw_detected_overlays(ax, df, market_context)
    paths[tf] = _save_chart_figure(fig, path)

  return paths


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


def _draw_price_line(
  ax,
  price: float,
  label: str,
  color: str,
  linestyle: str,
  *,
  label_side: str = "right",
) -> None:
  """Draw a horizontal level with a label pinned inside the chart (Telegram-safe)."""
  ax.axhline(price, color=color, linestyle=linestyle, linewidth=1.8, alpha=0.95)
  transform = blended_transform_factory(ax.transAxes, ax.transData)
  if label_side == "left":
    x, ha = 0.01, "left"
    text = f"{label} {price:,.2f} "
  else:
    x, ha = 0.99, "right"
    text = f" {label} {price:,.2f}"
  ax.text(
    x,
    price,
    text,
    color=color,
    fontsize=FONT_SIZE,
    fontweight="bold",
    va="center",
    ha=ha,
    transform=transform,
    clip_on=True,
  )


def _save_figure(fig, path: Path) -> str:
  """Save PNG at fixed figsize (avoid bbox_inches=tight blowing up width)."""
  path.parent.mkdir(parents=True, exist_ok=True)
  fig.tight_layout()
  fig.savefig(path, dpi=DPI, pad_inches=0.15)
  plt.close(fig)
  _ensure_telegram_safe_image(path)
  return str(path)


def _ensure_telegram_safe_image(path: Path) -> None:
  """Downscale charts that exceed Telegram limits; never squash to a thin strip."""
  try:
    from PIL import Image
  except ImportError:
    return

  with Image.open(path) as im:
    width, height = im.size
    if height <= 0 or width <= 0:
      return

    aspect = width / height
    if aspect > 8 or aspect < 0.125:
      # Degenerate export — resizing would produce an unusable sliver.
      return

    too_large = (
      width > TELEGRAM_MAX_CHART_WIDTH
      or height > TELEGRAM_MAX_CHART_HEIGHT
      or width * height > 25_000_000
    )
    if not too_large:
      return

    scale = min(
      TELEGRAM_MAX_CHART_WIDTH / width,
      TELEGRAM_MAX_CHART_HEIGHT / height,
      1.0,
    )
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    resized = im.resize(new_size, Image.Resampling.LANCZOS)
    resized.save(path, optimize=True)


def _draw_detected_overlays(
  ax,
  df: pd.DataFrame,
  market_context: MarketContext | None,
) -> None:
  """Draw programmatic 24h range and order blocks on the M5 chart."""
  if market_context is None:
    return

  if market_context.range_24h:
    r = market_context.range_24h
    _draw_price_line(ax, r.high, "24h High", "#7B68EE", ":")
    _draw_price_line(ax, r.low, "24h Low", "#7B68EE", ":")

  for ob in market_context.order_blocks[-3:]:
    try:
      x0, x1 = _bar_x_range(df, ob.start_ts, ob.end_ts)
      left = x0 - 0.3
      width = max(x1 - x0 + 0.6, 0.8)
      color = "#90EE90" if ob.direction == "bullish" else "#FFB6C1"
      edge = "#228B22" if ob.direction == "bullish" else "#CD5C5C"
      rect = Rectangle(
        (left, float(ob.low)),
        width,
        float(ob.high) - float(ob.low),
        facecolor=color,
        edgecolor=edge,
        alpha=0.25,
        linewidth=1.0,
        zorder=1,
      )
      ax.add_patch(rect)
      ax.text(
        left,
        float(ob.high),
        " M5 OB",
        color=edge,
        fontsize=FONT_SIZE - 1,
        fontweight="bold",
        va="bottom",
        clip_on=True,
      )
    except (KeyError, ValueError, IndexError):
      continue


def build_output_charts(
  suggestion: Suggestion,
  data: dict[str, list[dict]],
  key_levels: list[KeyLevel],
  htf_zones: list[HTFZone],
  cycle_id: str,
  market_context: MarketContext | None = None,
) -> list[str]:
  """
  Build 1–2 proof charts (structure + entry) with macro overlays retained.
  """
  out_dir = _ensure_charts_dir()
  paths: list[str] = []
  valid_tfs = set(research.STRATEGY_TIMEFRAMES)

  def _render_output_chart(
    tf: str,
    suffix: str,
    title: str,
    *,
    show_trade: bool = False,
    show_fib: bool = False,
  ) -> str:
    bars = data.get(tf)
    if not bars:
      raise ValueError(f"Missing OHLC for {tf}")
    df = _to_mpf_df(bars)
    fig, ax_price = _render_candlestick_figure(df, title, figsize=OUTPUT_FIGSIZE)
    vis = _visible_key_levels(key_levels, df)
    _draw_htf_zones(ax_price, df, htf_zones)
    _draw_swing_hlines(ax_price, df)
    _draw_key_levels(ax_price, vis, df)
    if tf == "M5":
      _draw_detected_overlays(ax_price, df, market_context)

    if show_fib and suggestion.order_block:
      ob = suggestion.order_block
      direction = "bearish" if "sell" in suggestion.action else "bullish"
      _draw_fib_zone(
        ax_price,
        df,
        float(ob["low"]),
        float(ob["high"]),
        direction,
        ob.get("start_ts"),
        ob.get("end_ts"),
      )

    if show_trade and suggestion.action != "no_trade":
      if suggestion.order_block and not show_fib:
        ob = suggestion.order_block
        x0, x1 = _bar_x_range(df, ob["start_ts"], ob["end_ts"])
        left = x0 - 0.3
        rect = Rectangle(
          (left, float(ob["low"])),
          max(x1 - x0 + 0.6, 0.8),
          float(ob["high"]) - float(ob["low"]),
          facecolor="#FFD700",
          edgecolor="#B8860B",
          alpha=0.35,
          linewidth=1.5,
          zorder=4,
        )
        ax_price.add_patch(rect)
      if suggestion.entry is not None:
        _draw_price_line(
          ax_price, suggestion.entry, "Entry", "#00AA00", "--", label_side="left"
        )
      if suggestion.stop_loss is not None:
        _draw_price_line(
          ax_price, suggestion.stop_loss, "SL", "#CC0000", "-", label_side="left"
        )
      for i, tp in enumerate(suggestion.take_profits[:3], start=1):
        _draw_price_line(
          ax_price, tp, f"TP{i}", "#0066CC", ":", label_side="left"
        )

    out_path = out_dir / f"{cycle_id}_{tf}_{suffix}.png"
    return _save_chart_figure(fig, out_path)

  if suggestion.action == "no_trade":
    primary = suggestion.decision_charts[0] if suggestion.decision_charts else "H4"
    if primary not in valid_tfs:
      primary = "H4"
    paths.append(
      _render_output_chart(
        primary,
        "notrade",
        f"ETH-USD {primary} — No Trade",
      )
    )
    return paths

  structure_tf = suggestion.structure_chart or "H4"
  entry_tf = suggestion.entry_chart or "M5"
  if structure_tf not in valid_tfs:
    structure_tf = "H4"
  if entry_tf not in valid_tfs:
    entry_tf = "M5"

  paths.append(
    _render_output_chart(
      structure_tf,
      "structure",
      f"ETH-USD {structure_tf} — HTF Structure",
    )
  )

  if entry_tf != structure_tf or suggestion.entry is not None:
    paths.append(
      _render_output_chart(
        entry_tf,
        "entry",
        f"ETH-USD {entry_tf} — Entry / SL / TP",
        show_trade=True,
        show_fib=True,
      )
    )
  elif len(paths) < 2:
    paths.append(
      _render_output_chart(
        entry_tf,
        "entry",
        f"ETH-USD {entry_tf} — Entry",
        show_trade=True,
        show_fib=True,
      )
    )

  return paths[:2]


def annotate_chart(
  m5_path: str,
  suggestion: Suggestion,
  cycle_id: str,
  m5_bars: list[dict] | None = None,
  market_context: MarketContext | None = None,
) -> str:
  """
  Draw trade markup on a full-width M5 chart (rationale is sent separately via Telegram).
  Re-plots from m5_bars for correct price alignment (m5_path used for naming only).
  """
  out_dir = _ensure_charts_dir()
  annotated_path = out_dir / f"{cycle_id}_M5_annotated.png"

  if m5_bars is None:
    m5_bars = research.get_ohlc("M5")
  df = _to_mpf_df(m5_bars)

  title = "ETH-USD M5 — Trade Idea" if suggestion.action != "no_trade" else "ETH-USD M5 — No Trade"
  fig, ax = _render_candlestick_figure(df, title, figsize=OUTPUT_FIGSIZE)

  _draw_detected_overlays(ax, df, market_context)

  if suggestion.action == "no_trade":
    return _save_chart_figure(fig, annotated_path)

  if suggestion.order_block:
    ob = suggestion.order_block
    x0, x1 = _bar_x_range(df, ob["start_ts"], ob["end_ts"])
    left = x0 - 0.3
    width = max(x1 - x0 + 0.6, 0.8)
    rect = Rectangle(
      (left, float(ob["low"])),
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
      left,
      float(ob["high"]),
      " OB",
      color="#B8860B",
      fontsize=FONT_SIZE,
      fontweight="bold",
      va="bottom",
      clip_on=True,
    )

  if suggestion.entry is not None:
    _draw_price_line(ax, suggestion.entry, "Entry", "#00AA00", "--", label_side="left")
  if suggestion.stop_loss is not None:
    _draw_price_line(ax, suggestion.stop_loss, "SL", "#CC0000", "-", label_side="left")
  for i, tp in enumerate(suggestion.take_profits[:3], start=1):
    _draw_price_line(ax, tp, f"TP{i}", "#0066CC", ":", label_side="left")

  return _save_chart_figure(fig, annotated_path)


def render_research_chart(
  bars: list[dict],
  events: list,
  stats: dict,
  timeframe: str = "W1",
  cycle_id: str | None = None,
  years: int = 4,
  title_override: str | None = None,
  panel_text: str | None = None,
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
  title = title_override or f"ETH-USD {timeframe} — SFP Study ({years}y)"
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

  if panel_text is None:
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
  else:
    panel = panel_text
  _draw_rationale_panel(ax_text, "Research", panel)

  return _save_figure(fig, path)


def _fake_suggestion(m5_bars: list[dict]) -> Suggestion:
  """Build a plausible fake long setup from recent M5 structure."""
  df = research.to_dataframe(m5_bars)
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
    rationale="Fake M5 bullish OB retest in discount — markup test",
    order_block={
      "low": round(ob_low, 2),
      "high": round(ob_high, 2),
      "start_ts": start_ts,
      "end_ts": end_ts,
    },
  )


if __name__ == "__main__":
  from patterns.htf_structure import detect_htf_zones
  from patterns.key_levels import compute_key_levels

  cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  print("Fetching live OHLC...")
  data = research.get_all_timeframes()
  daily = research.get_daily_bars_for_levels()
  key_levels = compute_key_levels(daily)
  htf_zones = detect_htf_zones(data["H4"])
  print(f"Detected {len(htf_zones)} H4 OB/BRKR zones")

  print("Rendering marked charts...")
  paths = render_marked_charts(data, key_levels, htf_zones, cycle_id=cycle_id)
  for tf, path in paths.items():
    print(f"  {tf}: {path}")

  fake = _fake_suggestion(data["M5"])
  fake.structure_chart = "H4"
  fake.entry_chart = "M5"
  fake.decision_charts = ["H4", "M5"]

  outputs = build_output_charts(fake, data, key_levels, htf_zones, cycle_id)
  for path in outputs:
    print(f"  output: {path}")
