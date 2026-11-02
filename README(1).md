# IG Watchdog Telegram Bot

Button-first Telegram bot for verified Instagram profile snapshots and change notifications.

## Features
- Reply-keyboard menu: Profile Check, Live Monitor, Ban Monitor, Unban Monitor, Watchlist, Stats, Remove Monitor, Status, Help.
- Saves verified profile snapshots (name, bio, followers, following, post count) to persistent JSON storage.
- Compares each successful lookup against the previous verified snapshot and alerts when any tracked field changes.
- Keeps the last verified snapshot if a lookup fails; rate limits/login errors are not counted as ban events.
- Persists real counters; no random profile statistics.

## Important limitations
Instagram does not provide a stable public API for all of this data. This project uses `instagrapi`, an unofficial client; Instagram may challenge, block or rate-limit requests. The minimum check interval is 120 seconds and defaults to 180 seconds. This cannot guarantee instant notification or 24/7 uptime. Use only accounts/data you are authorized to monitor and respect Instagram's terms.

A generic lookup error is **not** a ban. A specific not-found exception may mean renamed, deleted, suspended or unavailable, and is not definitive proof of a ban. If Instagram changes its response format, the monitor may report lookup errors instead of a status transition.

## Run locally
1. Create a Telegram bot with BotFather and copy its token.
2. Copy `.env.example` to `.env` and fill `TELEGRAM_BOT_TOKEN`. Configure `IG_SESSIONID` only if you are authorized to use that Instagram session. Never send session IDs in Telegram or commit `.env` to GitHub.
3. Install Python 3.12+ and dependencies:
   ```bash
   pip install -r requirements.txt
   python telegram_watchdog.py
   ```
4. Open your bot and tap **Start**. Use buttons to operate it.

## Deploy with GitHub + Docker host
Push these project files to a private or public GitHub repository (do not commit `.env`, session cookies or generated state files). Create a Docker service on a host that supports persistent disk and long-running processes. Set environment variables `TELEGRAM_BOT_TOKEN`, `IG_SESSIONID`, and optionally `CHECK_SECONDS=180` in the host dashboard. Attach persistent storage for `watchdog_store.json` and `ig_settings.json`, or monitor history may be lost on redeploy. Free hosting plans can sleep, restart or have usage limits; no free host can be promised to stay up 24/7.

## How monitoring works
Add usernames via the menu buttons. The bot first performs a successful lookup and stores that as the baseline. Every monitoring cycle it attempts a fresh lookup. If a verified profile changes, it sends a field-by-field diff. If lookup fails, it preserves the prior snapshot and does not fabricate stats. Recovery notifications are only possible if a previously observed unavailable state was actually detected and a later lookup succeeds.
