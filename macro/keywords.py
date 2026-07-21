"""Tiered keyword relevance scoring for macro headline promotion."""

from __future__ import annotations

import re

T1_POINTS = 40
T2_POINTS = 20
T3_POINTS = 5
NEGATIVE_POINTS = -30

BLOCKED_STANDALONE = frozenset({"fed", "eth", "war", "ira", "hack"})

T1_PHRASES: list[str] = [
    # Monetary policy / Fed
    "federal reserve", "rate decision", "rate cut", "rate hike", "jerome powell",
    "emergency rate", "unscheduled meeting", "jackson hole", "dot plot",
    "quantitative tightening", "quantitative easing", "balance sheet runoff",
    # Inflation prints
    "core cpi", "core pce", "nonfarm payrolls", "jobs report", "unemployment rate",
    # Crypto structural / systemic
    "etf approval", "etf rejection", "spot etf", "ethereum etf", "eth etf", "staking etf",
    "exchange halt", "exchange hack", "exchange insolvency", "withdrawals halted",
    "withdrawals suspended", "stablecoin depeg", "usdt depeg", "usdc depeg",
    "tether collapse", "coinbase sec", "exchange freeze", "bridge hack",
    "protocol exploit", "oracle failure", "mass liquidation",
    # Geopolitical shock
    "strait of hormuz", "iran sanctions", "iran oil", "tanker attack", "tanker attacks",
    "oil embargo", "opec cut", "opec+ cut", "missile strike", "declares war",
    "martial law", "ceasefire collapse",
    # Regulatory hammer (bearish enforcement)
    "sec lawsuit", "sec charges", "doj crypto", "treasury sanctions ethereum",
    "crypto ban", "mining ban", "exchange shutdown", "tornado cash", "ofac crypto",
    # Regulatory / legislative catalyst (structural — bullish or bearish)
    "clarity act", "genius act", "market structure bill", "market structure act",
    "stablecoin bill", "crypto legislation", "crypto market structure",
    "signed into law", "senate passes", "house passes", "passes the senate",
    "passes the house", "clears the senate", "clears the house",
]

T1_TERMS: list[str] = [
    "fomc", "powell", "pivot", "cpi", "pce", "ppi", "nfp", "depeg", "binance",
    "contagion", "hormuz", "nuclear", "invasion",
]

T2_PHRASES: list[str] = [
    # Energy
    "oil price", "natural gas", "energy prices", "dollar index",
    # Inflation / rates
    "treasury yields", "10-year", "bond selloff", "yield curve", "basis points",
    "credit spreads",
    # Geopolitical
    "middle east", "trade war", "export controls", "drone strike",
    # Macro risk-off
    "debt ceiling", "government shutdown", "bank failure", "bank run",
    "credit crunch", "systemic risk", "margin call", "flash crash", "circuit breaker",
    # Crypto regulation / legislation
    "crypto regulation", "treasury crypto", "irs crypto",
    "custody rule", "wells notice", "market structure", "regulatory clarity",
    "crypto bill", "treasury secretary",
    # Crypto ecosystem
    "ethereum upgrade", "hard fork", "gas fees", "layer 2", "staking withdrawals",
    "spot flows", "etf inflows", "etf outflows", "blackrock ethereum", "fidelity ethereum",
    # Macro data
    "consumer confidence", "jobless claims", "durable goods", "michigan sentiment",
    "rate path",
    # Institutional / flows
    "whale transfer", "large transfer", "treasury purchase", "corporate treasury",
    "spot bitcoin", "institutional inflow", "fund flows", "futures open interest",
    "funding rate",
]

T2_TERMS: list[str] = [
    # Energy
    "oil", "crude", "brent", "wti", "gasoline", "opec", "saudi", "refinery", "pipeline",
    "gold", "dxy",
    # Inflation / rates
    "inflation", "deflation", "disinflation", "yields", "hawkish", "dovish",
    "tightening", "easing", "liquidity",
    # Geopolitical
    "iran", "israel", "russia", "ukraine", "china", "taiwan", "gaza", "sanctions",
    "tariff", "tariffs", "escalation", "conflict", "blockade", "sabotage", "cyberattack",
    # Macro risk-off
    "recession", "stagflation", "default", "deleveraging", "contagion",
    # Crypto regulation / legislation
    "sec", "gensler", "cftc", "mica", "subpoena", "legislation", "lawmakers",
    "congress", "senate", "bessent", "regulatory",
    # Crypto ecosystem
    "dencun", "pectra", "staking", "restaking", "eigenlayer", "l2", "blob", "validator",
    "slashing", "merge", "eip", "shanghai", "grayscale",
    # Macro data
    "gdp", "retail", "ism", "pmi", "housing", "boj", "ecb", "boe", "pboc",
    # Institutional
    "whale", "microstrategy", "cme",
]

