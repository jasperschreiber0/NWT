"""
nwt_agents/track_f/themes.py
Theme/ticker/term configuration for the Track F scanner. This is the one
place both scanner.py and (indirectly, via the DB it writes) the dashboard's
exposure-cap calculation derive theme membership from — adding a ticker or
theme here only changes what scanner.py *scores*; it never trades, and a
ticker only reaches nwt_track_f_candidates (human review) once it clears
BOTTLENECK_SCORE_CANDIDATE_THRESHOLD.

CONFIRMED_THEMES matches the theme/ticker set the dashboard has shipped
since its first build (moved here from a hardcoded dict in dashboard/app.py
so there's one source of truth, not two that can drift). CANDIDATE_THEMES
are speculative, pre-thesis themes scanned the same way but surfaced to
nwt_emerging_themes for approval before their tickers count toward anything.

revenue_leverage/attention_gap/smart_money_score/crowding_penalty are the
four analyst-curated inputs to compute_bottleneck_score() (see scanner.py's
docstring for exactly which components are live-computed from EDGAR vs
curated here) — defaults match validate_historical.py's own conservative
fallback so the live scanner's scores stay comparable to the backtest.
"""

_DEFAULT_APPROXIMATION = {
    "revenue_leverage": 50,
    "attention_gap": 15,
    "smart_money_score": 0,
    "crowding_penalty": 5,
}

CONFIRMED_THEMES = {
    "ai_power": {
        "tickers": ["ETN", "PWR", "VRT", "POWL", "EMR"],
        "terms": [
            "grid congestion", "power demand", "data center power",
            "utility interconnection", "transformer", "switchgear",
            "hyperscaler", "electric utility", "renewable energy",
        ],
    },
    "ai_networking": {
        "tickers": ["ANET", "AVGO", "CSCO"],
        "terms": [
            "data center networking", "switch fabric", "AI infrastructure",
            "high-speed interconnect", "network capacity", "hyperscaler",
        ],
    },
    "ai_cooling": {
        "tickers": ["VRT", "TT", "GNRC"],
        "terms": [
            "liquid cooling", "data center thermal", "cooling capacity",
            "thermal management", "direct-to-chip", "immersion cooling",
            "data center infrastructure",
        ],
    },
    "nuclear": {
        "tickers": ["CCJ", "NNE", "LEU"],
        "terms": [
            "uranium", "nuclear power", "nuclear energy", "fuel supply",
            "long-term contract", "uranium concentrate", "nuclear PPA", "enrichment",
        ],
    },
    "robotics": {
        "tickers": ["TDY", "ISRG", "ONTO"],
        "terms": [
            "automation", "robotics", "machine vision", "precision instruments",
            "industrial automation", "semiconductor inspection",
        ],
    },
    "copper_constraint": {
        "tickers": ["FCX", "SCCO", "WIRE"],
        "terms": [
            "copper supply", "grid electrification", "mine production",
            "smelter capacity", "copper demand", "electrification",
        ],
    },
}

# Speculative — same scoring, but results go to nwt_emerging_themes (pending
# human approval), never directly to nwt_track_f_candidates or the exposure cap.
CANDIDATE_THEMES = {
    "grid_storage": {
        "tickers": ["FLNC", "STEM", "NEE"],
        "terms": [
            "grid-scale battery storage", "battery energy storage system",
            "long-duration storage", "grid stabilization",
        ],
    },
    "small_modular_reactors": {
        "tickers": ["OKLO", "SMR", "BWXT"],
        "terms": [
            "small modular reactor", "microreactor", "advanced nuclear",
            "NRC licensing", "reactor design certification",
        ],
    },
}

# Per-ticker curated inputs (see module docstring). Entity name is required
# for EDGAR's `entity` filter param — must match how the company is indexed.
TICKER_ENTITY = {
    "ETN": "Eaton", "PWR": "Quanta Services", "VRT": "Vertiv", "POWL": "Powell Industries",
    "EMR": "Emerson Electric", "ANET": "Arista Networks", "AVGO": "Broadcom", "CSCO": "Cisco",
    "TT": "Trane Technologies", "GNRC": "Generac", "CCJ": "Cameco", "NNE": "NANO Nuclear",
    "LEU": "Centrus Energy", "TDY": "Teledyne", "ISRG": "Intuitive Surgical", "ONTO": "Onto Innovation",
    "FCX": "Freeport-McMoRan", "SCCO": "Southern Copper", "WIRE": "Encore Wire",
    "FLNC": "Fluence Energy", "STEM": "Stem Inc", "NEE": "NextEra Energy",
    "OKLO": "Oklo", "SMR": "NuScale Power", "BWXT": "BWX Technologies",
}

TICKER_APPROXIMATIONS = {
    # Cameco is a Canadian foreign private issuer (40-F/6-K) — EDGAR EFTS
    # does not fully index those forms for full-text search, so its live
    # theme_momentum/constraint_severity will read artificially low. Known
    # limitation, documented in validate_historical.py's own comments too.
    "CCJ": {"revenue_leverage": 95, "attention_gap": 15, "smart_money_score": 0, "crowding_penalty": 5},
}

BOTTLENECK_SCORE_CANDIDATE_THRESHOLD = 60  # matches validate_historical.py's own validated pass criteria


def get_approximation(ticker: str) -> dict:
    return TICKER_APPROXIMATIONS.get(ticker, _DEFAULT_APPROXIMATION)


def get_forms(ticker: str) -> list:
    return ["40-F", "6-K", "40-F/A"] if ticker == "CCJ" else ["10-Q", "10-K", "8-K"]
