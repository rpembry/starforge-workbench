#!/usr/bin/env python3
"""Sanitized helper for the issue-61 OpenCode delivery smoke.

This stdlib-only helper binds and probes loopback HTTP only. Probe output and
optional evidence files never include raw request or response bodies. They are
limited to structural delivery evidence: HTTP status, synthetic IDs, record
roles/types, admission sequence, part counts/types, and whether an assistant
record has an error.

Commands:
  python3 deploy/oc-delivery-smoke.py mock [PORT] [DELAY_MS]
  python3 deploy/oc-delivery-smoke.py write-config DIR [PORT]
  python3 deploy/oc-delivery-smoke.py probe CMD PATH [PAYLOAD.json [OUTPUT.json]]

CMD is get, post, post-empty, delete, patch, or sse. PROBE_BASE defaults to
http://127.0.0.1:4098 and PROBE_TIMEOUT defaults to 120 seconds.
"""

import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import urlparse


MOCK_PORT = 4097
SERVER_PORT = 4098
DEFAULT_BASE = f"http://127.0.0.1:{SERVER_PORT}"
MAX_BODY_BYTES = 1 << 20
MAX_EVENTS = 100
COMPLETIONS = (
    ("synthetic-ack", " synthetic-token-a", " synthetic-token-b"),
    ("answer-one", " from-mock-provider"),
    ("answer-two", " from-mock-provider"),
)
SAFE_KEYS = {
    "_tag",
    "admittedSeq",
    "delivery",
    "id",
    "isRetryable",
    "messageID",
    "name",
    "parentID",
    "role",
    "sessionID",
    "status",
    "statusCode",
    "type",
    "version",
}
SEQUENCE = 0
LOCK = threading.Lock()
STARTED = time.time()
DELAY_MS = 4000


def _port(value):
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return result


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        elapsed = round((time.time() - STARTED) * 1000)
        sys.stderr.write(f"[{self.server.server_port} +{elapsed}ms] {fmt % args}\n")

    def _send_json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_request(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("request body exceeds synthetic harness limit")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _next_completion(self):
        global SEQUENCE
        with LOCK:
            index = SEQUENCE
            SEQUENCE += 1
        return index, COMPLETIONS[index % len(COMPLETIONS)]

    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "spike-model",
                            "object": "model",
                            "created": int(STARTED),
                            "owned_by": "spike",
                        }
                    ],
                }
            )
            return
        self._send_json(
            {"error": {"message": "not found", "type": "invalid_request_error"}},
            404,
        )

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/v1/responses":
                self._responses()
                return
            if path == "/v1/chat/completions":
                self._chat()
                return
        except ValueError as error:
            self._send_json(
                {"error": {"message": str(error), "type": "invalid_request_error"}},
                413,
            )
            return
        self._send_json(
            {"error": {"message": "not found", "type": "invalid_request_error"}},
            404,
        )

    def _delay(self):
        time.sleep(DELAY_MS / 1000)

    def _chat(self):
        request = self._read_request()
        index, tokens = self._next_completion()
        response_id = f"chatcmpl-spike-{index}"
        model = request.get("model", "spike-model")
        self._delay()
        if not request.get("stream"):
            self._send_json(
                {
                    "id": response_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "".join(tokens),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                }
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for token_index, token in enumerate(tokens):
            delta = {"content": token}
            if token_index == 0:
                delta["role"] = "assistant"
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.4)
        done = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        }
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()

    def _responses(self):
        request = self._read_request()
        index, tokens = self._next_completion()
        text = "".join(tokens)
        response_id = f"resp_spike_{index}"
        item_id = f"msg_spike_{index}"
        model = request.get("model", "spike-model")
        self._delay()
        created = int(time.time())
        completed_item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": text, "annotations": []}
            ],
        }
        if not request.get("stream"):
            self._send_json(
                {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "completed",
                    "model": model,
                    "output": [completed_item],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "total_tokens": 7,
                    },
                }
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        events = [
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "model": model,
                    "status": "in_progress",
                    "output": [],
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    **completed_item,
                    "status": "in_progress",
                    "content": [],
                },
            },
            {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": completed_item,
            },
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "completed",
                    "model": model,
                    "output": [completed_item],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "total_tokens": 7,
                    },
                },
            },
        ]
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.3)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_base():
    base = os.environ.get("PROBE_BASE", DEFAULT_BASE).rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("PROBE_BASE must be an unauthenticated loopback HTTP origin")
    return base


def _read_bounded(stream):
    body = stream.read(MAX_BODY_BYTES + 1)
    return body[:MAX_BODY_BYTES], len(body) > MAX_BODY_BYTES


def _request(method, path, payload, base, timeout):
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=body, headers=headers, method=method
    )
    opener = urllib.request.build_opener(NoRedirect)
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw, truncated = _read_bounded(response)
            status = response.status
            transport = "response"
    except urllib.error.HTTPError as error:
        raw, truncated = _read_bounded(error)
        status = error.code
        transport = "response"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raw, truncated = b"", False
        status = None
        reason = getattr(error, "reason", None)
        is_timeout = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
        transport = "timeout" if is_timeout else "error"
    elapsed = round((time.monotonic() - started) * 1000)
    return status, raw, elapsed, truncated, transport


def _safe_error(value):
    if not isinstance(value, dict):
        return {"present": True}
    result = {"present": True}
    if isinstance(value.get("name"), str):
        result["name"] = value["name"]
    data = value.get("data")
    if isinstance(data, dict):
        for key in ("statusCode", "isRetryable"):
            if isinstance(data.get(key), (str, int, bool)):
                result[key] = data[key]
    return result


