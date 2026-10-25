# QijuNotes development

Product baseline: plans/dsh_codex_taskpack_v1.1/SPEC_v1.1.md.
Keep A1–A3 daily unless the user explicitly changes their schedule. Preserve original business dates, actual submissions, ongoing conversations, durable receipts and user-only versioned acceptance.
Use project-owned isolated runtime and data. Never alter unrelated services or user-global configuration. Never commit credentials, personal conversations, raw model output or real deployment screenshots.
One DSH Web writer and one personal daily scheduler. SQLite transactions own persistence and invariants; prompts do not grant permissions.
Test in temporary directories with synthetic fixtures. Distinguish code tests, actual model calls, real devices, elapsed observation and user acceptance.
The public progress directory starts with unexecuted templates. Keep actual private deployment records locally; do not publish them through this source snapshot.
