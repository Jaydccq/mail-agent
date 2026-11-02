# Setup guide

From nothing to your first notification in about 15 minutes. `./install.sh` runs a wizard that asks for each item below and checks it on the spot, so you can also just start it and come back here when it asks for something.

- [1. What you need](#1-what-you-need)
- [2. Gmail app password](#2-gmail-app-password)
- [3. A chat app to reach you](#3-a-chat-app-to-reach-you): [Telegram](#telegram) or [Lark / Feishu](#lark--feishu)
- [4. Jev API key](#4-jev-api-key)
- [5. A generative model](#5-a-generative-model-optional) (optional)
- [6. Install and run the wizard](#6-install-and-run-the-wizard)
- [7. Check that it works](#7-check-that-it-works)
- [Upgrade, move, uninstall](#upgrade-move-uninstall)
- [Troubleshooting](#troubleshooting)

## 1. What you need

| | What | Time |
|---|---|---|
| 💻 | A Mac or Linux machine that stays on. It runs as a background service (launchd on macOS, `systemd --user` on Linux) | — |
| 📧 | A Gmail or Google Workspace account with 2-Step Verification | 1 min |
| 💬 | A Telegram account, or a Lark / Feishu account that can create apps | 1 min (Telegram) / 10 min (Lark) |
| 🔑 | A Jev API key from [TypeSafe](https://docs.typesafe.ai), which decides which emails matter | 2 min |
| 🤖 | Optional: a logged-in `claude`, `codex` or `gemini` CLI, or an API key, for summaries, chat and drafts | — |

Python is not a requirement: the installer uses [uv](https://docs.astral.sh/uv/) if you have it (uv brings its own Python), otherwise pipx, otherwise your Python 3.11+.

## 2. Gmail app password

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification) for the Google account.
2. Open <https://myaccount.google.com/apppasswords> and create one with any name, e.g. `mail-agent`.
3. Copy the 16 letters. The wizard accepts them with or without spaces.

Google Workspace (company or school) addresses work the same way. Two cases can't use app passwords: a Workspace admin may have turned them off for the domain, and accounts with Advanced Protection don't have them.

Why not OAuth or Google's Gmail MCP: OAuth needs your own Google Cloud project and consent screen, and in testing mode its refresh token expires after 7 days. The official Gmail MCP also needs an OAuth client and is read + drafts only, with no push. An app password with IMAP IDLE is one field, and new mail arrives within seconds.

## 3. A chat app to reach you

Pick one. The wizard pairs it with you, and from then on it only answers your own private chat.

### Telegram

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token BotFather replies with (it looks like `123456789:AA…`) and paste it into the wizard.
4. The wizard shows a link: open it and tap **Start**. You're paired. (Or send your bot the 6-digit code the wizard shows.)

### Lark / Feishu

The bot uses a persistent connection, so it needs no public address. Have the developer console open: [Feishu](https://open.feishu.cn/app), [Lark](https://open.larksuite.com/app), or your company's own console. The wizard prints these same steps and waits for Enter after each one.

1. **Create Custom App.** Pick a name you can search for later, e.g. `Mail Agent`.
2. **Add Features → Bot.**
3. **Permissions & Scopes → Batch import/export scopes**, paste this and confirm:
   ```json
   {"scopes":{"tenant":["im:message","im:message:send_as_bot","im:message.p2p_msg:readonly"],"user":[]}}
   ```
4. **Events & Callbacks.** Set both subscription modes to *persistent connection*, then:
   - Event configuration → Add Events → `im.message.receive_v1`
   - Callback configuration → Add callback → `card.action.trigger` (*Card callback communication*, not the legacy one)

   If saving says no connection was detected, carry on: save again while the wizard is waiting for pairing.
5. **Create Version** → Availability: only you (no approval needed) → Save, then **Publish**. Saving alone doesn't publish; until the version is released, nobody can find the bot.
6. **Credentials & Basic Info** → copy the **App ID** and **App Secret** into the wizard.

The wizard works out by itself whether the app is on Lark or Feishu, checks that the bot is published, then asks you to send the bot a pairing code.

## 4. Jev API key

Jev scores every new email (importance, whether it needs action, whether it has a date). Get a key at <https://docs.typesafe.ai>; the wizard checks it right away. If Jev is unreachable later, a simple built-in heuristic takes over until it's back.

## 5. A generative model (optional)

The model writes summaries, chats with you and drafts replies. Choose it in the wizard, or any time later with `mail-agent model` (it saves and restarts the service).

| Option | Uses | Notes |
|---|---|---|
| Claude subscription | local `claude` CLI | install [Claude Code](https://claude.com/claude-code) and log in first |
| Codex subscription | local `codex` CLI | install the Codex CLI and log in with your ChatGPT account |
| Gemini subscription | local `gemini` CLI | install the Gemini CLI and log in |
| Doubao (豆包) | Volcengine Ark API key, pay as you go | URL is filled in; copy the model ID from the Ark console (IDs carry a date). International accounts: the BytePlus ModelArk preset |
| Other API presets | API key (+ model name) | DeepSeek, Kimi, Qwen, GLM, OpenAI, Anthropic, OpenRouter, Ollama (local, no key). URL is filled in and editable |
| Your own API URL | URL + key + model name | anything OpenAI-compatible (`…/chat/completions`) or Anthropic-compatible (`…/v1/messages`): a proxy, a self-hosted model, another region |
| None for now | — | still triages and notifies (with an email snippet instead of a summary), but no chat or drafts |

**Subscription CLIs.** No model is bundled. mail-agent calls the official CLI you're already logged into, so usage counts against your own subscription. Every tool in those calls is turned off (no file reads, no commands, no MCP), and they run in an empty temporary directory with only the environment variables they need. Use your own account for yourself; serving other people from your subscription is a matter for the provider's terms.

**API options** are checked with a tiny request before saving. If the model name is wrong you're asked for just the model name again, with the server's error message.

**Doubao subscriptions.** The Doubao app membership (豆包 App 会员) has no API or CLI. Volcengine's Coding Plan and Agent Plan do come with an API key, but their terms only allow them inside the listed AI coding tools ("不能用于 API 调用"; other use may get the subscription suspended or the account banned), so mail-agent does not offer them. The Doubao preset is the regular pay-as-you-go Ark API. Doubao Seed models think before answering by default, so they are slower than a plain chat model.

## 6. Install and run the wizard

```bash
git clone https://github.com/Jaydccq/mail-agent.git
cd mail-agent
./install.sh
```

`install.sh` installs the `mail-agent` command and starts the wizard. It has 5 steps, each verified on the spot; Enter keeps the value shown in `[brackets]`:

1. **Jev API key**
2. **Gmail address + app password.** Add as many mailboxes as you like; press Enter on an empty address when done
3. **Notification channel.** Telegram or Lark / Feishu, then pairing
4. **Generative model.** A logged-in CLI it finds is the default
5. **Time zone.** Detected automatically; Enter keeps it, or type a city like `Shanghai`

Then it installs the background service, which starts at boot or login. On Linux, to keep it running while you're logged out: `loginctl enable-linger $USER`.

## 7. Check that it works

```bash
mail-agent doctor        # checks each part and prints ✅ or ❌ with the reason
mail-agent test-notify   # sends you a test message
mail-agent status        # config (secrets hidden), counters, background service state
```

`doctor` goes through config, SQLite, Jev, IMAP login for each mailbox, the chat channel and pairing, the model, and the background service, and ends with `All good ✅` or the number of problems.

Then send your bot `/help`. New mail is watched from now on: older mail isn't back-filled, but searching in chat covers your whole mailbox.

## Upgrade, move, uninstall

- **Upgrade:** `git pull && ./install.sh`. Running it again upgrades in place, keeps your config, and restarts the service.
- **Change settings:** `mail-agent setup` (Enter keeps current values), or edit `~/.mail-agent/config.toml` and run `mail-agent restart`.
- **Keep the data somewhere else:** set `MAIL_AGENT_HOME` to a directory before running `./install.sh`. The background service is installed with it, and every `mail-agent` command you run needs the same variable.
- **Uninstall:** `mail-agent uninstall-service`, then remove the command (`uv tool uninstall mail-agent`, or `pipx uninstall mail-agent`, or delete `~/.local/share/mail-agent` and `~/.local/bin/mail-agent`), then delete `~/.mail-agent` if you don't want to keep your mail database.

## Troubleshooting

Start with `mail-agent doctor`: it names the part that's failing. The log is `~/.mail-agent/agent.log` (on Linux also `journalctl --user -u mail-agent -f`); it never contains secrets or email bodies.

| Symptom | What to check |
|---|---|
| Gmail login fails | 2-Step Verification is on; you pasted an app password, not your Google password; a Workspace admin hasn't disabled app passwords. After revoking a password, re-add the address with `mail-agent add-mailbox you@gmail.com` or `/addmailbox` to update it |
| Can't find the Lark / Feishu bot | The version isn't released yet: Create Version → Save → **Publish** |
| Lark pairing never arrives | Both subscription modes are *persistent connection*, `im.message.receive_v1` is added, and the scopes from step 3 are granted |
| No instant alerts | Quiet hours (23:00–08:00) hold them until morning; `/resume` if you paused them; a mute rule may match (`/rules`); otherwise the mail went to the digest |
| No summaries, chat says it only knows commands | No model is configured, or the CLI is logged out: `mail-agent doctor`, then `mail-agent model` |
| Nothing arrives while the laptop sleeps | Expected: it catches up after waking |
