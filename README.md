# Facebook Marketplace iPhone Watcher

Polls Facebook Marketplace every ~45 seconds, and sends you a Telegram and/or
Discord alert the moment a new matching iPhone listing appears. Remembers what
it has already seen, so you are never told about the same listing twice.

---

## What it does

```
Facebook Marketplace search (newest first)
        │
        ▼
  Parse listing cards ──► price band + keyword filters
        │
        ▼
  SQLite: already seen? ──no──► Telegram + Discord
        │
       yes
        ▼
      ignore
```

- **Dedupe** is atomic (`INSERT OR IGNORE`), so restarts never re-alert.
- **Filters run locally as well as in the URL**, so a Facebook-side parameter
  change can't flood you.
- **First run is silent** — it records the existing listings without alerting,
  so you don't wake up to 30 messages on start.
- If the Facebook session expires, it tells you once and stops cleanly instead
  of failing forever in the background.

---

## Setup

### 1. Install

Requires Python 3.11+ (3.13 is fine).

```bash
cd fb-iphone-watcher
python -m venv .venv
```

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

**macOS / Linux:**
```bash
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure

```bash
cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml
cp .env.example .env                    # Windows: copy .env.example .env
```

Edit `config.yaml` for search terms, location, price band and polling.
Edit `.env` for secrets.

### 3. Get a Telegram bot token

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, give it a name (e.g. `My iPhone Watcher`) and a username
   (must end in `bot`, e.g. `emmanuels_iphone_bot`).
3. BotFather replies with a token like
   `123456789:AAH...` — paste it into `.env` as `TELEGRAM_BOT_TOKEN`.
4. **Open a chat with your new bot and send it any message** (e.g. "hi").
   *This step is mandatory.* A bot can't start a conversation with you, so
   without it every send fails with "chat not found". This is the single most
   common setup mistake.
5. Find your chat id by opening this in a browser, with your real token:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Look for `result[0].message.chat.id` (a number, not your username) and put
   it in `.env` as `TELEGRAM_CHAT_ID`.
6. Verify with `python main.py --test-notify`.

### 4. Get a Discord webhook (optional)

In Discord: **Server Settings → Integrations → Webhooks → New Webhook →**
pick a channel → **Copy Webhook URL** → paste into `.env` as
`DISCORD_WEBHOOK_URL`.

To disable a channel, set it to `false` in `config.yaml`.

### 5. Log in to Facebook (required)

Marketplace requires a logged-in session — logged-out requests hit a login
wall. Log in once, by hand; the session is saved to a browser profile.

```bash
python main.py --login
```

A browser window opens. Log in to Facebook normally (2FA and captchas work
because you're doing it yourself), wait for the Marketplace page to load, then
close the window. The session is stored in `data/browser-profile/`.

> Your password is never handled by this program or saved in the repo.
> Re-run `--login` whenever the bot tells you the session expired — that
> happens after weeks of use, or if Facebook logs you out.

### 6. Test

```bash
python main.py --test-notify        # sample alert to each channel
python main.py --once --debug       # one real poll, dumps HTML on trouble
```

The `--once` run should log something like `Poll #1: parsed=24 new=0`. Format
your alerts and check the log. If `parsed=0`, look in `data/debug/` — it'll
have the page HTML and a screenshot.

Then run it for real:

```bash
python main.py
```

---

## Configuration reference

| Key | Default | Notes |
| --- | --- | --- |
| `search.query` | `iphone` | Search terms |
| `search.city_slug` | `london` | Marketplace area slug |
| `search.radius_km` | `25` | Search radius |
| `search.latitude` / `longitude` | London | Centre of the radius |
| `search.min_price` / `max_price` | `100` / `1200` | Price band in £, either can be `""` for open |
| `search.search_url_override` | `""` | Paste a full Marketplace URL to override everything |
| `polling.interval_seconds` | `45` | Minimum 15 |
| `polling.jitter_seconds` | `8` | Random ± on the interval |
| `filters.require_any` | `["iphone"]` | Title must contain one |
| `filters.exclude` | parts/cracked/locked… | Title containing any is dropped |
| `notifications.first_run_silent` | `true` | Record existing listings without alerting |
| `browser.headless` | `true` | `false` to watch it work |

**Changing location:** set `city_slug`, `latitude` and `longitude` together.
Find coordinates by right-clicking a spot in Google Maps — the first number is
latitude. For example, Birmingham is `51.4545, -1.8798`.

**Narrowing the price band:** £100–£1200 is deliberately wide. To focus on
genuine bargains, set `max_price: 350`.

---

