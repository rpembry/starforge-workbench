"""Versioned public coordinator models. References are policy keys, never paths."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResourceRequest(StrictModel):
    cpu_millis: int = Field(ge=1, le=256_000)
    memory_mb: int = Field(ge=16, le=1_048_576)


class OrphanPolicy(StrictModel):
    mode: Literal["strict", "trusted_local"] = "strict"
    grace_seconds: int = Field(default=10, ge=0, le=300)
    max_orphan_seconds: int = Field(default=0, ge=0, le=86_400)

    @model_validator(mode="after")
    def valid_budget(self):
        if self.mode == "strict" and self.max_orphan_seconds:
            raise ValueError("strict policy cannot continue an orphan")
        if self.mode == "trusted_local" and not self.max_orphan_seconds:
            raise ValueError("trusted-local continuation needs a bounded budget")
        return self


class JobSpec(StrictModel):
    api_version: Literal["coordinator.job.v1"] = "coordinator.job.v1"
    worker_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    worker_contract: Literal["worker.v1"] = "worker.v1"
    profile_ref: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    workspace_ref: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    payload: dict
    deadline_seconds: int = Field(ge=1, le=86_400)
    resources: ResourceRequest
    orphan_policy: OrphanPolicy = Field(default_factory=OrphanPolicy)
    consumer_ref: str | None = Field(default=None, max_length=256)
    parent_ref: str | None = Field(default=None, max_length=256)

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, payload):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 65_536:
            raise ValueError("payload exceeds 64 KiB")
        return payload


class Limits(StrictModel):
    max_pending: int = Field(ge=1, le=100_000)
    max_active: int = Field(ge=1, le=100_000)
    cpu_millis: int = Field(ge=1)
    memory_mb: int = Field(ge=16)


class Policy(StrictModel):
    profiles: frozenset[str]
    workspaces: frozenset[str]
    worker_types: frozenset[str]
    limits: Limits

    def validate_spec(self, spec: JobSpec):
        if spec.profile_ref not in self.profiles:
            raise ValueError("unapproved profile")
        if spec.workspace_ref not in self.workspaces:
            raise ValueError("unapproved workspace")
        if spec.worker_type not in self.worker_types:
            raise ValueError("unregistered worker type")
        if (spec.resources.cpu_millis > self.limits.cpu_millis or
                spec.resources.memory_mb > self.limits.memory_mb):
            raise ValueError("request exceeds coordinator budget")
