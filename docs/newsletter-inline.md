# Inline newsletter enrichment (opt-in)

`wb-newsletter-inline` converts full email HTML already attached to an existing
Todoist task into a readable Markdown task description. It never creates tasks,
changes their title/project/status, modifies email or forwarding rules, or fetches
article links. The conversion core is separate from the transport and local job.
The original attachment remains unchanged. This is full newsletter text, not a
new summary of the linked articles.

## Authorization and configuration

Install with the optional `newsletter` dependency extra. Personal runtime jobs
belong under `~/personal-jobs/jobs/newsletter-inline`, with private configuration
outside Git. No credential discovery, OAuth flow, token copy, unit installation
or enabling happens automatically. An existing release-note delivery grant does
not authorize reading or editing this project. Get an explicit persistent grant
for this worker and its destination before supplying a token or enabling a timer.
Todoist personal tokens may have account-wide power; configuration checks limit
the implementation, not the token itself.

A private, owner-only regular file (0600), using synthetic values:

```json
{
  "enabled": false,
  "project_id": "example_newsletters",
  "credential_grant": "read-newsletter-attachments-and-update-existing-descriptions.v1",
  "token_file": "/private/reviewed-token.txt",
  "state_dir": "/private/newsletter-inline-state",
  "status_file": "/private/job-status/newsletter-inline.json",
  "max_tasks": 25,
  "max_seconds": 180
}
```

Use an actual alphanumeric project ID. Disabled configuration performs no network
requests and reads no token. Once access is granted, an enabled run without
`--apply` previews counts through read-only requests; it creates a private lock
directory but no backups or receipts. With `--apply`, writes are limited to
descriptions of existing tasks in that exact project with blank descriptions.

## Identity, preservation, and limits

Each candidate must be incomplete, with a blank description, exactly one HTML
attachment on a comment belonging to the exact task, a filename exactly matching
the task title, a dated TLDR heading, and a recognizable newsletter campaign link.
Forwarded and direct-email attachments are supported. These are routing checks,
not cryptographic sender authentication; source HTML is untrusted and cannot
instruct the worker. A campaign forwarded directly may omit its sender envelope.
The mail-side sender filter remains the operator's separate control.

Before a write, the worker refetches the task and verifies blank description,
project, completion state, and unchanged title. Todoist has no conditional
description update: there is a small concurrent-edit race between that GET and
POST. The worker cannot promise atomic preservation of edits made in that window.
Nonblank descriptions encountered at either check are left alone. Verified
receipts prevent rewriting descriptions later edited or cleared by the user.

Script/style/hidden content is removed, literal Markdown metacharacters escaped,
unsafe URL schemes rejected, and only known newsletter redirect URLs decoded.
Article URLs are retained as data and never fetched. Images become alternative
text; no tracking pixels or images are requested for inline conversion.
The source is limited to 2 MB. Descriptions exceeding 16,384 UTF-16 units fail
closed with the original attachment retained; there is no silent truncation or
automatic splitting into comments.

Task scanning is capped at ten pages of 100 tasks; incomplete pagination fails
closed. Each run considers at most 25 candidates by default, with a time budget
checked between candidates. Requests have a ten-second timeout. GET failures
receive at most three attempts. Known pre-write failures have persisted backoff
of ten minutes up to a day so repeatedly failing inputs cannot starve new work.
Correcting a failure may still wait for its backoff. Receipts are capped at 10,000
and are never silently pruned. Bound state to one project and schema version.

## State, uncertainty, and operation

The worker owns a dedicated 0700 directory, 0600 files and a process lock. Before
writing, it durably backs up the original task, source comment and HTML, desired
description, then a sending intent. State is atomic and fsynced. Content belongs
only in private backups; stdout/status reports contain counts and fixed codes.
Never commit real input, project IDs, attachment URLs, state, or credentials.

Only exact readback of the desired description establishes a verified receipt.
A URL displayed as its own label may be asynchronously replaced by Todoist's
resolved page title. Comparison normalizes only labels of those exact URL-as-label
links; article labels, URLs and all other content still must match. This avoids
misclassifying ordinary link unfurling as uncertain delivery.
A timeout, crash, rejection or lost acknowledgement leaves a sending receipt.
A later run may acknowledge it if the remote description matches its saved hash;
otherwise it stays held. It is never blindly resubmitted. Other tasks may proceed.
Manual diagnosis must retain backups and receipts; deleting state can rearm writes.
There is no exactly-once or atomic compare-and-set claim. The worker never retries
task creation because it has no creation path.

