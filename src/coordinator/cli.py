"""Noninteractive first-class CLI; every normal command uses CoordinatorClient."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from pydantic import ValidationError

from .client import CoordinatorClient, CoordinatorError
from .contracts import JobSpec

EXIT = {"usage": 2, "not_found": 4, "conflict": 5, "unavailable": 6,
        "supervisor_unavailable": 6, "http_error": 7}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord", description="Local coordinator API client; watch detach never cancels a job")
    p.add_argument("--socket", default=os.environ.get("COORDINATOR_SOCKET", ""), help="protected local Unix socket")
    p.add_argument("--timeout", type=float, default=10, help="request timeout in seconds (0 < t <= 60)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("health", "capabilities", "capacity"):
        sub.add_parser(name)
    for name in ("validate", "submit"):
        s = sub.add_parser(name)
        s.add_argument("spec", type=Path, help="JSON JobSpec file")
        if name == "submit":
            s.add_argument("--key", required=True, help="stable idempotency key; reuse after uncertain response")
    s = sub.add_parser("list")
    s.add_argument("--limit", type=int, default=100)
    for name in ("inspect", "attempts", "recover", "events", "watch", "cancel", "retry", "reattach"):
        s = sub.add_parser(name)
        s.add_argument("job_id")
        if name in {"events", "watch"}:
            s.add_argument("--after-seq", type=int, default=0)
            s.add_argument("--limit", type=int, default=100)
        if name in {"cancel", "retry", "reattach"}:
            s.add_argument("--expected-version", type=int, required=True)
            s.add_argument("--key", required=True)
    for name in ("logs", "follow", "artifacts", "artifact"):
        s = sub.add_parser(name)
        s.add_argument("job_id")
        s.add_argument("attempt_id")
        if name in {"logs", "follow"}:
            s.add_argument("--cursor", type=int, default=0)
            s.add_argument("--limit", type=int, default=100)
        if name == "artifact":
            s.add_argument("artifact_id")
    return p


def _spec(path: Path) -> JobSpec:
    if path.stat().st_size > 65_536 + 4096:
        raise ValueError("spec file exceeds limit")
    return JobSpec.model_validate_json(path.read_bytes())


def _emit(value, *, json_mode: bool):
    # Structured output is always JSON; --json guarantees compact one-line output.
    print(json.dumps(value, sort_keys=True, separators=(",", ":") if json_mode else None))


def run(args, client: CoordinatorClient) -> int:
    cmd = args.command
    if cmd in {"validate", "submit"}:
        spec = _spec(args.spec)
        value = client.validate(spec) if cmd == "validate" else client.submit(spec, args.key)
    elif cmd == "health":
        value = client.health()
    elif cmd == "capabilities":
        value = client.capabilities()
    elif cmd == "capacity":
        value = client.capacity()
    elif cmd == "list":
        value = client.list(args.limit)
    elif cmd == "inspect":
        value = client.get(args.job_id)
    elif cmd == "attempts":
        value = client.attempts(args.job_id)
    elif cmd == "recover":
        value = client.recovery(args.job_id)
    elif cmd in {"cancel", "retry", "reattach"}:
        value = getattr(client, cmd)(args.job_id, args.expected_version, args.key)
    elif cmd in {"events", "watch"}:
        cursor = args.after_seq
        while True:
            value = client.events(args.job_id, after_seq=cursor, limit=args.limit)
            _emit(value, json_mode=args.json)
            cursor = value["next_cursor"]
            if cmd == "events":
                return 0
            time.sleep(1)
    elif cmd in {"logs", "follow"}:
        cursor = args.cursor
        while True:
            value = client.logs(args.job_id, args.attempt_id, cursor=cursor, limit=args.limit)
            _emit(value, json_mode=args.json)
            cursor = value.get("next_cursor", cursor)
            if cmd == "logs":
                return 0
            time.sleep(1)
    elif cmd == "artifacts":
        value = client.artifacts(args.job_id, args.attempt_id)
    else:
        value = client.artifact(args.job_id, args.attempt_id, args.artifact_id)
    _emit(value, json_mode=args.json)
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not args.socket:
        print("coord: --socket or COORDINATOR_SOCKET is required", file=sys.stderr)
        return EXIT["usage"]
    try:
        with CoordinatorClient(args.socket, timeout=args.timeout) as client:
            return run(args, client)
    except (ValueError, ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"coord: {exc}", file=sys.stderr)
        return EXIT["usage"]
    except CoordinatorError as exc:
        print(f"coord: {exc}", file=sys.stderr)
        return EXIT.get(exc.code, EXIT["http_error"])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
