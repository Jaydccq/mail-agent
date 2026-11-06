# Reliability and recovery

The 0.2.0 update addresses the findings in [the operational review](operations-review-2026-11-06.md). Existing accounts, rules, memories, quiet hours and task decisions remain authoritative.

## Analysis recovery

Normal processing still retries a transient model error twice before delivering a template summary. A separate persistent recovery queue then finds template-only analyses from the last seven days, limited to configured mailboxes, unhandled mail and messages with an important/action/date signal. Newly received mail waits at least 15 minutes before this additional recovery starts; historical eligible mail can start immediately.

Recovery does not re-triage mail or replay its original notification. It updates the stored analysis and can create future deadlines. All deadlines from one analysis commit in a single transaction. Repeated recovery, including changed model wording after a crash, cannot create another set of tasks for that mail. Existing tasks, including completed or dismissed tasks, are preserved.

Recovery uses one background worker sharing the existing model concurrency limit. Each mail gets at most five recovery attempts, with increasing delays capped at four hours, and no automatic analysis beyond seven days after receipt. Exhausted jobs stay visible in `/status`. A missing model does not trigger a recovery loop. Handled mail and removed mailboxes leave the recovery queue.

The owner receives one queued notice when recovery work appears and one when it clears. These follow quiet hours. They describe analysis completeness, not whether the current provider endpoint is down. CLI diagnostics retain a safe error category, such as HTTP 426, without copying raw CLI output, prompts, email text or service URLs into error logs.

After fixing a provider problem, explicitly restart an exhausted, still-eligible recovery cycle with:

```bash
mail-agent retry-analysis
```

This command requeues analysis only. It does not resend historical alerts. Dates that have already passed do not become new tasks. Historical mail may contain no valid actionable dates; an empty recovered deadline list is legitimate.

## Conversation notifications

The first ordinary alert for a conversation is immediately eligible for delivery, subject to existing quiet hours. If the conversation has had an alert in the last 15 minutes, subsequent ordinary updates are collected for five minutes. Updates can join an unsent group without extending its original delivery time. Groups are separated by mailbox, Gmail conversation, channel and owner; each contains at most ten emails and approximately 12,000 characters before the existing channel splitting applies.

VIP and security mail bypass grouping. Existing quiet-hour rules still apply: VIP bypasses quiet hours, ordinary security mail does not. Mail without a Gmail conversation ID remains individually notified. No global sender mute or importance-threshold change is introduced.

A group preserves each update's subject, analysis and dates. Buttons apply to its latest email. Marking it handled preserves earlier updates in the displayed card. Individual message IDs are included for follow-up chat requests.

Membership and payload changes are atomic. The sender claims the latest payload before making a network call; a sending or previously attempted payload is immutable to grouping. Every member receives its delivery timestamp only after the channel accepts that group. A crash during notification delivery can still cause an at-least-once notification retry; arbitrary external network delivery cannot be guaranteed exactly once. Email sending still requires the owner's explicit approval and retains its existing unknown-send protection.

## Health checks and overdue tasks

`mail-agent doctor` returns nonzero when the background daemon is not running, even if dependency checks pass. A successful INBOX catch-up advances the sync timestamp even if no new mail arrived. The watcher checks at least once each nine-minute IDLE cycle while connected. CLI status distinguishes the last successful INBOX check from the last received email; `/status` also shows recovery counts and the latest verified backup or backup error.

Use `/overdue` or `/overdue 2` in the owner chat to review five old tasks per page. Only an explicit Done or Dismiss tap closes a task. Reviewing or starting the new version does not automatically complete, delete or postpone existing tasks. Scheduled digests reserve space for imminent tasks and report the overdue count with a review link instead of letting a large historical backlog hide upcoming work.

## Backups

The scheduler makes a local backup immediately if none has been recorded, then every 24 hours while running. Backups use SQLite's backup API, including committed WAL transactions, without stopping mail processing. Each snapshot is restored into an isolated temporary database and checked for integrity, foreign keys and required application tables before being published. A failed snapshot is not published; automatic failure retries wait one hour and remain visible in `/status`.

Snapshots live under `~/.mail-agent/backups/`, with directory mode 0700 and file mode 0600. The latest seven snapshots are retained. These contain private email data and must not be committed. Credentials in `config.toml` are not included. Keep the current configuration separately using a private system backup.

```bash
mail-agent backup                         # create and validate a snapshot now
mail-agent backup --verify /path/to/mail-agent-TIMESTAMP.sqlite3
```

Verification performs an actual isolated restore and reads counts only. It does not replace the live database or send notifications.

For a deliberate disaster restore: first stop the service using `mail-agent uninstall-service`, verify the selected snapshot, and preserve the current data directory privately. Restore the verified snapshot as `agent.db` in that directory while no agent process is running; remove the obsolete `agent.db-wal` and `agent.db-shm` sidecars from the preserved old database only after stopping it. Keep `config.toml`. Set private permissions, then run `mail-agent install-service` and `mail-agent doctor`. Restoring an older snapshot also restores older cursors and task states, so mail received after it can be replayed. Do not restore merely to retry a failed analysis.

Schema version 2 creates dedicated recovery and notification-membership tables and an arrival-time index. There is no legacy implementation or migration framework. Older binaries reject this database version. Reverting the binary requires an appropriate pre-upgrade database snapshot as well.

## Verification

Regression coverage in `tests/test_reliability.py` includes empty-INBOX heartbeats, daemon health exit codes, WAL snapshots and isolated restores, retention and file permissions, backup failure/restart scheduling, analysis recovery after restart, bounded retries, atomic deadlines, grouped delivery membership and claims, quiet-hour boundaries, group limits, grouped-card edits, explicit overdue decisions, digest ordering and safe CLI diagnostics.

The full integration environment can run without Docker when Java 17 is available. Download `greenmail-standalone-2.1.3.jar` from Maven Central into a temporary directory, then run:

```bash
java -Dgreenmail.setup.test.all -Dgreenmail.hostname=127.0.0.1 \
  -Dgreenmail.users=me:apppass@localhost -Dgreenmail.users.login=email \
  -jar /path/to/greenmail-standalone-2.1.3.jar
```

Use only these synthetic local credentials. Wait for port 3993 before running tests. The end-to-end test also reads `MAIL_AGENT_TEST_JEV_KEY` from the environment; never echo or commit it. Shut down the temporary Java process when testing finishes.
