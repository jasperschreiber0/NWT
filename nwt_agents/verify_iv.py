"""
nwt_agents/verify_iv.py
Eyeball check: for each ticker, print our computed 30-DTE ATM IV next to
the provider's raw per-strike IVs around ATM so correctness is visually
verifiable. Run on the server with nwt_agents/.env loaded.

Usage:
    python3 verify_iv.py                  # default SPY QQQ AAPL
    python3 verify_iv.py NVDA TSLA FXI
    python3 verify_iv.py --check-feed     # report whether the current Alpaca
                                          # data tier returns IV/greeks at all
"""

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from iv_pipeline.alpaca_provider import AlpacaIVProvider
from iv_pipeline.pipeline import compute_ticker_iv
from iv_pipeline.provider import IVUnavailableError

logging.basicConfig(level=logging.WARNING)

DEFAULT_TICKERS = ["SPY", "QQQ", "AAPL"]


def check_feed(provider: AlpacaIVProvider) -> int:
    """Empirically check whether the data tier returns IV/greeks."""
    today = date.today()
    try:
        quotes = provider.get_chain(
            "SPY",
            expiry_gte=today + timedelta(days=20),
            expiry_lte=today + timedelta(days=45),
        )
    except IVUnavailableError as exc:
        print(f"FEED CHECK FAILED: {exc}")
        print("→ The current Alpaca options data subscription does not provide IV.")
        print("→ Cheapest viable options: Alpaca 'indicative' feed (free, 15-min")
        print("  delayed quotes but includes greeks/IV) via NWT_ALPACA_OPTIONS_FEED=indicative,")
        print("  or Tradier sandbox (free, greeks included) via the TradierIVProvider stub.")
        return 1
    except Exception as exc:
        print(f"FEED CHECK ERROR (network/auth, not tier): {exc}")
        return 1

    with_iv = sum(1 for q in quotes if q.has_iv)
    with_delta = sum(1 for q in quotes if q.delta is not None)
    print(f"Feed check OK: {len(quotes)} SPY contracts, {with_iv} with IV, "
          f"{with_delta} with delta (feed={provider.feed or 'account default'})")
    return 0


def show_ticker(provider: AlpacaIVProvider, ticker: str) -> None:
    today = date.today()
    print(f"\n{'=' * 72}\n{ticker}\n{'=' * 72}")
    try:
        snap = compute_ticker_iv(provider, ticker, today)
    except IVUnavailableError as exc:
        print(f"  IV UNAVAILABLE: {exc}")
        return
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    print(f"  spot={snap['spot']}  computed atm_iv_30d={snap['atm_iv_30d']}  "
          f"atm_iv_60d={snap['atm_iv_60d']}")
    print(f"  term_slope={snap['term_slope']}  put_skew_25d={snap['put_skew_25d']}  "
          f"hv_20d={snap['hv_20d']}  hv_iv_spread={snap['hv_iv_spread']}")
    print(f"  detail: {snap['detail']}")

    # Raw per-strike IVs around ATM for the expiries the computation
    # actually used, so the computed value can be compared directly
    spot = snap["spot"]
    quotes = provider.get_chain(
        ticker,
        expiry_gte=today + timedelta(days=7),
        expiry_lte=today + timedelta(days=80),
        strike_gte=spot * 0.95,
        strike_lte=spot * 1.05,
    )
    used = (snap["detail"].get("atm_30") or {}).get("expiries")
    if used:
        expiries = sorted({q.expiry for q in quotes if str(q.expiry) in used})
    else:
        expiries = sorted({q.expiry for q in quotes})[:2]
    for exp in expiries:
        print(f"\n  raw chain — expiry {exp} ({(exp - today).days} DTE):")
        print(f"  {'strike':>10} {'call bid/ask':>16} {'call IV':>9} "
              f"{'put bid/ask':>16} {'put IV':>9}")
        by_strike = {}
        for q in quotes:
            if q.expiry == exp:
                by_strike.setdefault(q.strike, {})[q.option_type] = q
        for strike in sorted(by_strike, key=lambda k: abs(k - spot))[:7]:
            c = by_strike[strike].get("call")
            p = by_strike[strike].get("put")
            c_ba = f"{c.bid:.2f}/{c.ask:.2f}" if c else "—"
            p_ba = f"{p.bid:.2f}/{p.ask:.2f}" if p else "—"
            c_iv = f"{c.iv:.4f}" if c and c.iv else "—"
            p_iv = f"{p.iv:.4f}" if p and p.iv else "—"
            print(f"  {strike:>10.2f} {c_ba:>16} {c_iv:>9} {p_ba:>16} {p_iv:>9}")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    provider = AlpacaIVProvider()
    if "--check-feed" in args:
        sys.exit(check_feed(provider))
    tickers = [a.upper() for a in args if not a.startswith("-")] or DEFAULT_TICKERS
    for ticker in tickers:
        show_ticker(provider, ticker)


if __name__ == "__main__":
    main()
