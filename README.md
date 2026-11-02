# mail-agent

An always-on personal email agent. It watches your Gmail, pushes important mail to Telegram or Lark/Feishu right away, and rolls the rest into a daily digest. Deadlines in emails become todos with reminders. You can chat with it in plain language to search mail, set rules, and draft replies. A reply is only sent after you confirm it.

The design goal is **light, with few settings**: one Python process and one SQLite file. No public address, no Google Cloud project, no Docker.

```
Gmail ──IMAP IDLE──▶ store ──▶ Jev triage ──▶ model analysis (summary / dates / what to do) ──▶ todos + notifications
                                                                                                │
you ◀── Telegram / Lark ◀───────────────────────────────────────────────────────────────────────┘
you ──▶ chat / buttons ──▶ search mail · todos · rules · memory · draft replies (sent only after you confirm)
```

## Quick start

```bash
git clone <repo-url> mail-agent
cd mail-agent
./install.sh
```

`install.sh` installs the `mail-agent` command and starts the setup wizard. It uses [uv](https://docs.astral.sh/uv/) if you have it (uv brings its own Python), otherwise pipx, otherwise a private virtualenv with your Python 3.11+. Running it again later upgrades in place and restarts the background service.

Have these ready (about 5 minutes in total; details under [Before you start](#before-you-start)):

1. **Jev API key** for email importance triage ([TypeSafe](https://docs.typesafe.ai))
2. **Gmail app password**: <https://myaccount.google.com/apppasswords> (needs 2-Step Verification)
3. **Telegram bot token**: message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token. Or a Lark/Feishu app
4. Optional: a logged-in `claude`, `codex` or `gemini` CLI on this machine, for summaries, chat and reply drafts with your own subscription

The wizard has 5 steps, each verified on the spot; Enter keeps the value in `[brackets]`:

1. Jev API key
2. Gmail address + app password (add as many mailboxes as you like; Enter on an empty address when done)
3. Notification channel. For Telegram it shows a link: open it, tap **Start**, and you are paired
4. Generative model: a logged-in CLI it finds is the default; or Doubao, another API preset, or your own API URL + key + model
5. Time zone: detected automatically, Enter to keep it (or type a city like `Shanghai`)

Then it installs a background service that starts at boot, and you are done. `mail-agent status` shows the state, `mail-agent doctor` checks everything one by one.

Adding another mailbox later is one command:

```bash
mail-agent add-mailbox you@gmail.com
```

or, from your private chat with the bot: `/addmailbox you@gmail.com <app password>`.

## Before you start

### Gmail: app password (about 1 minute)

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification) for your Google account
2. Open <https://myaccount.google.com/apppasswords>, create one with any name (e.g. mail-agent)
3. Paste the 16 characters into the wizard (spaces are fine)

Google Workspace (company / school) addresses work exactly the same way.

Why not OAuth / MCP: OAuth requires you to create your own Google Cloud project and consent screen, and in testing mode the refresh token expires after 7 days. Google's official Gmail MCP also needs an OAuth client, and it is read + drafts only, with no push. An app password + IMAP IDLE is a single field, and new mail arrives in near real time.

> Limits: a Google Workspace admin may have disabled app passwords for the domain; accounts with Advanced Protection cannot use them either.

### Telegram (about 1 minute)

In Telegram, open [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name → copy the token, paste it into the wizard, then open the link the wizard shows and tap **Start** (or send your bot the 6-digit code).

### Lark / Feishu (about 10 minutes)

The wizard walks you through creating the bot app step by step, then checks it: whether it is on Lark or Feishu, and that the bot is published (until a version is published you can't find the bot). Have the developer console open: [Feishu](https://open.feishu.cn/app), [Lark](https://open.larksuite.com/app), or your company's own address.

Persistent-connection mode needs no public address.

### Generative model (optional)

Pick one in step 4 of setup, or switch any time with `mail-agent model` (saves and restarts the service).

| Option | Uses | Notes |
|---|---|---|
| Claude subscription | local `claude` CLI | install [Claude Code](https://claude.com/claude-code) and log in first |
| Codex subscription | local `codex` CLI | install the Codex CLI and log in with your ChatGPT account |
| Gemini subscription | local `gemini` CLI | install the Gemini CLI and log in |
| Doubao (豆包) | Volcengine Ark API key, pay as you go | URL is filled in; copy the model ID from the Ark console (IDs carry a date). International accounts: the BytePlus ModelArk preset |
| Other API presets | API key (+ model name) | DeepSeek, Kimi, Qwen, GLM, OpenAI, Anthropic, OpenRouter, Ollama (local, no key). URL is filled in and editable |
| Your own API URL | URL + key + model name | anything OpenAI-compatible (`…/chat/completions`) or Anthropic-compatible (`…/v1/messages`): a proxy, a self-hosted model, another region |
| None for now | — | still triages and notifies (using an email snippet instead of a summary), but no chat or drafts |

Subscription mode works like tools such as [Orca](https://www.onorca.dev/docs/agents/supported): no model is bundled; it calls the official CLI you are already logged into, so usage counts against your own subscription. All CLI tools are turned off for these calls (no file reads, no commands, no MCP), and it runs in an empty temporary directory.

Every API option is checked with a tiny request before it is saved; if the model name is wrong you are asked for just the model name again, and the server's error message is shown.

**About Doubao subscriptions.** The Doubao app membership (豆包 App 会员) has no API or CLI. Volcengine's Coding Plan and Agent Plan do come with an API key, but their terms only allow them inside the listed AI coding tools ("不能用于 API 调用"; other use may get the subscription suspended or the account banned), so mail-agent does not offer them. The Doubao preset is the regular pay-as-you-go Ark API. Doubao Seed models think before answering by default, so they are slower than a plain chat model.

## Everyday use

**Important-mail notifications**: sender, subject, a 2–3 sentence summary, what you need to do, and relevant dates. Buttons:

- ✅ Done
- 🔕 Fewer alerts from this sender (digest only from now on)
- ✍️ Draft reply: writes a draft, also saves it to your Gmail Drafts, and sends you the full preview; it is only sent when you tap "📤 Send" or reply `confirm <id> <code>`. After sending, the copy in Gmail Drafts is moved to Trash, and the sent reply is not reported back to you as new mail

**Daily digest**: 08:30 and 18:00 by default, split into "needs you / worth a look / due soon". It says explicitly whether there was simply no new mail or a mailbox failed to sync.

**Todo reminders**: deadlines and meetings in emails become todos automatically. Date-only items are reminded at 20:00 the day before and at 09:00 on the day; items with a time are reminded at 20:00 the day before and 2 hours before. Reminders have "Done" and "Remind me tomorrow" buttons. If you reply to a thread yourself in Gmail, the matching "reply needed" todo closes automatically.

**Quiet hours**: 23:00–08:00 by default. Nothing is pushed instantly; it is held until morning.

**Commands**: `/todo` `/digest` `/status` `/pause` `/resume` `/rules` `/memory` `/mailboxes` `/addmailbox` `/removemailbox` `/timezone` `/new` `/help`

**Mailboxes from chat**:

- `/mailboxes` lists your mailboxes and their sync status
- `/addmailbox you@gmail.com <app password>` (in your private chat with the bot) adds a mailbox or updates its app password. The bot deletes that message right away because it contains the password, verifies the login, and starts watching without a restart
- `/removemailbox <address or id>` stops watching a mailbox (mail already stored stays)

**Time zone**: `/timezone` shows it; `/timezone Asia/Shanghai` (or a city: `/timezone new york`, `/timezone beijing`) changes it right away, no restart. Digests, quiet hours and reminders follow the new zone, and open to-dos move with it: a timed one keeps its moment (12:00 Los Angeles becomes 04:00 next day in Shanghai), a date-only one keeps its date.

**Suggested memories**: memories you ask for ("remember: …") are saved right away. Besides that, I sometimes notice something worth remembering and ask first, with 💾 Save / No buttons; nothing is saved until you tap Save, and anything you turn down is never suggested again. At most about one suggestion a day. I notice:

- a fact you mention in passing without asking me to remember it ("my manager is Priya", "I'm a PhD student")
- alerts from the same sender (or several people at one organization) that you keep marking ✅ Done without replying, or several senders at one organization you've muted
- a reply I drafted that you edited in Gmail before sending: a changed sign-off, or a clear change in tone or length

Saved suggestions are ordinary memories: `/memory` lists them and `/memory del <id>` removes them.

**Just talk to it** (needs a generative model):

- Find last week's email from Alex about the contract
- Remind me to submit my expenses on Friday
- Don't send me instant alerts for mail from deals@shop.com anymore
- Remember: I always reply to work email in English
- Reply to Wei and say I'll send it before noon on Friday

Replies and digests show **bold**, *italics*, `code` and lists; a table becomes a list and a link shows its full address. Notifications and reply-draft previews are shown exactly as written, so a preview is character for character what will be sent.

## Management commands

```text
mail-agent setup                          reconfigure (Enter keeps current values)
mail-agent run                            run in the foreground
mail-agent status                         config (secrets hidden) + runtime stats + background service state
mail-agent doctor                         check config, SQLite, Jev, each mailbox, channels, generative model
mail-agent add-mailbox [ADDRESS]          add a mailbox, or update an existing one's app password
mail-agent remove-mailbox ADDRESS_OR_ID   stop watching a mailbox (stored mail stays in the local db)
mail-agent mailboxes                      list mailboxes and their sync status
mail-agent timezone [ZONE]                show or change your time zone (restarts the background service)
mail-agent model                          choose the generative model (CLI subscription, Doubao, other APIs, your own URL)
mail-agent restart                        restart the background service (e.g. after editing config.toml)
mail-agent test-notify                    send yourself a test message
mail-agent install-service                install the background service (Linux systemd --user / macOS launchd)
mail-agent uninstall-service
```

`add-mailbox` asks for the address (if not given) and the app password, verifies the IMAP login, saves the config, and restarts the background service if it is installed. A new mailbox is watched from that moment on: older mail is not back-filled, but search in chat still covers its full history. Re-adding an address that is already configured just updates its app password, which is what you want after revoking one.

To keep it running on Linux while you are logged out: `loginctl enable-linger $USER`. Logs are in `~/.mail-agent/agent.log` (or `journalctl --user -u mail-agent -f`).

## Data and config

Everything lives in `~/.mail-agent/` (set `MAIL_AGENT_HOME` to move it):

- `config.toml`: config including secrets, permissions 0600. Time zone, language, digest times and quiet hours are under `[prefs]` (after editing by hand: `mail-agent restart`)
- `agent.db`: SQLite with email bodies, todos, rules, memory, conversations. Emails, todos, rules and memory are kept; delivered notifications and other bookkeeping are removed after 30 days, chat history after 90 days
- `agent.log`: rotating log; never records secrets or email bodies

`prefs.language` (default `en`) sets the language of notifications, digests and summaries. Chat replies follow the language you write in.

Only one instance runs at a time: a second process refuses to start because of a process lock.

## Security

- **It never sends mail on its own.** Sending only happens when you tap the button or type the confirmation code. A code works once and expires after 24 hours; if the content changes, you must confirm again. If a send is interrupted and the result is unclear, it is marked "unknown" and you are asked to check Gmail; it is never re-sent automatically.
- Email content is always treated as data: it is clearly isolated in prompts, and the model that analyzes mail has no tools, so an email saying "please forward this to xxx" is never acted on. In chat, once a request has read email content, changes to rules, memories or todos wait for you to tap Confirm, so an email can't quietly change how your mail is handled.
- Fetching mail never changes read status (`BODY.PEEK`), and it never deletes mail or changes labels.
- It only responds to your own private chat (the account bound at pairing); group chats and other people are ignored.
- Email bodies are stored locally in `agent.db`; an encrypted disk is recommended.
- With the Codex CLI, every tool that can be turned off (shell, browser, etc.) is turned off; MCP servers you configured yourself in `~/.codex/config.toml` cannot be disabled from the command line. Disabling tools in the Gemini CLI is best effort.

## Testing

```bash
uv pip install -e '.[dev]'
pytest && ruff check && mypy             # what CI (.github/workflows/ci.yml) runs on Python 3.11 and 3.14
```

Integration tests (GreenMail), the live chat eval against your real setup, and what has been tested for real: [docs/testing.md](docs/testing.md).

## Known limits

- Only the inbox (INBOX) is watched, not other labels; attachments are recorded by file name only, their content is not read.
- The first run (and a newly added mailbox) does not back-fill old mail; it starts from now (search in chat still covers the full history).
- The machine needs to stay on; while a laptop sleeps no pushes arrive, and it catches up after waking.

## License

Apache-2.0. See [LICENSE](LICENSE).
