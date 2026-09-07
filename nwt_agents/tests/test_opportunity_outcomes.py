import pytest

from opportunity_outcomes import LANES, upsert_outcome


class Cursor:
    description = [("ok",)]

    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Conn:
    def __init__(self):
        self.cursor_obj = Cursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def test_all_lanes_are_explicit():
    assert LANES == ("RAW_SHADOW", "BRAIN_SHADOW", "RISK_SHADOW", "PAPER", "LIVE")


def test_upsert_is_idempotent_shape():
    conn = Conn()
    upsert_outcome(conn, "00000000-0000-0000-0000-000000000001", "RAW_SHADOW", {
        "strategy_id": "EU-MR-001",
        "symbol": "FEZ",
        "direction": "short",
        "regime": {"primary_regime": "risk_on"},
        "decision": "CANDIDATE",
    })
    sql, params = conn.cursor_obj.calls[0]
    assert "nwt_opportunity_outcomes" in sql
    assert "ON CONFLICT" in sql
    assert params[2] == "EU-MR-001"
    assert conn.commits == 1


def test_invalid_lane_rejected_before_database_call():
    with pytest.raises(ValueError, match="invalid outcome lane"):
        upsert_outcome(Conn(), "id", "UNKNOWN", {"strategy_id": "S", "symbol": "SPY"})


def test_required_fields_rejected():
    with pytest.raises(ValueError, match="missing required fields"):
        upsert_outcome(Conn(), "id", "PAPER", {"strategy_id": "S"})

