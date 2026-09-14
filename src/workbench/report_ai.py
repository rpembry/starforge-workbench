"""Optional Ollama generation inside the API service, outside request handlers."""
import json
import os
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .report_suggestions import KINDS, SYSTEM, Suggestions, digest, snapshot, validate_output


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    model: str = Field(min_length=1, max_length=200)
    url: str = 'http://127.0.0.1:11434'
    interval_seconds: int = Field(default=3600, ge=300, le=86400)

    @field_validator('url')
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if (url.username or url.password or url.query or url.fragment or
            not url.hostname or url.path not in ('', '/') or
            url.scheme not in ('http', 'https')):
            raise ValueError('Use a credential-free Ollama origin')
        if url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Remote Ollama must use HTTPS')
        return value.rstrip('/')


def load_settings():
    name = os.environ.get('WB_REPORT_AI_CONFIG')
    if not name:
        return None
    path = Path(name)
    try:
        info = path.stat()
        if (not path.is_absolute() or path.is_symlink() or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > 10000):
            raise ValueError('Configuration must be an owned private file')
        return Settings.model_validate_json(path.read_text())
    except (OSError, ValueError):
        raise RuntimeError('Invalid private report AI configuration') from None


def generate(settings, evidence):
    # No model pull, tools, provider sessions, or shell commands. Ollama must already have the model.
    body = dict(model=settings.model, system=SYSTEM,
                prompt=json.dumps(evidence, sort_keys=True), format=Suggestions.model_json_schema(),
                stream=False, think=False, keep_alive=0, options={'num_predict': 1800, 'num_ctx': 16384, 'temperature': 0.2})
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False) as api:
        with api.stream('POST', settings.url+'/api/generate', json=body) as response:
            response.raise_for_status()
            output = bytearray()
            for part in response.iter_bytes():
                output.extend(part)
                if len(output) > 128000 or time.monotonic()-started > 120:
                    raise ValueError('Generation exceeded limits')
    result = json.loads(output)
    if result.get('done') is not True or result.get('done_reason') == 'length':
        raise ValueError('Incomplete generation')
    return json.loads(result['response'])


def report_data(repo, kind, zone):
    if kind == 'dashboard':
        return repo.dashboard()
    from .reports import report
    return report(repo, kind, zone=zone)


def tick(repo, settings, zone, generator=generate, clock=time.time, stop=None):
    for kind in KINDS:
        if stop and stop.is_set(): return
        data = snapshot(kind, report_data(repo, kind, zone))
        identity = digest(data)
        stamp = clock()
        token = str(uuid.uuid4())
        with repo.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO report_suggestions(kind) VALUES (?)', (kind,))
            row = db.execute('SELECT * FROM report_suggestions WHERE kind=?', (kind,)).fetchone()
            if (row['lease_until'] > stamp or row['next_attempt'] > stamp or
                (row['snapshot_hash'] == identity and row['result'] and
                 json.loads(row['result']).get('model') == settings.model and stamp-row['completed_at'] < 86400)):
                db.commit()
                continue
            # Persist before inference. Crashes wait out the lease instead of immediate replay.
            db.execute('UPDATE report_suggestions SET lease_until=?,lease_id=?,next_attempt=?,failure=0 WHERE kind=?',
                       (stamp+300, token, stamp+settings.interval_seconds, kind))
            db.commit()
        try:
            items = validate_output(generator(settings, data), data['evidence']) if data['evidence'] else []
            result = dict(items=items, generated_at=datetime.fromtimestamp(clock(), timezone.utc).isoformat(),
                          snapshot_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(), model=settings.model, snapshot_hash=identity,
                          window=data['window'], basis='model' if data['evidence'] else 'no_evidence')
            # Save the input hash; readers compare against current evidence and label old results stale.
            with repo.connection() as db:
                db.execute('UPDATE report_suggestions SET result=?,snapshot_hash=?,completed_at=?,lease_until=0,failure=0 '
                           'WHERE kind=? AND lease_id=?', (json.dumps(result), identity, clock(), kind, token))
                db.commit()
        except Exception:
            # Never store model output, exception text, credentials, or request bodies as diagnostics.
            with repo.connection() as db:
                db.execute('UPDATE report_suggestions SET lease_until=0,failure=1 WHERE kind=? AND lease_id=?', (kind,token))
                db.commit()


def start(repo, settings, zone):
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                tick(repo, settings, zone, stop=stop)
            except Exception:
                pass  # A transient database failure must not stop factual reporting.
            stop.wait(60)
    thread = threading.Thread(target=loop, name='report-ai', daemon=True)
    thread.start()
    return stop, thread
