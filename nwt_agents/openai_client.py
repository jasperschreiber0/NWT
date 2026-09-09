"""OpenAI-only JSON inference and per-request usage accounting.

Uses the Responses REST API through the existing requests dependency. No
provider fallback or configurable proxy endpoint can silently change providers.
"""
import json
import os

import requests

API_URL = "https://api.openai.com/v1/responses"
# USD per million tokens: input, cached input, output. Verified 2026-09-09.
MODEL_RATES = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-mini-2025-04-14": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-2025-04-14": (2.00, 0.50, 8.00),
}


def call_json(prompt, model, expected_type, *, conn=None, component="openai_client"):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")
    if model not in MODEL_RATES:
        raise ValueError("OpenAI model has no verified cost rate; configure a supported model")
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "input": prompt, "max_output_tokens": 2048,
              "store": False},
        timeout=(10, 90),
        allow_redirects=False,
    )
    if response.status_code != 200:
        # Do not put provider response bodies or credentials into operational logs.
        raise RuntimeError(f"OpenAI Responses API HTTP {response.status_code}")
    body = response.json()
    usage = body.get("usage") or {}
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0))
    rate_in, rate_cached, rate_out = MODEL_RATES[model]
    cost = ((tokens_in - cached) * rate_in + cached * rate_cached
            + tokens_out * rate_out) / 1_000_000
    # Log billed usage before parsing: incomplete/refused/invalid responses also cost money.
    if conn is not None:
        from shared_context import log_system_event
        log_system_event(conn, "INFO", component, "OpenAI API usage", {
            "provider": "openai", "model": body.get("model", model),
            "response_id": body.get("id"), "status": body.get("status"),
            "tokens_used": {"openai_in": tokens_in, "openai_out": tokens_out,
                            "openai_cached_in": cached, "openai_cost_usd": cost},
        })
    if body.get("status") != "completed":
        raise RuntimeError("OpenAI response incomplete; no proposal accepted")
    parts = [part for item in body.get("output", []) if item.get("type") == "message"
             for part in item.get("content", [])]
    if any(part.get("type") == "refusal" for part in parts):
        raise RuntimeError("OpenAI response refused; no proposal accepted")
    raw = "".join(part.get("text", "") for part in parts
                  if part.get("type") == "output_text").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    value = json.loads(raw)
    if not isinstance(value, expected_type):
        raise ValueError("OpenAI JSON response has an unexpected shape")
    if expected_type is list and not all(isinstance(item, dict) for item in value):
        raise ValueError("OpenAI prescreening items must be objects")
    return value, tokens_in, tokens_out
