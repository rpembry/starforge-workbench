#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
bash -n "$ROOT/bin/ai-workbench"
"$ROOT/bin/ai-workbench" --dry-run up ai-workbench >/dev/null
"$ROOT/bin/ai-workbench" list >/dev/null
"$ROOT/bin/ai-workbench" plan >/dev/null
