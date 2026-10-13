# Security

Report a suspected vulnerability or accidental disclosure through [GitHub private vulnerability reporting](https://github.com/rpembry/starforge-workbench/security/advisories/new) or email rpembry@gmail.com. Describe the affected commit and a minimal reproduction; do not include live credentials or private transcripts. There is no guaranteed response time for this personal project.

Deployments need their own credentials and access policies. The service supports local bearer credentials or validated Cloudflare Access identity. The example systemd service binds to loopback. Keep databases and credentials outside the source tree with restrictive permissions.

Collectors are not a security boundary for arbitrary sensitive source content. In particular, the Codex observer submits brief derived response text, and its sensitive-pattern filter is incomplete. Review collection scope before enabling it. The service is a single-owner design; do not assume tenant isolation or a complete agent authorization engine.

This public repository starts from a sanitized source snapshot. Local configuration, operational inventories, private deployment history, and personal data are excluded. Checks reduce risk but do not establish that every future change is safe. Review the exact files and history you intend to publish.
