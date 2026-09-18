"""Flatten model-written markdown into text that reads well in Telegram.

Chat replies are sent without a parse_mode, so anything the model writes in
markdown arrives literally: `##` headers, `**bold**`, `---` rules and pipe
tables all land as punctuation noise. Rendering as Markdown instead is not an
option — Telegram rejects the whole message when an entity fails to parse, and
it has no table or header syntax at all, so the worst offenders would survive
anyway. Flattening to clean prose is the only form that always arrives intact.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*$")
_RULE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?=\S)")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

_LINK = re.compile(r"\[([^\]]+)\]\((\s*<?)([^)\s]+)>?[^)]*\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.DOTALL)
_BOLD_ALT = re.compile(r"__(\S(?:.*?\S)?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])")
_ITALIC_ALT = re.compile(r"(?<![\w_])_(\S(?:[^_\n]*\S)?)_(?![\w_])")

_BLANK_RUN = re.compile(r"\n{3,}")


def _strip_inline(text: str) -> str:
    text = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(3)})", text)
    text = _CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_ALT.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _ITALIC_ALT.sub(r"\1", text)
    return text


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(rows: list[list[str]]) -> list[str]:
    """Turn a pipe table into one labelled block per row.

    A table is unreadable on a phone-width bubble, so each row is re-emitted
    as its first cell followed by `header: value` lines — the same information
    in the shape Telegram can actually show.
    """
    if not rows:
        return []
    headers = rows[0]
    body = rows[1:]
    if not body:
        return [" — ".join(c for c in headers if c)]

    out: list[str] = []
    for row in body:
        label = row[0] if row else ""
        details = []
        for idx in range(1, len(row)):
            value = row[idx]
            if not value:
                continue
            header = headers[idx] if idx < len(headers) else ""
            details.append(f"  {header}: {value}" if header else f"  {value}")
        if label:
            out.append(label)
        out.extend(details)
        out.append("")
    if out and out[-1] == "":
        out.pop()
    return out


def to_plain_text(text: str) -> str:
    """Return `text` with markdown syntax resolved into readable plain text."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    table: list[list[str]] = []
    in_fence = False

    def flush_table() -> None:
        if table:
            out.extend(_render_table(table))
            table.clear()

    for raw in lines:
        if _FENCE.match(raw):
            flush_table()
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(raw)
            continue

        if _TABLE_ROW.match(raw):
            if not _TABLE_SEPARATOR.match(raw):
                table.append([_strip_inline(c) for c in _split_row(raw)])
            continue
        flush_table()

        if _RULE.match(raw):
            out.append("")
            continue

        line = _QUOTE.sub("", raw)
        heading = _HEADING.match(line)
        if heading:
            line = heading.group(1)
        line = _BULLET.sub(r"\1• ", line)
        out.append(_strip_inline(line).rstrip())

    flush_table()

    return _BLANK_RUN.sub("\n\n", "\n".join(out)).strip()
