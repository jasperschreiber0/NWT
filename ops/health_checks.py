"""Credential-safe context for failed health reads; preserves failure semantics."""
from contextlib import contextmanager
import time


class HealthCheckError(RuntimeError):
    def __init__(self, operation, error_type, elapsed_seconds):
        self.operation = operation
        self.error_type = error_type
        self.elapsed_seconds = round(elapsed_seconds, 3)
        # Stable text preserves daily notification deduplication. Timing belongs
        # in structured details, not the issue key used by the watchdog.
        super().__init__(f'{operation}: {error_type}')

    def details(self):
        return dict(operation=self.operation, error_type=self.error_type,
                    elapsed_seconds=self.elapsed_seconds)


@contextmanager
def health_operation(operation):
    # Labels are fixed call-site names, never URLs, headers or exception bodies.
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        raise HealthCheckError(operation, type(exc).__name__,
                               time.monotonic() - started) from None
