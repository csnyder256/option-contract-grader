"""Regenerate the market-screener universe.

Builds the FULL US optionable universe (~6k names) and orders it liquidity-first
so a bounded sweep scans the most-tradable names before the long tail.

Sources (try in order; each is a free, no-auth HTTP download):
  1. OCC Directory of Listed Products (authoritative, ~6k distinct underlyings)
  2. Cboe symbol-reference CSV (fallback)
  3. Curated ETFs + S&P 500 only (last-resort fallback)

Run occasionally to refresh:

    python scripts/update_universe.py

Output: app/universe/optionable.txt (the legacy app/universe/sp500_etfs.txt is
left untouched for back-compat). Ordering = curated ETFs -> S&P 500 -> the rest
of the optionable list. Tickers are stored uppercased; the providers map them to
each feed's format at request time.

NOTE: the raw OCC/Cboe/Nasdaq files are for INTERNAL use only - do not
redistribute them. Building your own screener universe from them is the intended
use; republishing the lists is not.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

OCC_URL = "https://marketdata.theocc.com/delo-download?prodType=ALL&downloadFields=US&format=csv"
CBOE_URL = "https://cdn.cboe.com/data/us/options/market_statistics/symbol_reference/cone-underlying.csv"
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

OUT = Path(__file__).resolve().parent.parent / "app" / "universe" / "optionable.txt"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsFinder/0.1"

# Curated liquid, optionable ETFs (broad, sector, bonds, commodities, intl, vol).
ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "QQQM", "RSP", "MDY",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "XBI", "IBB", "KRE", "ITB", "XHB", "XOP", "OIH", "XRT", "JETS",
    "TLT", "IEF", "SHY", "HYG", "LQD", "AGG", "BND", "TIP",
    "GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "DBC",
    "ARKK", "TAN", "LIT", "XME",
    "EEM", "EFA", "FXI", "EWZ", "INDA", "VWO", "VEA", "EWJ",
    "VXX", "UVXY", "SVXY",
]

# Cash-settled index roots to drop (compared by separator-stripped key). These
# are not single-name/ETF underlyings a buyer screener should surface.
INDEX_ROOTS = {
    "SPX", "SPXW", "SPXPM", "XSP", "OEX", "XEO", "DJX", "NDX", "NDXP", "MNX",
    "RUT", "RUTW", "MRUT", "RUI", "VIX", "VIXW", "VVIX", "NANOS", "SET", "BKX",
    "OEXW", "NDXW", "GVZ", "OVX", "RVX",
}


def _http_get(url: str) -> str:
    try:
        import httpx

        return httpx.get(
            url, timeout=60, follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/csv,*/*"},
        ).text
    except Exception:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return r.read().decode("utf-8", errors="replace")


def _norm_key(sym: str) -> str:
    """Separator-stripped key for dedupe/index matching (BRK-B == BRKB == BRK.B)."""
    return sym.replace(".", "").replace("-", "").upper()


def fetch_occ() -> list:
    """OCC DELO list: each line is a space/tab-padded root symbol, no header."""
    text = _http_get(OCC_URL)
    out = []
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        sym = parts[0].strip().upper()
        if sym and sym.isascii() and sym[0].isalpha():
            out.append(sym)
    return out


def fetch_cboe() -> list:
    """Cboe symbol-reference CSV fallback: column 0 = Symbol, with a Symbol Type."""
    text = _http_get(CBOE_URL)
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    type_idx = next(
        (i for i, h in enumerate(header) if h.strip().lower() == "symbol type"), None
    )
    out = []
    for row in reader:
        if not row:
            continue
        sym = row[0].strip().upper()
        if not sym:
            continue
        if type_idx is not None and type_idx < len(row):
            if row[type_idx].strip().lower() == "index":
                continue
        out.append(sym)
    return out


def fetch_sp500() -> list:
    text = _http_get(SP500_CSV_URL)
    syms = []
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
        if sym:
            syms.append(sym.replace(".", "-"))  # Yahoo style for class shares
    return syms


def main() -> None:
    sp500 = fetch_sp500()
    if len(sp500) < 400:
        raise SystemExit(f"Refusing: only got {len(sp500)} S&P names (fetch issue?)")

    # Comprehensive optionable set, with a fallback chain.
    comprehensive = fetch_occ()
    source = "OCC"
    if len(comprehensive) < 1000:
        comprehensive = fetch_cboe()
        source = "Cboe"
    if len(comprehensive) < 1000:
        comprehensive = []  # last resort: ETFs + S&P only
        source = "ETF+S&P only"

    seen = set()
    ordered = []
    for s in ETFS + sp500 + sorted(comprehensive):
        if not s:
            continue
        k = _norm_key(s)
        if k in seen or k in INDEX_ROOTS:
            continue
        seen.add(k)
        ordered.append(s)

    if len(ordered) < 565:
        raise SystemExit(f"Refusing: only {len(ordered)} symbols assembled (fetch issue?)")

    lines = [
        "# Full US optionable universe, liquidity-ordered (ETFs -> S&P 500 -> rest).",
        "# Regenerate with: python scripts/update_universe.py",
        f"# Source: {source}. {len(ETFS)} ETFs + {len(sp500)} S&P + long tail "
        f"= {len(ordered)} unique optionable symbols.",
        f"# Generated: {date.today().isoformat()}. Internal use only; do not redistribute.",
        "",
    ]
    lines.extend(ordered)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(ordered)} symbols to {OUT} (source: {source})")


if __name__ == "__main__":
    main()
