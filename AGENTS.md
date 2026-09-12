# Working on Starforge Workbench

Read README.md and docs/adapting.md, then inspect current source and tests; documentation can lag implementation. This repository is a public personal reference implementation.

- Preserve unrelated changes. Keep edits focused and test the affected behavior.
- Do not spawn agents automatically.
- Do not install services, launch providers, change existing session bindings, or enable collection merely to inspect or test the code.
- Use synthetic data and isolated test state. Never commit private manifests, credentials, transcripts, databases, machine inventories, or real deployment configuration.
- Keep observation, proposal, accepted work, process presence, activity, and completion semantics separate. Missing collector evidence means unknown visibility.
- Local configuration belongs outside Git. Preserve the default-deny ignore policy and third-party license notices.
- Run `uv run pytest -q` and `git diff --check`; report environmental skips. Desktop integration tests are explicit opt-in checks.
