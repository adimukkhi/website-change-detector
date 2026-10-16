# Website Change Detector

Website Change Detector monitors public web pages in a real headless browser,
stores meaningful versions, and alerts users when monitored content changes.
It is built with Flask, SQLAlchemy, APScheduler, Playwright, Beautiful Soup,
Pillow, and optional SMTP/Telegram/Discord/OpenAI integrations.

> Use this project only for public pages you are authorized to monitor. Do not
> submit passwords, cookies, API keys, or other stored credentials for a page.

## Live deployment

The deployed website will be available here:

**[Open Website Change Detector](https://your-deployed-domain.example.com)**

Replace the placeholder URL above with the actual deployment URL when it is
available.

## Features

### Monitoring

- Add a monitor with a name, public URL, and interval from **15 minutes to 24 hours**.
- Render pages with Playwright Chromium, including JavaScript-generated content.
- Save the rendered page text, content hash, title, HTTP status, and screenshot.
- Create a new snapshot only when normalized meaningful content changes.
- Display monitor status as `PENDING`, `OK`, `ERROR`, or `BLOCKED`.
- Show last checked, last successful check, and last changed timestamps in IST.
- Pause, resume, check immediately, or delete monitors.
- Search monitors by name or URL.
- Filter monitors by status and active/paused state.
- Export a monitor's complete change history as an authenticated CSV file.
- Use a light/dark theme switch with persisted browser preference.

### Noise filtering and selectors

- Normalize timestamps, clock values, dynamic counters, random tokens, and
  common session/request identifiers.
- Ignore common advertisement, sponsored, cookie-consent, banner, carousel,
  slider, and live-region elements by default.
- Configure additional CSS selectors to ignore.
- Configure CSS selectors for regions that should be watched exclusively.
- Use low, normal, or high sensitivity:
  - **Low:** ignores numeric and case-only changes in the content hash.
  - **Normal:** suppresses very long numeric noise.
  - **High:** preserves meaningful text and numeric differences.
- Numeric values are retained separately for tracking even when low sensitivity
  is selected.

### History and comparisons

- View a per-monitor version timeline.
- Compare adjacent versions or choose any two versions.
- View an HTML text diff with added, removed, and changed lines highlighted.
- Generate a highlighted screenshot diff.
- Generate a side-by-side screenshot comparison.
- Store optional one-line AI change summaries.
- Track labeled numeric values and view their history as charts.

### Alerts and digests

- Send change alerts through:
  - Email via SMTP
  - Discord webhooks
  - Telegram Bot API
- Include monitor name, URL, version IDs, changed text, summary, and a diff link.
- Configure keyword-only alerts.
- Keyword matching checks both added and removed text.
- Use a database-backed outbox with retries and exponential backoff.
- Prevent duplicate outbox records and delivery records.
- Send optional daily email digests.
- Digest delivery is idempotent for each monitor, recipient, and IST calendar day.

### Scheduling, reliability, and security

- APScheduler schedules checks and notification/digest workers.
- A database-backed queue deduplicates checks by canonical URL.
- Multiple monitors watching the same canonical URL share one browser fetch.
- Queue jobs use conditional database claims to avoid duplicate processing.
- Stale running jobs are recovered.
- Per-domain locking and configurable request spacing reduce request bursts.
- Browser navigation and rendering have bounded timeouts and retries.
- Public URL validation rejects:
  - Non-HTTP(S) schemes
  - Localhost and private hosts
  - Loopback, link-local, multicast, reserved, and unspecified addresses
  - Embedded usernames and passwords
  - Non-standard ports
- Redirects and unsafe browser subresources are blocked.
- CAPTCHA, bot challenge, and access-denied pages are recorded as `BLOCKED`,
  not as genuine content changes.
- HTTP/server/browser failures are recorded as `ERROR`.
- CSRF protection is enabled for form submissions.
- User passwords are hashed and monitor/history routes enforce ownership.

## Project layout

| Path | Purpose |
| --- | --- |
| `app.py` | Flask application, routes, authentication, dashboard, exports |
| `models.py` | SQLAlchemy models and timezone helpers |
| `scraper.py` | URL validation, Playwright rendering, screenshots, blocking |
| `scheduler.py` | Queue processing, normalization, scheduling, retries |
| `alerts.py` | Notification outbox and email/Telegram/Discord delivery |
| `digests.py` | Daily digest generation and delivery |
| `image_diff.py` | Screenshot diff and side-by-side image generation |
| `metrics.py` | Numeric value extraction and persistence |
| `summaries.py` | Optional one-line AI summaries |
| `templates/` | HTML pages and dashboard UI |
| `test_script.py` | Automated regression and integration tests |
| `instance/` | Local SQLite database directory |
| `data/screenshots/` | Saved screenshots and generated comparisons |

## Run locally on Windows

These commands assume PowerShell and Python 3.11 or newer.

### 1. Copy the project

Clone the repository:

```powershell
git clone <REPOSITORY_URL> website-change-detector
Set-Location .\website-change-detector
```

If you received the project as a ZIP instead, extract it and open PowerShell
in the extracted project folder.

### 2. Create a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script activation, either allow scripts for the current
user or invoke Python directly:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### 3. Install Python dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Install the Playwright browser

```powershell
python -m playwright install chromium
```

### 5. Create local configuration

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Open `.env` and replace the development secret:

```dotenv
SECRET_KEY=generate-a-long-random-secret
```

For a local installation, the default values use:

```dotenv
HOST=127.0.0.1
PORT=5000
DATABASE_URL=sqlite:///website_changes.db
SCREENSHOT_DIR=data/screenshots
APP_TIMEZONE=Asia/Kolkata
DAILY_DIGEST_HOUR=8
PUBLIC_BASE_URL=http://127.0.0.1:5000
```

The application displays timestamps and runs the daily digest schedule in
**IST (Asia/Kolkata)** by default. Database timestamps are kept timezone-aware
internally.

### 6. Start the application

```powershell
python app.py
```

Open this address in a browser:

<http://127.0.0.1:5000>

The application creates the local database and screenshot directory on startup.
Create an account, add a public monitor, and wait for or manually trigger its
first check. The first successful check establishes the baseline; alerts begin
on later meaningful changes.

### 7. Stop the application

Press `Ctrl+C` in the PowerShell window running the application.

## Run locally on macOS or Linux

```bash
git clone <REPOSITORY_URL> website-change-detector
cd website-change-detector
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
python app.py
```

Then open <http://127.0.0.1:5000>.

## Optional integrations

All alert integrations are optional. Leave their settings empty to disable
them.

### Email

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-sender@example.com
SMTP_PASSWORD=your-password-or-app-password
SMTP_FROM=your-sender@example.com
SMTP_STARTTLS=true
```

Set an alert email address on a monitor. Daily digests require both an alert
email and SMTP configuration.

### Discord

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### Telegram

```dotenv
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

### AI summaries

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-4o-mini
```

AI summaries are optional. A failed or unavailable AI request does not prevent
snapshot creation or ordinary alerts.

## Configuration reference

| Variable | Default | Description |
| --- | --- | --- |
| `SECRET_KEY` | development fallback | Flask session and CSRF secret; replace it |
| `DATABASE_URL` | `sqlite:///website_changes.db` | SQLAlchemy database URL |
| `SCREENSHOT_DIR` | `data/screenshots` | Screenshot storage directory |
| `HOST` | `127.0.0.1` | Development server bind host |
| `PORT` | `5000` | Development server port |
| `PLAYWRIGHT_TIMEOUT_MS` | `25000` | Browser navigation/render timeout |
| `SCRAPE_RETRIES` | `2` | Browser retry count after the first attempt |
| `DOMAIN_MIN_INTERVAL_SECONDS` | `2` | Minimum per-worker delay between same-domain checks |
| `APP_TIMEZONE` | `Asia/Kolkata` | Display and scheduler timezone |
| `DAILY_DIGEST_HOUR` | `8` | Daily digest hour in `APP_TIMEZONE` |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:5000` | Base URL included in alert diff links |
| `SMTP_*` | empty | Email delivery settings |
| `DISCORD_WEBHOOK_URL` | empty | Discord delivery setting |
| `TELEGRAM_*` | empty | Telegram delivery settings |
| `OPENAI_*` | empty | Optional AI summary settings |

## Run tests

From the activated virtual environment:

```powershell
python -m pytest -q
```

The suite covers URL validation, selector parsing, normalization, snapshot
deduplication, queue processing, alerts, keyword filtering, metric handling,
authentication, exports, and other application behavior.

## Production notes

For local development, `python app.py` is sufficient. For production:

- Run behind a production WSGI server such as Waitress or Gunicorn.
- Use a strong secret and never commit `.env`.
- Prefer PostgreSQL or another server database for multiple workers.
- Use HTTPS and set secure cookie settings.
- Run the scheduler/queue worker in a deliberate deployment topology.
- Add network-level outbound egress restrictions for SSRF defense in depth.
- Configure backups for the database and screenshot directory.
- Test real SMTP, Discord, and Telegram delivery in staging.
- Do not expose Flask's development server directly to the internet.

## Limitations

- Browser extension support is not included.
- Per-page RSS feeds are not included.
- The selector picker uses a bookmarklet and saved CSS selectors rather than
  embedding arbitrary third-party pages inside the application.
- Noise filtering is heuristic; page-specific watch and ignore selectors are
  recommended for highly dynamic pages.
