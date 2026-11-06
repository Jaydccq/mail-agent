# Reliability improvements

Goal: complete the five findings in the 2026-11-06 operational review and deploy a verified local installation.

Success criteria:

1. Transient model failure keeps notification delivery working, records recoverable analysis work, and later extracts future deadlines without replaying a notification or duplicating a todo. Recovery is bounded, persistent, and observable.
2. The first ordinary thread notification is immediate; nearby follow-ups are grouped for five minutes. VIP and security mail bypass grouping. Unrelated mailboxes/threads remain separate, retries do not duplicate entries, and restart preserves groups.
3. Successful INBOX checks advance the sync heartbeat even without new mail; doctor fails when the installed daemon is stopped. Status distinguishes sync from message arrival.
4. Overdue tasks remain open until the owner explicitly dismisses them. An overdue review command offers per-task actions, and old overdue tasks cannot crowd imminent tasks out of scheduled digests.
5. A daily local SQLite backup includes committed WAL transactions, is private, is validated through an isolated restore, and keeps seven snapshots. CLI commands create and verify snapshots without modifying the live mailbox.

Assumptions: existing accounts and preferences remain authoritative. Use standard-library SQLite backup support and existing outbox/channel interfaces. Keep new operational state in dedicated tables created by the existing schema setup; do not add legacy readers or data migrations. Existing mail and task records are retained.

Uncertainties: historical template analyses may contain no actionable dates; recovery must never invent a task. Message grouping affects alert cadence, so only ordinary follow-ups are delayed, with bounded group size. External services and laptop sleep remain outside continuous availability guarantees.

Slices: health heartbeat and doctor; private backups and restore verification; persistent analysis recovery; thread grouping; overdue review and digest ordering; full regression and deployment. Each slice includes focused regression tests, then the final suite, lint, type checks, package verification, and real read-only dependency probes.
