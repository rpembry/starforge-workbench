"""Small read projections for the foreground status view; no provider polling here."""
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .attention_projection import read as read_attention
from .repository import Problem


class ManualAllowance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(min_length=1, max_length=80)
    product: str = Field(min_length=1, max_length=80)
    profile: str = Field(min_length=1, max_length=80)
    bucket: str = Field(min_length=1, max_length=80)
    window: str = Field(min_length=1, max_length=80)
    remaining_percent: float | None = Field(default=None, allow_inf_nan=False)
    reset_at: datetime | None = None
    observed_at: datetime

    @field_validator('reset_at', 'observed_at')
    @classmethod
    def aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError('timezone offset is required')
        return value


def record_manual(repository, body: ManualAllowance):
    data = body.model_dump()
    if data['observed_at'] > datetime.now(timezone.utc):
        raise Problem(422, 'future_observation', 'Observation time cannot be in the future')
    data['observed_at'] = data['observed_at'].astimezone(timezone.utc).isoformat()
    data['reset_at'] = data['reset_at'].astimezone(timezone.utc).isoformat() if data['reset_at'] else None
    data.update(id=str(uuid4()), received_at=datetime.now(timezone.utc).isoformat(), source_kind='manual')
    with repository.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        key = (data['provider'], data['product'], data['profile'], data['bucket'], data['window'], data['observed_at'])
        existing = db.execute('''SELECT * FROM allowance_observations WHERE provider=? AND product=?
            AND profile=? AND bucket=? AND window=? AND observed_at=?''', key).fetchone()
        if existing:
            if existing['remaining_percent'] != data['remaining_percent'] or existing['reset_at'] != data['reset_at']:
                raise Problem(409, 'observation_conflict', 'An observation at this time already exists')
            return dict(existing)
        db.execute('''INSERT INTO allowance_observations
            (id,provider,product,profile,bucket,window,remaining_percent,reset_at,observed_at,received_at,source_kind)
            VALUES (:id,:provider,:product,:profile,:bucket,:window,:remaining_percent,:reset_at,:observed_at,:received_at,:source_kind)''', data)
        db.commit()
    return data


def read_widget(repository, widget: str):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    with repository.connection() as db:
        if widget == 'allowances':
            rows = [dict(row) for row in db.execute('''SELECT * FROM allowance_observations o WHERE NOT EXISTS
                (SELECT 1 FROM allowance_observations newer WHERE newer.provider=o.provider
                 AND newer.product=o.product AND newer.profile=o.profile AND newer.bucket=o.bucket
                 AND newer.window=o.window AND newer.observed_at>o.observed_at)
                ORDER BY provider,product,profile,bucket,window LIMIT 100''')]
            for row in rows:
                age = (now - datetime.fromisoformat(row['observed_at'])).total_seconds()
                row['freshness'] = 'stale' if age > 3600 else 'manual'
            return {'checked_at': stamp, 'source': 'manual observation', 'items': rows,
                    'availability': 'unknown' if not rows else 'available'}
        if widget == 'hosts':
            rows = [dict(row) for row in db.execute('SELECT id,source,status,reason,heartbeat_at,last_success_at FROM collectors ORDER BY source LIMIT 100')]
            for row in rows:
                age = (now - datetime.fromisoformat(row['heartbeat_at'])).total_seconds()
                row['freshness'] = 'disconnected' if age > 90 else 'fresh'
            return {'checked_at': stamp, 'source': 'Workbench collectors', 'items': rows,
                    'availability': 'unknown' if not rows else 'available'}
        if widget == 'jobs':
            rows = [dict(row) for row in db.execute('''SELECT id,context,provider,status,started_at,heartbeat_at,last_activity_at
                FROM runs WHERE status IN ('running','waiting','approval_needed') ORDER BY heartbeat_at DESC LIMIT 100''')]
            for row in rows:
                age = (now - datetime.fromisoformat(row['heartbeat_at'])).total_seconds()
                row['freshness'] = 'stale' if age > 90 else 'fresh'
            return {'checked_at': stamp, 'source': 'Workbench runs', 'items': rows, 'availability': 'available'}
        if widget == 'attention':
            result = read_attention(db, now)
            return {'checked_at': stamp, 'source': 'Workbench attention', 'items': result['items'][:100],
                    'truncated': len(result['items']) > 100, 'availability': 'available'}
    raise Problem(404, 'not_found', 'Unknown status widget')
