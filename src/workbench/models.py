"""JSON contracts; collectors propose/observe, authenticated operators commit."""
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=500)]
Details = Annotated[str, Field(max_length=10000)]
Level = Annotated[int, Field(ge=0, le=3)]


def now():
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Status(StrEnum):
    observed = 'observed'
    proposed = 'proposed'
    accepted = 'accepted'
    in_progress = 'in_progress'
    waiting = 'waiting'
    approval_needed = 'approval_needed'
    done = 'done'
    rejected = 'rejected'
    canceled = 'canceled'


# No executor or autonomy policy engine exists in this slice. These are advisory
# metadata, never permission to perform a task. Commitment requires an operator.
class ObjectiveIn(Model):
    title: Text
    details: Details = ''
    project: Text | None = None
    autonomy_ceiling: Level = 0


class ActionIn(ObjectiveIn):
    objective_id: str | None = None
    status: Status = Status.proposed
    execution_mode: Literal['human', 'agent', 'assisted', 'external', 'waiting'] = 'human'
    actor: Text = 'Operator'
    priority: Annotated[int, Field(ge=0, le=3)] = 2
    due_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None
    required_level: Level = 0
    source: Text = 'manual'
    source_id: Text | None = None

    @field_validator('due_date')
    @classmethod
    def valid_date(cls, value):
        if value:
            datetime.strptime(value, '%Y-%m-%d')
        return value


class ActionPatch(Model):
    version: Annotated[int, Field(ge=1)]
    title: Text | None = None
    details: Details | None = None
    status: Status | None = None
    execution_mode: Literal['human', 'agent', 'assisted', 'external', 'waiting'] | None = None
    actor: Text | None = None
    priority: Annotated[int, Field(ge=0, le=3)] | None = None
    due_date: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = None

    @field_validator('due_date')
    @classmethod
    def valid_date(cls, value):
        return ActionIn.valid_date(value)


class Transition(Model):
    version: Annotated[int, Field(ge=1)]
    transition: Literal['propose', 'accept', 'start', 'wait', 'request-approval', 'approve', 'reject', 'complete', 'cancel']


class EventIn(Model):
    kind: Literal['observation', 'accomplishment', 'run_observation', 'decision'] = 'observation'
    summary: Text
    details: Details = ''
    project: Text | None = None
    action_id: str | None = None
    run_id: str | None = None
    source: Text
    source_id: Text
    occurred_at: datetime | None = None

    @field_validator('occurred_at')
    @classmethod
    def aware_time(cls, value):
        if value and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError('Timezone is required')
        return value


class RunIn(Model):
    source: Text
    source_id: Text
    context: Text
    provider: Text
    actor: Text
    status: Literal['running', 'waiting', 'approval_needed', 'stopped', 'unknown'] = 'unknown'
    action_id: str | None = None
    objective_id: str | None = None
    started_at: datetime
    last_activity_at: datetime | None = None
    activity_basis: Text = 'process presence only'
    autonomy_ceiling: Level = 0
    required_level: Level = 0

    @field_validator('started_at', 'last_activity_at')
    @classmethod
    def aware_time(cls, value):
        return EventIn.aware_time(value)


Sha256 = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class LegacyRecord(Model):
    legacy_id: Annotated[int, Field(ge=1)]
    row_sha256: Sha256
    source_sha256: Sha256
    disposition: Literal['imported', 'redacted', 'quarantined']
    event: EventIn | None = None
    action: ActionIn | None = None
    metadata: Annotated[str, Field(max_length=5000)] = '{}'

    @field_validator('metadata')
    @classmethod
    def valid_metadata(cls, value):
        import json
        metadata = json.loads(value)
        if not isinstance(metadata, dict):
            raise ValueError('Metadata must be a JSON object')
        original = metadata.get('occurred_at')
        if original is not None:
            if not isinstance(original, str):
                raise ValueError('Original time must be an ISO date or timestamp')
            datetime.fromisoformat(original)
        return value  # Preserve provenance bytes and existing idempotency hashes.


class ImportBatch(Model):
    dataset: Literal['starforge-worklog']
    snapshot_sha256: Sha256
    phase: Literal['events', 'tasks']
    records: Annotated[list[LegacyRecord], Field(max_length=1000)]


class CollectorIn(Model):
    source: Text
    instance_id: Text
    scope: Text
    status: Literal['ok', 'degraded']
    reason: Literal['scan_complete', 'tmux_unavailable', 'scan_failed', 'submission_failed']
    observed_runs: Annotated[int, Field(ge=0)] = 0


ProviderIdentity = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.:-]{1,200}$')]


class ProviderGenerationIn(Model):
    provider: Literal['opencode']
    session_id: ProviderIdentity
    generation_id: ProviderIdentity
    source: Text
    source_instance: ProviderIdentity
    started_at: datetime
    provenance: Literal['opencode.chat.message']

    @field_validator('started_at')
    @classmethod
    def aware_time(cls, value):
        return EventIn.aware_time(value)


class ProviderAttentionIn(Model):
    provider: Literal['opencode']
    session_id: ProviderIdentity
    generation_id: ProviderIdentity
    source: Text
    source_instance: ProviderIdentity
    incident_id: Sha256
    sequence: Annotated[int, Field(ge=1)]
    observed_at: datetime
    reason: Literal['permission_wait', 'user_question', 'provider_error']
    state: Literal['open', 'resolved']
    provenance: Literal['opencode.permission.asked', 'opencode.permission.updated', 'opencode.permission.replied',
                        'opencode.tool.question', 'opencode.question.completed',
                        'opencode.message.error']

    @field_validator('observed_at')
    @classmethod
    def aware_time(cls, value):
        return EventIn.aware_time(value)

    @model_validator(mode='after')
    def matching_provenance(self):
        expected = {
            ('permission_wait', 'open'): 'opencode.permission.updated',
            ('permission_wait', 'resolved'): 'opencode.permission.replied',
            ('user_question', 'open'): 'opencode.tool.question',
            ('user_question', 'resolved'): 'opencode.question.completed',
            ('provider_error', 'open'): 'opencode.message.error',
        }
        # Preserve already queued legacy evidence while new bridges report the runtime event.
        valid_asked = (self.reason, self.state, self.provenance) == ('permission_wait', 'open', 'opencode.permission.asked')
        if not valid_asked and self.provenance != expected.get((self.reason, self.state)):
            raise ValueError('Reason does not match provider provenance')
        return self


class RunPatch(Model):
    version: Annotated[int, Field(ge=1)]
    status: Literal['running', 'waiting', 'approval_needed', 'stopped', 'unknown'] | None = None


class RunLink(Model):
    action_version: Annotated[int, Field(ge=1)]
    run_version: Annotated[int, Field(ge=1)]
    replace_action_id: str | None = None


class ArtifactIn(Model):
    title: Text
    uri: Annotated[str, Field(min_length=1, max_length=2000)]
    action_id: str | None = None
    event_id: str | None = None
    media_type: Text = 'text/uri-list'

    @field_validator('uri')
    @classmethod
    def safe_reference(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in {'https', 'urn'} or parsed.username or parsed.password:
            raise ValueError('Use an HTTPS or URN reference without credentials')
        return value
