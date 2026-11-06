# Reliability acceptance — 2026-11-06

Verified at 2026-11-06 13:48 PST. Implementation commit: `20a41cc`. Installed package: **mail-agent 0.2.0**.

## Completed scope

- Durable, bounded template-analysis recovery and owner health notices; atomic deadline creation and deduplication.
- Conversation follow-up grouping, immutable sending/retry payloads, persistent membership and delivery markers.
- Empty-INBOX heartbeats, distinct arrival status and a failing doctor result when the daemon is stopped.
- Explicit paginated overdue review, with no automatic task closure and imminent tasks protected in digests.
- Private daily SQLite snapshots, WAL consistency, isolated restore verification, seven-snapshot retention and manual verification commands.

## Evidence

- **172 tests passed, zero skipped**, including local GreenMail IMAP/SMTP, reconnect/draft tests and the real-Jev synthetic end-to-end test. The temporary test server was stopped afterward.
- Ruff passed; mypy passed for all 38 source modules; git diff whitespace checks passed.
- The wheel built successfully and was installed in an isolated environment before deployment. An installed-wheel database/backup/restore smoke test passed.
- A pre-upgrade snapshot of the live database was created and restored successfully before stopping the daemon. The existing launchd service was then restarted with 0.2.0, retaining all 17 prior dependency versions.
- Installed Python files match repository Python files exactly. The configuration hash is unchanged, and mailbox UIDVALIDITY/cursors did not regress.
- Formal installed `mail-agent doctor` returned 0 with all dependencies and the daemon passing. Separate read-only Gmail, Jev, Feishu and synthetic AI structured-output probes passed.
- All **8 historical template analyses recovered**. Their original notification dedupe keys have exactly their pre-upgrade row counts: **no old mail alerts were replayed**. The recovery queue is empty with no exhausted jobs.
- Final live state: **753 mails done**, **122 outbox messages sent**, zero pending/failed mail or notification rows; 131 existing tasks remain open. The two additional outbox messages are the pending/completed recovery health notices.
- The daemon created its daily backup and the installed CLI verified it by an isolated restore. SQLite quick_check is ok and foreign_key_check is empty. Backup status has no error.

## Observed limits

The first restart logged one IMAP callback timeout during startup. Automatic reconnect returned to ok at 13:43:42 PST, retained the original cursor, and later read-only mailbox comparison found no backlog. No further callback timeout was observed during this acceptance window; no unverified root cause is attributed to it.

The Feishu SDK still emits its existing datetime deprecation warning in tests. This verification covers a time window, not future continuous availability. Conversation grouping was verified with synthetic bursts and concurrent-send regressions; this deployment did not manufacture a real burst in the owner's mailbox.

See [Reliability and recovery](reliability.md) for operating commands and recovery boundaries. No code was pushed to a remote repository.