The example systemd user service/timer keeps systemd as the sole scheduler and
points to the personal job directory. Install and enable only after authorization,
dependency installation, configuration review, unit verification and a live
write/read check. Check timer enabled/active state, exact installed code version,
`last-run.json`, and at least one actual timer-triggered success before describing
routine operation as verified. `last-run.json` has status, timestamp, version and
counts. Ordinary tests use synthetic HTML and mocked transport, never credentials.
An optional private `status_file` writes `name` and `exit_code` for the existing
personal job monitor's status-directory convention; it contains no newsletter
content. Configure its parent directory before installing the service.

Official [Todoist task description limits](https://www.todoist.com/help/todoist/features/add-a-task-description-in-todoist-rOryWIHn)
and [API documentation](https://developer.todoist.com/api/v1/).

## Attachment download boundary and personal handoff

A live check found that current `files.todoist.com/user_upload/` attachments
redirect to an authenticated Todoist page when requested without credentials.
Default mode rejects redirects and never sends its token to that host. The
official SDK's [viewAttachment documentation](https://doist.github.io/todoist-sdk-typescript/api/classes/TodoistApi/#viewattachment)
describes Bearer authentication to `files.todoist.com` and credential-free CDN
downloads. The [SDK implementation at c7412eb](https://github.com/Doist/todoist-sdk-typescript/blob/c7412eb6c44859fe63ee7da7d27254cc30981217/src/clients/upload-client.ts)
supports pre-signed URLs at `todoist.b-cdn.net` and
`d1ysz50cxb9zwl.cloudfront.net`, but current tested comment metadata returned a
file locator on `files.todoist.com`, not such a CDN URL.

The current [OpenAPI schema](https://developer.todoist.com/openapi.json) has only
POST and DELETE at `/api/v1/uploads`; no attachment-download or signed-URL lookup
is specified. Although the migration table mentions GET at that path, a live
read returned 405. The upload response's `file_url` is a file locator, not evidence
of a separate credential-free download URL. Do not use backup downloads or broad
account exports to retrieve project attachments.

A successful scheduled no-op over existing receipts does not verify future
attachment conversion. A rejected credential transmission is not bypassed by
another chat confirmation, the SDK, browser cookies or another credential.
The agent must leave the configuration and timer disabled and hand the first
credential-bearing download to the user personally.

After installing the reviewed build, the user can run this in their own terminal:

```sh
"$HOME/personal-jobs/jobs/newsletter-inline/.venv/bin/python" -m workbench.newsletter_activate --config "$HOME/.config/newsletter-inline/config.json"
```

The program displays the exact mechanism and requires an interactive `ACTIVATE`
confirmation before reading the existing local token. No token is pasted,
printed, copied or sent in a URL. It validates the installed unit templates and
requires initially disabled configuration. Before any task update or enabling,
it reads and parses an actual matching attachment from the configured project.
An existing nonblank task can prove retrieval without clearing or editing it.
Empty scans, login pages, source mismatches and no-op service reports cannot
substitute for the real source proof.

This explicit mode sends the token only to the API host and exact HTTPS
`files.todoist.com/user_upload/[v2/]ID/NAME.html` resources from matching comments.
Automatic redirects are disabled, including on clients configured to follow them.
Only one explicit redirect to an HTTPS pre-signed URL on the two documented CDN
hosts is accepted; that request strips Authorization, cookies and ambient HTTP
authentication. Other hosts, ports, userinfo, fragments, controls, traversal,
unsigned CDN URLs and additional hops are rejected. HTML responses are limited
to 2 MB, strict UTF-8 and expected content types. Compressed responses are
rejected to bound decoding. No articles, images or scripts are fetched.

After source proof, the program saves a private proof bound to the exact project
and hashes of the downloader, converter, worker and activation code. It records
the explicit `todoist-files-origin-download.v1` grant, runs the existing service,
requires a fresh successful service report, enables the timer, and checks active
state and next trigger. It prints only fixed status/counts. Later authenticated
runs require the matching private proof; code changes invalidate it and require
another personal proof. Failure after configuration changes attempts to disable
both configuration and timer. If systemd rollback fails, the terminal reports
activation failure and asks the user to check the timer remains disabled.

This handoff has offline regression coverage; the agent does not run the real
credential-bearing retrieval or enable the timer. The first actual scheduled run
after personal activation must still be checked separately.