T3_TERMS: list[str] = [
    "ethereum", "bitcoin", "btc", "crypto", "cryptocurrency", "blockchain", "market",
    "markets", "stocks", "equities", "nasdaq", "dow", "altcoin", "defi", "nft", "web3",
    "digital", "token", "coin", "rally", "selloff", "volatility", "bull", "bear",
    "bullish", "bearish", "price", "trading",
]

NEGATIVE_TERMS: list[str] = [
    "sports", "celebrity", "nfl", "nba", "championship", "recipe", "movie", "film",
    "actor", "actress", "netflix", "gaming", "esports", "horoscope", "dating", "gossip",
    "fashion", "concert", "giveaway", "influencer", "tiktok", "kardashian", "mascot",
]

NEGATIVE_PHRASES: list[str] = [
    "to the moon", "meme coin scam", "airdrop scam", "celebrity endorsement",
    "team wins", "box office", "weather forecast", "music album", "royal family",
    "super bowl", "tv show",
]

# s&p handled separately
T3_PHRASES: list[str] = ["s&p"]


def _normalize(text: str) -> str:
    return f" {text.lower().replace(chr(10), ' ')} "


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if " " in term:
        return re.compile(escaped)
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def _match_phrases(
    working: str,
    phrases: list[str],
    points: int,
    rule: str,
    hits: list[dict],
) -> str:
    for phrase in sorted(phrases, key=len, reverse=True):
        pattern = _term_pattern(phrase)
        while True:
            match = pattern.search(working)
            if match is None:
                break
            hits.append({"rule": rule, "term": phrase, "points": points})
            start, end = match.span()
            working = working[:start] + (" " * (end - start)) + working[end:]
    return working


def _match_terms(
    working: str,
    terms: list[str],
    points: int,
    rule: str,
    hits: list[dict],
    *,
    block_standalone: bool = False,
) -> str:
    for term in sorted(terms, key=len, reverse=True):
        if block_standalone and term in BLOCKED_STANDALONE:
            continue
        pattern = _term_pattern(term)
        while True:
            match = pattern.search(working)
            if match is None:
                break
            hits.append({"rule": rule, "term": term, "points": points})
            start, end = match.span()
            working = working[:start] + (" " * (end - start)) + working[end:]
    return working


def relevance_score(
    text: str,
    extra_t2: list[str] | None = None,
) -> tuple[int, list[dict]]:
    """Return (score 0-100, keyword_hits audit trail)."""
    working = _normalize(text)
    hits: list[dict] = []
    score = 0

    working = _match_phrases(working, T1_PHRASES, T1_POINTS, "T1_PHRASE", hits)
    extra_phrases = [t.strip().lower() for t in (extra_t2 or []) if t.strip()]
    if extra_phrases:
        working = _match_phrases(working, extra_phrases, T2_POINTS, "EXTRA_T2", hits)
    working = _match_phrases(working, T2_PHRASES, T2_POINTS, "T2_PHRASE", hits)
    working = _match_phrases(working, T3_PHRASES, T3_POINTS, "T3_PHRASE", hits)

    working = _match_terms(working, T1_TERMS, T1_POINTS, "T1_TERM", hits)
    working = _match_terms(working, T2_TERMS, T2_POINTS, "T2_TERM", hits, block_standalone=True)
    working = _match_terms(working, T3_TERMS, T3_POINTS, "T3_TERM", hits, block_standalone=True)

    for hit in hits:
        score += int(hit["points"])

    neg_working = _normalize(text)
    neg_hits: list[dict] = []
    neg_working = _match_phrases(neg_working, NEGATIVE_PHRASES, NEGATIVE_POINTS, "NEGATIVE_PHRASE", neg_hits)
    neg_working = _match_terms(neg_working, NEGATIVE_TERMS, NEGATIVE_POINTS, "NEGATIVE_TERM", neg_hits)

    for hit in neg_hits:
        score += int(hit["points"])
        hits.append(hit)

    score = max(0, min(100, score))
    return score, hits
