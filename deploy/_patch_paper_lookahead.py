"""Apply the barrier-walk window fix to the server's paper.py in place.

The server's paper.py is neither local HEAD nor the local working copy — it
carries its own in-progress changes — so the file is patched by anchored
replacement rather than overwritten. Every anchor is checked before anything
is written, and --check reports without modifying.
"""
import argparse
import pathlib
import shutil
import sys

OLD_M5 = '''def _m5_path(product_id: str, since: str | None) -> list[dict]:
    """M5 bars between the last barrier walk and now, oldest first.

    Returns empty when the window is unknown or the fetch fails, which
    collapses the caller back to a spot-only check. A missing candle feed must
    not stall the cycle, but it does mean a stop can still be missed, so the
    failure is logged rather than swallowed silently.
    """
    if not since:
        return []
    try:
        start = int(
            datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        return []
    end = int(datetime.now(timezone.utc).timestamp())
    if start >= end:
        return []'''

NEW_M5 = '''_M5_SECONDS = 300


def _m5_path(
    product_id: str, since: str | None, not_before: str | None = None
) -> list[dict]:
    """M5 bars between the last barrier walk and now, oldest first.

    Returns empty when the window is unknown or the fetch fails, which
    collapses the caller back to a spot-only check. A missing candle feed must
    not stall the cycle, but it does mean a stop can still be missed, so the
    failure is logged rather than swallowed silently.

    ``since`` is rounded *down* to the M5 boundary so the partial bar the last
    walk stopped inside is re-walked rather than skipped; re-walking a bar is
    idempotent, whereas skipping one loses a wick. ``not_before`` is the entry
    time and is rounded *up*, because the bar that straddles the entry carries
    ticks from before the position existed and must not resolve it.
    """
    if not since:
        return []
    try:
        start = int(
            datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        return []
    start -= start % _M5_SECONDS
    if not_before:
        try:
            entry = int(
                datetime.fromisoformat(
                    str(not_before).replace("Z", "+00:00")
                ).timestamp()
            )
        except ValueError:
            entry = None
        if entry is not None:
            if entry % _M5_SECONDS:
                entry += _M5_SECONDS - (entry % _M5_SECONDS)
            start = max(start, entry)
    end = int(datetime.now(timezone.utc).timestamp())
    if start >= end:
        return []'''

OLD_PROBES = '''def _barrier_probes(
    side: str, product_id: str, since: str | None, spot: float
) -> list[tuple[float, float]]:'''

NEW_PROBES = '''def _barrier_probes(
    side: str, product_id: str, since: str | None, spot: float,
    not_before: str | None = None,
) -> list[tuple[float, float]]:'''

OLD_CALL = '''    for bar in _m5_path(product_id, since):'''
NEW_CALL = '''    for bar in _m5_path(product_id, since, not_before):'''

OLD_SITE = '''        probes = _barrier_probes(
            side, product_id, position.get("path_checked_at"), spot
        )'''
NEW_SITE = '''        probes = _barrier_probes(
            side, product_id, position.get("path_checked_at"), spot,
            position.get("opened_at"),
        )'''

EDITS = [
    ("_m5_path window", OLD_M5, NEW_M5),
    ("_barrier_probes signature", OLD_PROBES, NEW_PROBES),
    ("_m5_path call site", OLD_CALL, NEW_CALL),
    ("barrier walk entry floor", OLD_SITE, NEW_SITE),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    p = pathlib.Path(args.path)
    src = p.read_text(encoding="utf-8")

    ok = True
    for name, old, new in EDITS:
        n = src.count(old)
        if new in src and n == 0:
            print(f"  [already applied] {name}")
            continue
        if n != 1:
            print(f"  [ANCHOR MISS x{n}] {name}")
            ok = False
        else:
            print(f"  [ok] {name}")
    if not ok:
        print("refusing to patch: anchors did not match exactly once")
        return 1
    if args.check:
        print("check only, nothing written")
        return 0

    shutil.copy2(p, p.with_suffix(".py.prelookahead"))
    for _, old, new in EDITS:
        if old in src:
            src = src.replace(old, new, 1)
    p.write_text(src, encoding="utf-8")
    print(f"patched {p} (backup at {p.with_suffix('.py.prelookahead')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
