"""Bounded POSIX receipt store. No deletion/repair of existing evidence."""
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager

MAX_RECORDS = 1024
MAX_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 4096


def checked_stat(info, uid, directory=False, private=True):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Unexpected storage type')
    if info.st_uid not in ({uid} if private else {0, uid}):
        raise ValueError('Unexpected storage owner')
    if stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022):
        raise ValueError('Unsafe storage permissions')
    if not directory and info.st_nlink != 1:
        raise ValueError('Unexpected hardlink')


def encode(event):
    payload = json.dumps(event, sort_keys=True, separators=(',', ':'), allow_nan=False)
    raw = (json.dumps({'event': event, 'sha256': hashlib.sha256(payload.encode()).hexdigest()},
                      sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    decode(raw)
    return raw


def decode(raw):
    if not raw or len(raw) > MAX_RECORD_BYTES or not raw.endswith(b'\n'):
        raise ValueError('Invalid receipt size/framing')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate field')
            result[key] = value
        return result
    wrapper = json.loads(raw, object_pairs_hook=unique)
    if set(wrapper) != {'event','sha256'}: raise ValueError('Invalid envelope')
    event = wrapper['event']
    required = {'version','observed_at','result','operation','attempts','elapsed_seconds','error_classes','terminal_error'}
    if not isinstance(event,dict) or set(event) != required: raise ValueError('Invalid event')
    from datetime import datetime
    if datetime.fromisoformat(event['observed_at']).utcoffset() is None: raise ValueError('Naive timestamp')
    if event['version'] != 1 or event['operation'] != 'broker.calendar': raise ValueError('Invalid schema')
    if event['result'] not in ('recovered','failed'): raise ValueError('Not relevant evidence')
    if type(event['attempts']) is not int or not 0 <= event['attempts'] <= 2: raise ValueError('Invalid attempts')
    import math
    if type(event['elapsed_seconds']) not in (int,float) or not math.isfinite(event['elapsed_seconds']) or event['elapsed_seconds'] < 0: raise ValueError('Invalid elapsed')
    errors = event['error_classes']
    if not isinstance(errors,list) or len(errors)>2 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',x) for x in errors): raise ValueError('Invalid classes')
    terminal = event['terminal_error']
    if terminal is not None and (not isinstance(terminal,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',terminal)): raise ValueError('Invalid terminal')
    if event['result']=='recovered' and (event['attempts']!=2 or errors!=['ConnectTimeout'] or terminal is not None): raise ValueError('Invalid recovery')
    if event['result']=='failed' and terminal is None: raise ValueError('Missing failure')
    payload=json.dumps(event,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()
    if hashlib.sha256(payload).hexdigest()!=wrapper['sha256']: raise ValueError('Receipt checksum mismatch')
    return event


@contextmanager
def locked_store(state):
    import fcntl
    path = os.fspath(state)
    if not os.path.isabs(path) or '..' in path.split('/'): raise ValueError('Absolute storage path required')
    uid=os.geteuid(); handles=[]
    try:
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);handles.append(fd)
        checked_stat(os.fstat(fd),uid,True,False)
        for part in filter(None,path.split('/')):
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);handles.append(fd)
            checked_stat(os.fstat(fd),uid,True,False)
        checked_stat(os.fstat(fd),uid,True,False)
        try: os.mkdir('calendar-receipts',0o700,dir_fd=fd)
        except FileExistsError: pass
        os.fsync(fd)
        folder=os.open('calendar-receipts',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);handles.append(folder)
        checked_stat(os.fstat(folder),uid,True)
        lock=os.open('.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=folder);handles.append(lock)
        checked_stat(os.fstat(lock),uid)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield folder
    finally:
        for fd in reversed(handles): os.close(fd)


def read_all_locked(folder):
    names=os.listdir(folder)
    if len(names)>MAX_RECORDS+1: raise ValueError('Receipt count limit')
    records=[];total=0
    for name in names:
        if name=='.lock': continue
        if not re.fullmatch(r'[0-9a-f]{32}\.json',name): raise ValueError('Uncommitted/unknown storage artifact')
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
        with os.fdopen(fd,'rb') as source:
            checked_stat(os.fstat(source.fileno()),os.geteuid())
            raw=source.read(MAX_RECORD_BYTES+1)
        total+=len(raw)
        if total>MAX_BYTES: raise ValueError('Receipt byte limit')
        records.append(decode(raw))
    return records,total


def read_receipts(state):
    with locked_store(state) as folder:
        return read_all_locked(folder)[0]


def store(state,event):
    raw=encode(event)
    with locked_store(state) as folder:
        records,total=read_all_locked(folder)
        if len(records)>=MAX_RECORDS or total+len(raw)>MAX_BYTES: raise ValueError('Receipt capacity exhausted')
        identifier=uuid.uuid4().hex; pending=identifier+'.pending';final=identifier+'.json'
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=folder)
        with os.fdopen(fd,'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        # Atomic no-replace publication; crash remnants are rejected, never deleted on retry.
        os.link(pending,final,src_dir_fd=folder,dst_dir_fd=folder,follow_symlinks=False)
        os.fsync(folder)
        os.unlink(pending,dir_fd=folder)
        os.fsync(folder)
        return identifier
