# Google Keep Collector

`bin/keep-collector.mjs` is an opt-in, read-only collector for one selected Google Keep checklist. It uses Playwright to attach to an already-running Chrome DevTools Protocol (CDP) endpoint. It does not use the Google Keep API, reverse-engineered libraries, a browser profile, or stored credentials.

The collector opens Keep only in the browser context it attached to. It does not mark, edit, archive, delete, or synchronize any note.

## Runtime Configuration

Keep the selector outside the repository, at `~/.config/starforge-ai-workbench/keep-collector.json` (mode `0600`):

```json
{
  "note_title": "Your checklist title",
  "require_pinned": true
}
```

The title is deliberately a local runtime choice. The collector requires one
exact title exposed by a title-labeled editable element or heading and rejects
duplicate exact matches. It does not select body text or fabricate a missing
title. The matched card must expose checkbox/list structure; malformed items
fail closed, while an explicit empty-list marker is returned as an empty
checklist. Do not commit a selector, note content, browser profile, cookies,
screenshots, or collector output. `require_pinned` defaults to `true`, preventing
an unpinned note with a matching title from being read.

The default Playwright installation is `~/browser-automation`. Set `AI_WORKBENCH_PLAYWRIGHT_DIR` only when it is installed elsewhere.

## Use

Start the already-authenticated Chrome instance with a local CDP endpoint, then run:

```sh
node bin/keep-collector.mjs --format markdown
node bin/keep-collector.mjs --format json
```

The default endpoint is `http://127.0.0.1:9222`; provide `--cdp-url` to use a different local endpoint. Do not expose the CDP port beyond localhost. The collector fails closed when it cannot attach, finds no matching pinned note, or cannot load the runtime selector.

## AI Workbench Practice

Treat collector output as an observation, not authorization or evidence of completion. Review it with the user before creating or transitioning actions. If a reviewed item should become active work, use the live Workbench API through `wb-api`, consult `/openapi.json` for unfamiliar writes, and re-read the record after a conflict or uncertain write.

Record only concise, useful milestones. Do not place note transcripts, credentials, personal data, regulated data, or raw browser output into Workbench records, Git, or issue discussions. Use a stable source identifier and collection time when recording a reviewed observation so repeated reads can be deduplicated.
