"""Actual POSIX checks; run only inside the approved offline Linux sandbox."""
import os
from pathlib import Path
import pytest
import calendar_store as store
from test_calendar_evidence import event

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Requires real POSIX directory descriptors/flock/fsync')


@pytest.fixture
def private_state():
    # Harness must supply an owned0700 directory with safe nonsymlink ancestors.
    base=Path(os.environ['CALENDAR_TEST_PRIVATE_ROOT'])
    import tempfile
    with tempfile.TemporaryDirectory(dir=base) as name:
        yield Path(name)


def test_real_atomic_store_reader_and_capacity(private_state,monkeypatch):
    identifier=store.store(private_state,event())
    assert store.read_receipts(private_state)==[event()]
    assert (private_state/'calendar-receipts'/(identifier+'.json')).stat().st_nlink==1
    monkeypatch.setattr(store,'MAX_RECORDS',1)
    with pytest.raises(ValueError):store.store(private_state,event())


def test_real_symlink_component_denied(private_state):
    target=private_state/'actual';target.mkdir(mode=0o700)
    alias=private_state/'alias';alias.symlink_to(target,target_is_directory=True)
    with pytest.raises(OSError):store.store(alias,event())


def test_existing_permissive_directory_rejected_without_repair(private_state):
    directory=private_state/'calendar-receipts';directory.mkdir(mode=0o700);directory.chmod(0o755)
    with pytest.raises(ValueError):store.store(private_state,event())
    assert directory.stat().st_mode & 0o777==0o755


def test_pending_file_reader_rejects_and_preserves(private_state):
    store.store(private_state,event())
    partial=private_state/'calendar-receipts'/'interrupted.pending';partial.write_bytes(b'{')
    with pytest.raises(ValueError):store.read_receipts(private_state)
    assert partial.read_bytes()==b'{'
