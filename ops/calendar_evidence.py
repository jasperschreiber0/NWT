"""Immutable local calendar receipts; persistence failure blocks inspection."""
from calendar_store import store
from datetime import datetime, timezone
from health_checks import calendar_read, HealthCheckError


def retain_calendar(state, receipt, result):
    event = {'version': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
             'result': result, 'operation': 'broker.calendar',
             'attempts': receipt['attempts'], 'elapsed_seconds': receipt['elapsed_seconds'],
             'error_classes': receipt.get('error_classes', receipt.get('recovered_errors', [])),
             'terminal_error': receipt.get('error_type')}
    return store(state, event)


def observed_calendar_read(fetch, state, monotonic=None):
    try:
        data, receipt = calendar_read(fetch, monotonic)
    except HealthCheckError as original:
        try:
            retain_calendar(state, original.details(), 'failed')
        except Exception:
            error = HealthCheckError('calendar.evidence', 'PersistenceFailed', original.elapsed_seconds)
            error.calendar_failure = original.details()
            raise error from None
        raise
    if receipt['attempts'] == 1:
        return data, receipt  # Ordinary success creates no evidence volume.
    try:
        receipt['evidence_id'] = retain_calendar(state, receipt, 'recovered' if receipt['attempts'] > 1 else 'ok')
    except Exception:
        error = HealthCheckError('calendar.evidence', 'PersistenceFailed', receipt['elapsed_seconds'])
        error.calendar_failure = dict(receipt)
        raise error from None
    return data, receipt
