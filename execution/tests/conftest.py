"""
execution/engine.py reads its Alpaca/DB config from os.environ at import
time (fails loudly if missing, by design — see CLAUDE.md's Startup
Integrity Gate). Tests never talk to a real broker or DB (everything that
touches either is mocked), so this just supplies harmless dummy values so
the module can be imported at all.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_DATA_URL", "https://data.alpaca.markets")
os.environ.setdefault("NWT_DB_DSN", "postgresql://test:test@localhost/test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
