"""Provider routing, failure handling, and mixed historical cost accounting."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai_client
import cost_agent
import prescreener
import conviction_engine


def response(text='[{"symbol":"SPY","score":7}]', status="completed"):
    return Mock(status_code=200, json=lambda: {
        "id": "resp_test", "model": "gpt-4.1-mini-2025-04-14", "status": status,
        "usage": {"input_tokens": 1000, "output_tokens": 100,
                  "input_tokens_details": {"cached_tokens": 200}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    })


def test_openai_only_and_usage(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other-provider.invalid")
    with patch.object(openai_client.requests, "post", return_value=response()) as post, \
         patch("shared_context.log_system_event") as log:
        data, ti, to = openai_client.call_json("JSON please", "gpt-4.1-mini", list, conn=object())
    assert data[0]["symbol"] == "SPY"
    assert (ti, to) == (1000, 100)
    assert post.call_args.args == ("https://api.openai.com/v1/responses",)
    assert post.call_args.kwargs["allow_redirects"] is False
    assert post.call_args.kwargs["json"]["store"] is False
    payload = log.call_args.args[-1]
    assert payload["provider"] == "openai"
    assert payload["tokens_used"]["openai_cost_usd"] == pytest.approx(0.0005)


@pytest.mark.parametrize("text,status", [('[]', 'incomplete'), ('invalid', 'completed'),
                                         ('{}', 'completed'), ('[7]', 'completed')])
def test_invalid_results_are_rejected_but_billed(monkeypatch, text, status):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    with patch.object(openai_client.requests, "post", return_value=response(text, status)), \
         patch("shared_context.log_system_event") as log:
        with pytest.raises((ValueError, RuntimeError)):
            openai_client.call_json("JSON", "gpt-4.1-mini", list, conn=object())
    assert log.call_args.args[-1]["tokens_used"]["openai_in"] == 1000


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_never_fall_back(monkeypatch, status):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    with patch.object(openai_client.requests, "post", return_value=Mock(status_code=status)) as post:
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            openai_client.call_json("JSON", "gpt-4.1", dict)
    assert post.call_count == 1


def test_missing_key_stops_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    with patch.object(openai_client.requests, "post") as post:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            openai_client.call_json("JSON", "gpt-4.1", dict)
    post.assert_not_called()


def test_both_stages_use_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    with patch.object(openai_client.requests, "post", return_value=response()) as post:
        prescreener.call_prescreener("JSON")
        assert post.call_args.kwargs["json"]["model"] == prescreener.PRESCREENER_MODEL
    with patch.object(openai_client.requests, "post", return_value=response('{}')) as post:
        conviction_engine.call_conviction("JSON")
        assert post.call_args.kwargs["json"]["model"] == conviction_engine.CONVICTION_MODEL


def test_mixed_costs_are_counted_once():
    rows = [{"payload": {"tokens_used": {"haiku_in": 1000000, "sonnet_out": 1000000}}},
            {"payload": {"provider": "openai", "tokens_used": {
                "openai_in": 1000, "openai_out": 100, "openai_cost_usd": 0.0005}}},
            {"payload": {"provider": "openai", "model": "gpt-4.1-mini"}},
            {"payload": {"today": {"tokens": {"openai_cost_usd": 0.0005}}}}]
    conn = Mock()
    conn.cursor.return_value.__enter__ = Mock(return_value=Mock(fetchall=lambda: rows))
    conn.cursor.return_value.__exit__ = Mock(return_value=False)
    for fetch in [cost_agent.fetch_token_usage_today, cost_agent.fetch_cumulative_costs]:
        totals = fetch(conn)
        costs = cost_agent.compute_costs(totals)
        assert costs["total_cost_usd"] == pytest.approx(15.8005)
        assert costs["openai_cost_usd"] == 0.0005


def test_prescreener_failure_clears_stale_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(prescreener, "AGENTS_DIR", tmp_path)
    target = tmp_path / "prescreened_symbols.json"
    target.write_text('[{"symbol":"STALE"}]')
    with patch.object(prescreener, "get_db", return_value=Mock()), \
         patch.object(prescreener, "load_layer0_data", return_value={"symbols": {"SPY": {}}}), \
         patch.object(prescreener, "load_master_directives", return_value={}), \
         patch.object(prescreener, "apply_hard_filters", return_value=(["SPY"], [])), \
         patch.object(prescreener, "build_prescreener_prompt", return_value="JSON"), \
         patch.object(prescreener, "call_prescreener", side_effect=RuntimeError("HTTP 401")), \
         patch.object(prescreener, "log_system_event"):
        with pytest.raises(SystemExit):
            prescreener.main()
    assert json.loads(target.read_text()) == []
