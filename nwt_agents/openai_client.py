"""OpenAI-only JSON inference and per-request usage accounting.

Uses the Responses REST API through the existing requests dependency. No
provider fallback or configurable proxy endpoint can silently change providers.
"""
import json
import os
import math
import random
import time
from email.utils import parsedate_to_datetime

import requests

API_URL = "https://api.openai.com/v1/responses"
QUOTA_CODES = frozenset({'insufficient_quota', 'credit_balance_exhausted',
    'organization_spend_limit_exceeded', 'project_spend_limit_exceeded',
    'organization_usage_limit_exceeded', 'billing_hard_limit_reached'})


class OpenAIRequestError(RuntimeError):
    """Only application-owned classifications, never response bodies/headers."""
    def __init__(self, status, category, attempts, quota_code=None, quota_type=None):
        self.details = dict(http_status=status, category=category, attempts=attempts)
        # Only exact enumerated constants are retained; never arbitrary provider text.
        for name, value in [('quota_code', quota_code), ('quota_type', quota_type)]:
            if isinstance(value, str) and value in QUOTA_CODES:
                self.details[name] = value
        super().__init__(f'OpenAI Responses API HTTP {status}: {category}; attempts={attempts}')


def error_metadata(response):
    code = None
    kind = None
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        fields = [error.get('code'), error.get('type')] if isinstance(error, dict) else []
        code = error.get('code') if isinstance(error, dict) else None
        kind = error.get('type') if isinstance(error, dict) else None
        fields = {v for v in fields if isinstance(v, str)}
    except (ValueError, TypeError):
        fields = set()
    if fields & QUOTA_CODES:
        return dict(category='quota_exhausted',
                    quota_code=code if isinstance(code,str) and code in QUOTA_CODES else None,
                    quota_type=kind if isinstance(kind,str) and kind in QUOTA_CODES else None)
    if response.status_code == 429:
        known = {'rate_limit_exceeded', 'slow_down'}
        return dict(category='rate_limited' if (isinstance(code, str) and code in known) or (code is None and fields & known) else 'unknown_429')
    return dict(category={401:'authentication_failed',403:'permission_denied'}.get(response.status_code,'http_error'))


def error_category(response):
    return error_metadata(response)['category']


def retry_delay(headers):
    """Respect Retry-After, or stop if it cannot fit a ten-second wait budget."""
    if headers.get('x-should-retry') == 'false':return None
    value = headers.get('Retry-After')
    if value is None:
        return 1 + random.random()
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:return None
            delay = max(0, stamp.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(1, delay) if math.isfinite(delay) and 0 <= delay <= 10 else None


def error_event(conn, component, details):
    if conn is not None:
        try:
            from shared_context import log_system_event
            log_system_event(conn, 'WARNING', component, 'OpenAI request failure', details)
        except Exception:
            pass  # Audit failure cannot turn an unsuccessful request into success.
# USD per million tokens: input, cached input, output. Verified 2026-09-09.
MODEL_RATES = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-mini-2025-04-14": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-2025-04-14": (2.00, 0.50, 8.00),
}


def call_json(prompt, model, expected_type, *, conn=None, component="openai_client", retry_rate_limit=False):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")
    if model not in MODEL_RATES:
        raise ValueError("OpenAI model has no verified cost rate; configure a supported model")
    for attempt in range(1, 3 if retry_rate_limit else 2):
        try:
            response = requests.post(
                API_URL, headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "input": prompt, "max_output_tokens": 2048,"store": False},
                timeout=(10, 90), allow_redirects=False)
        except requests.RequestException:
            # An ambiguous transport failure is not a safe inference retry.
            error = OpenAIRequestError(None, 'transport_failure', attempt)
            error_event(conn, component, error.details)
            raise error from None
        if response.status_code == 200:break
        metadata = error_metadata(response)
        category = metadata["category"]
        delay = retry_delay(response.headers) if retry_rate_limit and attempt == 1 and category == 'rate_limited' else None
        error = OpenAIRequestError(response.status_code, category, attempt,
                                   metadata.get("quota_code"), metadata.get("quota_type"))
        error_event(conn, component, dict(error.details, retry_scheduled=delay is not None,
                                         retry_delay_seconds=delay))
        response.close()
        if delay is None:raise error from None
        time.sleep(delay)
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
