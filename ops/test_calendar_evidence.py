"""Portable algorithm tests; POSIX security/durability are a separate gate."""
import json
import os
import stat
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
import requests
import calendar_store as store
import calendar_evidence as evidence
from health_checks import HealthCheckError


def event():
    return dict(version=1, observed_at='2026-11-04T10:00:00+00:00', result='recovered',
                operation='broker.calendar', attempts=2,elapsed_seconds=15.1,
                error_classes=['ConnectTimeout'],terminal_error=None)


@pytest.fixture
def portable(tmp_path,monkeypatch):
    # Emulate dir_fd operations on Windows, NOT POSIX security or directory fsync.
    folder=tmp_path/'calendar-receipts';folder.mkdir();lock=threading.Lock()
    @contextmanager
    def locked(state):
        with lock: yield folder
    monkeypatch.setattr(store,'locked_store',locked)
    original_open=os.open; original_link=os.link; original_unlink=os.unlink; original_sync=os.fsync
    def opened(name,flags,mode=0o777,dir_fd=None):
        return original_open((dir_fd/name) if dir_fd else name,flags & ~getattr(os,'O_NOFOLLOW',0),mode)
    def linked(a,b,src_dir_fd=None,dst_dir_fd=None,follow_symlinks=False):
        # Synthetic publication model only: Windows hardlinks are not permitted.
        # Actual atomic link/no-replace semantics MUST run in Linux tests.
        with (dst_dir_fd/b).open('xb') as out:
            out.write((src_dir_fd/a).read_bytes())
    def unlinked(name,dir_fd=None): return original_unlink(dir_fd/name)
    monkeypatch.setattr(store,'os',SimpleNamespace(open=opened,fdopen=os.fdopen,listdir=os.listdir,
        fstat=os.fstat,geteuid=lambda:0,fsync=lambda fd:None if fd==folder else original_sync(fd),
        link=linked,unlink=unlinked,O_RDONLY=os.O_RDONLY,O_WRONLY=os.O_WRONLY,O_CREAT=os.O_CREAT,
        O_EXCL=os.O_EXCL,O_NOFOLLOW=0,O_NONBLOCK=0))
    monkeypatch.setattr(store,'checked_stat',lambda *a,**k:None)
    return tmp_path,folder


def test_recovery_survives_later_failure_and_next_success(portable):
    state,folder=portable;calls=[]
    def fetch(timeout):
        calls.append(timeout)
        if len(calls)==1:raise requests.ConnectTimeout('PRIVATE')
        return []
    evidence.observed_calendar_read(fetch,state,lambda:0)
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    (state/'status.json').write_text('{"issues":["later clock failure"]}')
    evidence.observed_calendar_read(lambda timeout:[],state,lambda:0)
    (state/'status.json').write_text('{"issues":[]}')
    assert before=={p.name:p.read_bytes() for p in folder.iterdir()}
    assert store.read_receipts(state)[0]['result']=='recovered'


def test_healthy_has_no_storage_dependency(monkeypatch,tmp_path):
    monkeypatch.setattr(evidence,'store',lambda *a:pytest.fail('ordinary success stored'))
    evidence.observed_calendar_read(lambda timeout:[],tmp_path,lambda:0)
    assert not list(tmp_path.iterdir())


def test_persistent_failure_retained(portable):
    state,_=portable
    def fail(timeout):raise requests.ConnectTimeout('PRIVATE')
    with pytest.raises(HealthCheckError):evidence.observed_calendar_read(fail,state,lambda:0)
    assert store.read_receipts(state)[0]['error_classes']==['ConnectTimeout','ConnectTimeout']


@pytest.mark.parametrize('limit',['MAX_RECORDS','MAX_BYTES'])
def test_limit_preserves_evidence_blocks_append(portable,monkeypatch,limit):
    state,folder=portable;store.store(state,event());before=list(folder.iterdir())[0].read_bytes()
    monkeypatch.setattr(store,limit,1 if limit=='MAX_RECORDS' else len(before))
    with pytest.raises(ValueError):store.store(state,event())
    assert len(list(folder.iterdir()))==1
    assert list(folder.iterdir())[0].read_bytes()==before


@pytest.mark.parametrize('stage',['write_sync','publish','after_publish_sync','unlink','final_sync'])
def test_crash_windows_never_publish_partial_or_silently_recover(portable,monkeypatch,stage):
    state,folder=portable;sync=store.os.fsync;link=store.os.link;unlink=store.os.unlink;ticks=[]
    def broken_sync(fd):
        ticks.append(fd)
        if (stage=='write_sync' and fd!=folder) or (stage=='after_publish_sync' and len(ticks)==2) or (stage=='final_sync' and len(ticks)==3):raise OSError('injected')
        return sync(fd)
    def broken_link(*a,**k):
        if stage=='publish':raise OSError('injected')
        return link(*a,**k)
    def broken_unlink(*a,**k):
        if stage=='unlink':raise OSError('injected')
        return unlink(*a,**k)
    monkeypatch.setattr(store.os,'fsync',broken_sync);monkeypatch.setattr(store.os,'link',broken_link);monkeypatch.setattr(store.os,'unlink',broken_unlink)
    with pytest.raises(OSError):store.store(state,event())
    for p in folder.glob('*.json'):assert store.decode(p.read_bytes())==event()
    if stage!='final_sync':
        with pytest.raises(ValueError):store.read_receipts(state)
    else:assert store.read_receipts(state)==[event()]


@pytest.mark.parametrize('raw',[b'{',b'{}\n',b'x'*4097,b'{"event":{},"event":{}}\n'])
def test_reader_rejects_partial_corrupt_oversize(raw):
    with pytest.raises((ValueError,TypeError)):store.decode(raw)


def test_checksum_and_schema_rejection():
    raw=json.loads(store.encode(event()));raw['event']['attempts']=1
    with pytest.raises(ValueError):store.decode((json.dumps(raw)+'\n').encode())


def test_concurrent_capacity_is_serialized(portable,monkeypatch):
    state,folder=portable;monkeypatch.setattr(store,'MAX_RECORDS',3)
    def write(n):
        try:store.store(state,event());return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(write,range(8)))
    assert sum(results)==3 and len(list(folder.iterdir()))==3


def test_persistence_failure_blocks_and_preserves_original(monkeypatch,tmp_path):
    def broken(*a):raise OSError('PRIVATE')
    monkeypatch.setattr(evidence,'store',broken)
    def fail(timeout):raise requests.ConnectTimeout('PRIVATE')
    with pytest.raises(HealthCheckError) as error:evidence.observed_calendar_read(fail,tmp_path,lambda:0)
    assert error.value.operation=='calendar.evidence'
    assert error.value.details()['calendar_failure']['attempts']==2
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('mode,uid,nlink',[(stat.S_IFLNK|0o700,7,1),(stat.S_IFDIR|0o777,7,1),(stat.S_IFDIR|0o700,8,1),(stat.S_IFREG|0o600,7,2)])
def test_unsafe_metadata_rejected(mode,uid,nlink):
    with pytest.raises(ValueError):store.checked_stat(SimpleNamespace(st_mode=mode,st_uid=uid,st_nlink=nlink),7,directory=not stat.S_ISREG(mode))


def test_private_metadata_accepted():
    store.checked_stat(SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=7,st_nlink=2),7,True)
