# Automatic report suggestions

Workbench can generate and save up to five AI suggestions per dashboard,
standup, accomplishments, and action report. Traffic measurements from #41 are
included in the dashboard evidence. Suggestions are shared by HTML and the
`report_suggestions` field of `/api/dashboard` and `/api/reports/{kind}`. Ask AIW
to include that field when explaining a report. The traffic panel's fixed
comparison questions remain separate from model-generated ideas.

Each suggestion includes a question or opportunity, rationale, next question to
ask AIW, and references to supplied evidence with its original timestamps where
available. A snapshot timestamp covers records such as collector health.
Evidence IDs are validated; this does not establish that the model's reasoning
is correct. Treat output as hypotheses to review, not verified findings. No
suggestion creates a task, executes work, or sends a notification.

## Configure existing Ollama

Generation is unconfigured by default. An operator may configure it after
reviewing the scope of the report summaries. Create a private JSON file owned
by the API service user, mode `0600`, outside the repository:

```json
{
  "model": "YOUR_ALREADY_INSTALLED_MODEL",
  "url": "http://127.0.0.1:11434",
  "interval_seconds": 3600
}
```

Set `WB_REPORT_AI_CONFIG` to its absolute path in the API service environment,
then restart that service during an authorized deployment. The worker runs in
the API process and uses the service's repository; no external process opens
server SQLite. The Ollama origin is relative to the API host. If Ollama lives
elsewhere, use a private authenticated network arrangement or a local tunnel;
remote origins require HTTPS. This initial adapter has no bearer-key option.
The worker never downloads models or opens provider sessions. It uses Ollama's
[structured generation endpoint](https://docs.ollama.com/api/generate), without
tools, with a 1,800-token output limit and unload-after-request preference.

The worker checks once per minute, serially, with a database lease per report.
It generates on startup and changed evidence, subject to the configured minimum
interval (at least five minutes; default one hour). Unchanged evidence is reused
for up to one day. At most four calls occur in a normal sweep, each with a
bounded response size and timeout. A crashed attempt retains its lease and
backoff, so startup does not immediately replay it. A request accepted by Ollama
before a crash may consume compute again on a later retry; there are no external
delivery side effects or exactly-once inference guarantee.

## Evidence and failure behavior

Inputs contain at most 20 records per category and ten traffic properties:
short action titles/statuses, accomplishment or recent-event summaries,
collector health categories, and aggregate traffic values. Free text is bounded;
raw event details, provider transcripts, credentials, and tool payloads are not
included. These are partial reports, not a complete project history. Text is
untrusted data in the prompt. The model has no action tools, its output is
schema-checked, unsupported evidence references are rejected, and HTML escapes
all returned text. Identical suggestion titles within a result are deduplicated;
semantic duplicates across different reports are not automatically reconciled.

Factual reports never wait for inference. They show unavailable status before
the first saved result, stale status after evidence changes or a day passes,
and an explicit generation failure if an attempt fails. Last successful output
is retained on failure. This says nothing about agent progress. Backend error
bodies and rejected model output are not stored or logged. Settings changes take
effect when the API service restarts; changing the model prompts regeneration
subject to the normal interval.

Saved output occupies one row per report in migration 006, separate from actions
and attention alerts. Reports continue to render if generation is unconfigured
or Ollama is unavailable. Tests use synthetic evidence and mocked delivery;
a real model quality check and deployment are separate steps.

An operator can force a refresh with
`POST /api/reports/{kind}/suggestions/refresh`, including `dashboard` as a kind.
It queues work for the next worker sweep, retains prior output, and never breaks
an active generation lease. Unconfigured generation returns 503. The bounded
structured-output task disables model thinking so its token allowance is used
for the requested JSON rather than hidden reasoning.
