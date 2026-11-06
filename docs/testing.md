# Testing

Operational regression coverage and the local Java/GreenMail setup are documented in [Reliability and recovery](reliability.md#verification).

```bash
uv pip install -e '.[dev]'
pytest                                   # offline unit tests + fake Telegram
ruff check && mypy                       # lint + type checks; CI (.github/workflows/ci.yml) runs all three on Python 3.11 and 3.14
```

For the IMAP/SMTP integration and end-to-end tests: start a GreenMail container with the command at the top of `tests/test_mail_greenmail.py`; the end-to-end test also needs `MAIL_AGENT_TEST_JEV_KEY`. CI starts the same GreenMail container, so the integration tests run on every push (the end-to-end test is skipped there: no Jev key).

Chat and memory against your own setup: real model, real Gmail, a **copy** of your database, and no messages to Telegram or Lark; reply drafts it saves to Gmail are deleted at the end. It sends one real email: a reply to a synthetic email whose Reply-To is your own address (it refuses to send if the draft is addressed to anyone else), checks that it reached Sent, then moves it to Trash. About 100 checks: normal conversation (introducing itself, a general question, writing and revising an email that isn't a reply, small talk in Chinese, a quick sum; no tools, nothing drafted or stored), memory (store / recall after `/new` / forget / no small talk / used in drafts / changes triage), suggested memories (a fact mentioned in passing, ✅ Done and 🔕 taps, an edited draft's sign-off and tone; Save stores it, No means it never comes back, one card a day), todos, rules, search with follow-ups, an inbox overview that only mentions emails the search returned, details from one email (amount, due date, attachment name) turned into a todo, Gmail search filters (attachment, PDF, unread, sender, date), Chinese (search and a summary), a two-tool request, prompt injection inside an email, a change requested after reading an email waiting for your Confirm tap, ✍️ Draft a reply and Cancel on a notification, `/status` `/mailboxes` `/digest`, draft → revise → 📤 Send, what it must refuse, and that every reply renders as valid Telegram HTML with no raw Markdown tables.

```bash
python tests/live_chat_eval.py --provider claude-cli    # or codex-cli; --only memory,rules
```

Formatting on your own phone: one of each message type (chat replies with Markdown, a table, Chinese, a digest, and a notification and a draft preview that must show text exactly as written), titled "🧪 format check" and safe to delete. `--dry-run` prints the HTML instead.

```bash
python tests/live_format_check.py
```

| Part | Status |
|---|---|
| Jev triage | tested against the real API (9 synthetic emails, about 0.1 s each) |
| Claude subscription (claude CLI) | tested for real: email analysis, drafting, chat. Live chat eval: 100/100 checks on the current code, about 8 s per turn (22 s at most) |
| Codex subscription (codex CLI) | tested for real: email analysis, drafting, chat. Live chat eval: 100/100 checks on the current code. Slower: about 22 s per turn, up to 1.7 min for a multi-step answer |
| Gmail: IMAP fetch, IDLE push, Gmail search, Sent polling, saving/removing drafts, SMTP send | tested against a **real Gmail account** (localized folder names such as `[Gmail]/已发邮件` are found via SPECIAL-USE); reconnect and retry paths tested against GreenMail |
| Telegram | tested with a **real bot**: pairing, notifications with buttons, edits, replies; parsing of updates and callbacks tested against a mock Bot API. Formatting is checked offline by a mock that rejects HTML the real Bot API rejects (bad nesting such as code inside bold, unclosed tags), including 3000 random Markdown lines |
| End to end (receive → triage → notify → buttons → chat → draft → confirm send) | tested for real: real Gmail + real Telegram bot + real Jev + Codex CLI. Also covered offline (GreenMail + fake Telegram + scripted model), including catch-up after restart |
| Lark / Feishu | written against the lark-oapi 1.7.5 source; detecting Lark vs Feishu, the published-bot check and pairing tested with a real Feishu app (the token endpoint accepts an app on both editions, only the persistent-connection endpoint tells them apart), including re-pairing while the background service runs; the format-check cards checked by eye in Feishu web (headings, tables and `code`, which Feishu's card markdown lacks, are converted) |
| Gemini CLI | implemented from the official docs, **not tested**: on 2026-11-01 Gemini CLI 0.63.0 refused a personal Google login with `IneligibleTierError` ("no longer supported for Gemini Code Assist for individuals") |
| API providers (Doubao, DeepSeek, Kimi, Qwen, GLM, OpenAI, Anthropic, OpenRouter, Ollama, your own URL) | adapters tested against a mock server (custom URL/key/model, structured-output fallback, Anthropic-compatible auth headers, error handling) and the setup step offline; **not tested against the real services** (no keys) |
| Adding / removing mailboxes | `add-mailbox` tested against a real Gmail account; chat `/addmailbox`, `/removemailbox`, `/mailboxes` tested against GreenMail + fake Telegram |
| Chat agent and memory | `tests/live_chat_eval.py` on real Gmail with Claude and Codex: remember / recall after `/new` / forget, small talk not stored, memories used in drafts and in triage (the same email went digest → notify once "anything from my advisor is important" was remembered), todos, rules, follow-up questions, Chinese, two-tool requests, prompt injection inside an email (ignored and flagged), refusing to approve or send for you. Suggested memories: cards from a fact mentioned in passing, from ✅ Done / 🔕 taps and from an edited draft (sign-off, tone), Save / No, the daily cap, declined ones never coming back; offline tests cover the gate and each signal. Normal conversation, inbox overviews, details and attachments, search filters, notification buttons and commands are covered too. Reading Sent mail back for the draft comparison was checked read-only on real Gmail, and the suggestion eval simulates its edit-then-send step; the `send` scenario sends one real reply to your own address (draft, revise, 📤 Send, found in Sent, then trashed) |
| Install from a clone | `install.sh` tested from a fresh clone with uv, with plain python3 (venv), and as an upgrade of a running install (the pipx path is not tested); the setup wizard is covered offline end to end |
| Changing time zone | `/timezone` and moving open to-dos tested offline; `mail-agent timezone` (show) run for real |
