"""Bounded diagnostic vocabulary; never serialize exception text or model payloads."""
import json
import requests

REASONS = frozenset({
    'CLASSIFICATION_SCHEMA', 'CLASSIFICATION_SOURCE_QUOTE', 'CLASSIFICATION_ACTION',
    'EQUITY_QUOTE_UNTRADEABLE', 'OPTION_PAGINATION_REPEATED', 'OPTION_CHAIN_BUDGET',
})
STAGES = frozenset({
    'classification_request', 'classification_validation', 'classification_storage',
    'option_equity_quote', 'option_chain', 'option_selection', 'option_valuation',
    'option_storage', 'market_evaluation',
})


class ResearchValidationError(ValueError):
    def __init__(self, reason):
        self.reason = reason if isinstance(reason,str) and reason in REASONS else 'INVALID_VALUE'
        super().__init__(self.reason)


def failure(stage, exc):
    """Allowlisted stage/reason/category only; no exception text or dynamic class names."""
    reason, category = 'UNEXPECTED_FAILURE', 'Exception'
    if isinstance(exc, ResearchValidationError):
        reason = exc.reason if isinstance(exc.reason,str) and exc.reason in REASONS else 'INVALID_VALUE'
        category = 'ValueError'
    elif isinstance(exc, json.JSONDecodeError):
        reason, category = 'RESPONSE_JSON_INVALID', 'ValueError'
    elif isinstance(exc, (requests.Timeout, TimeoutError)):
        reason, category = 'DEPENDENCY_TIMEOUT', 'TimeoutError'
    elif isinstance(exc, requests.RequestException):
        reason, category = 'DEPENDENCY_REQUEST_FAILED', 'RequestException'
    elif isinstance(exc, ValueError):
        reason, category = 'INVALID_VALUE', 'ValueError'
    elif isinstance(exc, (KeyError, TypeError)):
        reason, category = 'DATA_SHAPE_INVALID', 'LookupOrTypeError'
    elif isinstance(exc, RuntimeError):
        reason, category = 'DEPENDENCY_OR_RUNTIME_FAILURE', 'RuntimeError'
    return dict(stage=stage if isinstance(stage,str) and stage in STAGES else 'unknown', reason=reason, error=category)