## Running it continuously

The bot only works while your PC is awake and logged in. **Set your machine
not to sleep** or polling pauses:

- Windows: Settings → System → Power → Screen and sleep → *Sleep: Never*
  (or `powercfg /change standby-timeout-ac 0`)
- macOS: System Settings → Lock Screen → "Prevent automatic sleeping" — or
  run it inside `caffeinate -i ./run.sh`

### Windows

Double-click **`run.bat`**. It restarts the bot within 30 seconds of any crash.

To start it automatically at logon:

1. Open **Task Scheduler** → *Create Task*.
2. **General**: name it "FB iPhone Watcher", tick *Run only when user is logged on*.
3. **Triggers** → New → *At log on*.
4. **Actions** → New → Program: full path to `run.bat`, Start in: the project folder.
5. **Conditions**: untick *Start the task only if the computer is on AC power*.
6. **Settings**: untick *Stop the task if it runs longer than:*.

### macOS

```bash
chmod +x run.sh
./run.sh
```

To keep it running (`~/Library/LaunchAgents/com.you.fbwatcher.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.fbwatcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/PATH/TO/fb-iphone-watcher/run.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/fbwatcher.log</string>
  <key>StandardErrorPath</key><string>/tmp/fbwatcher.err</string>
</dict></plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.you.fbwatcher.plist`.

For a quick session instead: `tmux new -s watcher` then `./run.sh`.

---

## When something goes wrong

| Symptom | What it means | Fix |
| --- | --- | --- |
| `Session expired` alert, bot exits | Facebook logged you out | `python main.py --login` |
| `parsed=0` for several polls | Page structure changed, or search genuinely empty | Check `data/debug/`; try `--once --debug` |
| Telegram: `401 Unauthorized` | Bad bot token | Re-copy the token from @BotFather |
| Telegram: `400 ... chat not found` | Bot never messaged, or wrong chat id | Send your bot a message, re-check `getUpdates` |
| Discord: `404 Unknown Webhook` | Webhook deleted | Create a new one |
| `Configuration problem: ...` at startup | Missing key, or token left in `.env.example` form | Read the message — it names the key |
| Alerts stopped but no errors | PC slept | See *Running it continuously* |

Logs: `data/logs/bot.log` (rotates at 5 MB, keeps 3 files).
Every poll logs `parsed=N new=N` — if `new` keeps being 0 and `parsed` is
healthy, the bot is fine and Marketplace is just quiet.

### It stopped alerting entirely after weeks

Almost certainly Facebook changed its page markup. Run:

```bash
python main.py --once --debug
```

Open the HTML and screenshot in `data/debug/`. The one thing to check is that
listing links still contain `/marketplace/item/123456`. Fix the matching
selector in `SELECTORS` at the top of `watcher/marketplace.py` — everything
else is anchored to that.

---

## How it works (for when you want to change it)

| File | Responsibility |
| --- | --- |
| [main.py](main.py) | CLI, poll loop, first-run handling, session-expiry alert |
| [watcher/config.py](watcher/config.py) | YAML + `.env` loading and validation |
| [watcher/browser.py](watcher/browser.py) | Persistent Chromium profile, login-wall detection |
| [watcher/marketplace.py](watcher/marketplace.py) | Search URL, card parsing, `SELECTORS`, debug dumps |
| [watcher/filters.py](watcher/filters.py) | Price band + include/exclude keywords |
| [watcher/store.py](watcher/store.py) | SQLite seen-listings store |
| [watcher/notify.py](watcher/notify.py) | Telegram + Discord with retries |

### Tests

```bash
python -m tests.run_all   # everything, exits non-zero on failure
```

Individually: `test_logic.py` (filters, URL building, dedupe), `test_parsing.py`
(real parser against a synthetic page), `test_notify.py` (both channels against
a local mock server — no tokens needed), `test_loop.py` (first-run silence and
duplicate suppression).

### Known trade-offs

- **One notification can be lost on a hard crash.** A listing is recorded
  before its alert is sent, so a crash in that instant loses that alert rather
  than risking a duplicate every restart. Deliberate.
- **Radius is approximate.** Facebook treats it as a hint; results slightly
  outside 25 km can appear.
- **Scraping Marketplace is against Facebook's terms**, and polling too fast
  can get the logged-in account temporarily restricted. The defaults (45s with
  jitter, one session, first page only) are deliberately gentle — use an
  account you'd be comfortable having restricted, and don't set the interval
  below 15s.
- **Detection latency** is bounded by the poll interval plus a few seconds of
  page load — typically under a minute at the default settings.
