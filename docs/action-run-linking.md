# Explicit action-to-run linking

Run association is an operator decision, not a collector inference. Use exact IDs
from current API records; do not select by title, directory, provider, or recent
activity. If a query leaves zero or multiple candidates, stop and ask the operator
to identify the intended action and run.

Read both records immediately before linking:

```sh
uv run wb-api GET /api/actions/ACTION_ID
uv run wb-api GET /api/runs/RUN_ID
```

Confirm that the action is a committed agent action and that the run identity,
`source_id`, `started_at`, status, and heartbeat describe the intended process
generation. Put the two returned versions and no other inferred identity into a
private temporary JSON file, then submit:

```json
{"action_version": 3, "run_version": 7}
```

```sh
uv run wb-api POST /api/actions/ACTION_ID/runs/RUN_ID/link --json-file /path/to/private/link.json
```

The response returns the unchanged action, updated run, and immutable decision
event. Linking changes no action/run status, starts no process, grants no authority,
and marks nothing complete. Ordinary collector heartbeats preserve the link when
they replay their source observation without an `action_id`.

A collector cannot attach an action to a newly observed process generation. Read
and explicitly link that new run ID. A run already assigned elsewhere returns
`assignment_conflict`; after verifying all three exact records, reassignment must
name the current action in `replace_action_id`. Stale versions, stale/stopped runs,
and non-agent or uncommitted actions are rejected. A stale or stopped run that was
linked while fresh remains linked as history, but attention correctly treats it as
not fresh active progress.
