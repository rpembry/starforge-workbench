"""Reference offline protocol worker; paths and identity come from host plan.

Example invocation in a reviewed Python image:
  python /opt/coordinator-worker.py JOB ATTEMPT INCARNATION
The host mounts one attempt's input file, event socket, and writable scratch.
"""

import json
from pathlib import Path
import sys

from worker_sdk import WorkerClient


def main():
    job_id, attempt_id, incarnation = sys.argv[1:4]
    client = WorkerClient("/channel/events.sock", "/scratch/worker-sender.json",
                          job_id=job_id, attempt_id=attempt_id, incarnation=incarnation)
    if client.state["pending"]:
        client.retry_pending()
    client.send("hello", {"capabilities": []})
    payload = json.loads(Path("/input/job.json").read_text())
    client.send("ready", {})
    client.send("heartbeat", {})
    result = {"value": payload.get("value")}
    Path("/scratch/result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    client.send("progress", {"completed": 1, "total": 1})
    client.send("artifact", {"path": "result.json"})
    client.send("result", {"ok": True, "summary": "fixture completed"})


if __name__ == "__main__":
    main()
