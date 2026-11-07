"""Credential-safe context for failed health reads; preserves failure semantics."""
from contextlib import contextmanager
import time
import requests


def calendar_read(fetch, monotonic=None):
    """One fresh read plus one ConnectTimeout retry; never use cached data.

    35 seconds is an acceptance deadline, not a hard cancellation guarantee
    for requests/DNS. Late responses are rejected. No HTTP/read-timeout retry.
    """
    clock = monotonic or time.monotonic
    started = clock()
    failures = []
    attempts = 0
    def failure(kind, elapsed=None):
        error = HealthCheckError('broker.calendar', kind, clock() - started if elapsed is None else elapsed)
        error.attempts = attempts
        error.error_classes = list(failures)
        return error
    for attempt in (1, 2):
        remaining = 35 - (clock() - started)
        if remaining <= 0:
            raise failure('DeadlineExceeded') from None
        try:
            attempts = attempt
            result = fetch(min(15, remaining))
            elapsed = clock() - started
            if elapsed >= 35:
                raise failure('DeadlineExceeded', elapsed)
            return result, dict(attempts=attempt, recovered_errors=failures,
                                elapsed_seconds=round(elapsed, 3))
        except HealthCheckError:
            raise
        except Exception as exc:
            failures.append(type(exc).__name__)
            if isinstance(exc, requests.ConnectTimeout) and attempt == 1:
                continue
            raise failure(type(exc).__name__) from None


class HealthCheckError(RuntimeError):
    def __init__(self, operation, error_type, elapsed_seconds):
        self.operation = operation
        self.error_type = error_type
        self.elapsed_seconds = round(elapsed_seconds, 3)
        # Stable text preserves daily notification deduplication. Timing belongs
        # in structured details, not the issue key used by the watchdog.
        super().__init__(f'{operation}: {error_type}')

    def details(self):
        result = dict(operation=self.operation, error_type=self.error_type,
                      elapsed_seconds=self.elapsed_seconds)
        if hasattr(self, 'attempts'):
            result['attempts'] = self.attempts
        if hasattr(self, 'error_classes'):
            result['error_classes'] = self.error_classes
        if hasattr(self, 'calendar_failure'):
            result['calendar_failure'] = self.calendar_failure
        return result


@contextmanager
def health_operation(operation):
    # Labels are fixed call-site names, never URLs, headers or exception bodies.
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        raise HealthCheckError(operation, type(exc).__name__,
                               time.monotonic() - started) from None
