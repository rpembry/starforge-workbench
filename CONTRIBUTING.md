# Contributing

Feedback, adaptations, bug reports, and small pull requests are welcome. Open an issue or discussion for a broad change so we can compare it with existing tools first. Email Randall Embry at rpembry@gmail.com if you prefer.

Describe the problem, the resulting behavior, and how you checked it. Include your source commit and relevant platform/provider versions. Use synthetic examples instead of personal records. AI-assisted contributions are welcome; understand and verify what you submit.

Run `uv sync --frozen`, `uv run pytest -q`, and `git diff --check`. Graphical terminal and optional browser checks need additional local tooling; report skips honestly. Never use real agent sessions or production data as disposable test fixtures.

Keep credentials, local manifests, databases, session records, logs, personal instructions, and deployment inventories outside Git. The `.gitignore` defaults to ignoring files unless their source category is explicitly allowed; review additions before committing.

Keep changes focused. Avoid broad rewrites solely to make this personal reference more generic. Preserve the distinction between observations and accepted work. Contributions submitted for inclusion are under the project's MIT license; retain notices for third-party material.