def _safe_part(value):
    if not isinstance(value, dict):
        return {"kind": type(value).__name__}
    return {
        key: value[key]
        for key in ("id", "messageID", "sessionID", "type")
        if isinstance(value.get(key), (str, int, bool))
    }


def _safe_json(value):
    if isinstance(value, list):
        return {
            "kind": "array",
            "count": len(value),
            "items": [_safe_json(item) for item in value[:MAX_EVENTS]],
            "truncated": len(value) > MAX_EVENTS,
        }
    if not isinstance(value, dict):
        return {"kind": type(value).__name__}

    result = {
        key: value[key]
        for key in SAFE_KEYS
        if isinstance(value.get(key), (str, int, bool))
    }
    if isinstance(value.get("time"), dict):
        result["record_completed"] = "completed" in value["time"]
    if "error" in value:
        result["error"] = _safe_error(value["error"])
    if isinstance(value.get("parts"), list):
        parts = value["parts"]
        result["parts"] = {
            "count": len(parts),
            "items": [_safe_part(part) for part in parts[:MAX_EVENTS]],
            "truncated": len(parts) > MAX_EVENTS,
        }
    for key in ("info", "data"):
        if isinstance(value.get(key), (dict, list)):
            result[key] = _safe_json(value[key])

    role = result.get("role")
    if role == "assistant":
        if "error" in result:
            result["assistant_record_outcome"] = "error"
        elif result.get("record_completed"):
            result["assistant_record_outcome"] = "completed_without_error"
        else:
            result["assistant_record_outcome"] = "not_completed"
    return result or {"kind": "object", "safe_fields": []}


def _summarize_body(raw, truncated):
    if not raw:
        return {"kind": "empty", "truncated": truncated}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "kind": "non_json",
            "bytes_observed": len(raw),
            "truncated": truncated,
        }
    result = _safe_json(value)
    result["truncated_bytes"] = truncated
    return result


def _stream(path, base, timeout):
    request = urllib.request.Request(
        base + path, headers={"Accept": "text/event-stream"}, method="GET"
    )
    opener = urllib.request.build_opener(NoRedirect)
    started = time.monotonic()
    status = None
    events = []
    stream_end = "eof"
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            for raw_line in response:
                if not raw_line.startswith(b"data:"):
                    continue
                data = raw_line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    event = None
                if event is not None and len(events) < MAX_EVENTS:
                    events.append(_safe_json(event))
                if len(events) >= MAX_EVENTS:
                    stream_end = "event_limit"
                    break
    except urllib.error.HTTPError as error:
        status = error.code
        stream_end = "http_error"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None)
        is_timeout = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
        stream_end = "timeout" if is_timeout else "error"
    elapsed = round((time.monotonic() - started) * 1000)
    return status, events, elapsed, stream_end


def cmd_mock(args):
    global DELAY_MS
    port = _port(args[0]) if args else MOCK_PORT
    DELAY_MS = int(args[1]) if len(args) > 1 else 4000
    if DELAY_MS < 0:
        raise SystemExit("delay must be non-negative")
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    print(f"mock provider listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


def cmd_write_config(args):
    if not args:
        raise SystemExit("write-config requires a fresh destination directory")
    destination = Path(args[0])
    port = _port(args[1]) if len(args) > 1 else MOCK_PORT
    if destination.exists() and destination.is_symlink():
        raise SystemExit("refusing symlink destination")
    config_dir = destination / "opencode"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = config_dir / "opencode.json"
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "openai": {
                "options": {
                    "baseURL": f"http://127.0.0.1:{port}/v1",
                    "apiKey": "sk-synthetic-spike",
                }
            }
        },
        "experimental": {"disableSharing": True, "disableModelCache": True},
    }
    descriptor = os.open(
        config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    print(config_path)


def cmd_probe(args):
    if len(args) < 2:
        raise SystemExit("probe requires CMD and a root-relative PATH")
    command, path = args[0], args[1]
    methods = {
        "get": "GET",
        "post": "POST",
        "post-empty": "POST",
        "delete": "DELETE",
        "patch": "PATCH",
    }
    if command not in {*methods, "sse"}:
        raise SystemExit("unknown probe command")
    if not path.startswith("/") or path.startswith("//"):
        raise SystemExit("probe path must be root-relative")
    base = _safe_base()
    timeout = float(os.environ.get("PROBE_TIMEOUT", "120"))
    if timeout <= 0:
        raise SystemExit("PROBE_TIMEOUT must be positive")

    payload = None
    if command in {"post", "patch"}:
        if len(args) < 3:
            raise SystemExit(f"{command} requires a JSON payload file")
        with open(args[2], encoding="utf-8") as source:
            payload = json.load(source)

    if command == "sse":
        status, events, elapsed, stream_end = _stream(path, base, timeout)
        result = {
            "method": "GET",
            "path": path,
            "status": status,
            "elapsed_ms": elapsed,
            "stream_end": stream_end,
            "events": events,
            "event_count": len(events),
        }
    else:
        method = methods[command]
        status, raw, elapsed, truncated, transport = _request(
            method,
            path,
            None if command in {"get", "post-empty", "delete"} else payload,
            base,
            timeout,
        )
        result = {
            "method": method,
            "path": path,
            "status": status,
            "elapsed_ms": elapsed,
            "transport": transport,
            "body_summary": _summarize_body(raw, truncated),
        }

    output_index = 3 if command in {"post", "patch"} else 2
    if len(args) > output_index:
        with open(args[output_index], "x", encoding="utf-8") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
    print(json.dumps(result, indent=2, sort_keys=True))


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    command, args = sys.argv[1], sys.argv[2:]
    if command == "mock":
        cmd_mock(args)
    elif command == "write-config":
        cmd_write_config(args)
    elif command == "probe":
        cmd_probe(args)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
