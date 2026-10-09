# GT SeatWatch

Alerts on: open seat (by CRN), open waitlist spot, NEW section of a course, new term posted.

## Setup
1. `pip install -r requirements.txt`
2. Edit `config.yaml` — term, CRNs, courses.
3. Telegram: message **@BotFather** → `/newbot` → copy token.
   `$env:TELEGRAM_BOT_TOKEN="<token>"`, message your new bot once, then
   `python seatwatch.py --chat-id` → `$env:TELEGRAM_CHAT_ID="<id>"`.
   GitHub Actions: `gh secret set TELEGRAM_BOT_TOKEN` and `gh secret set TELEGRAM_CHAT_ID`.
4. `python seatwatch.py --test` → confirm push.
5. `python seatwatch.py` → runs every ~60s.

## Run 24/7
- **Best:** Raspberry Pi / Oracle Cloud free VM / any VPS → `nohup python seatwatch.py &` or systemd.
- **Free, slower:** push to a *private* GitHub repo; `.github/workflows/watch.yml` runs every ~5 min.
- **Laptop:** works only while awake.

## Endpoints (Banner 9, no login)
- `GET  /ssb/searchResults/getEnrollmentInfo?term=&courseReferenceNumber=` — seats for 1 CRN
- `POST /ssb/term/search?mode=search` (term=) → cookie, then
  `GET /ssb/searchResults/searchResults?txt_subject=&txt_courseNumber=&txt_term=` — all sections, JSON
- `GET  /ssb/classSearch/getTerms?searchTerm=` — available terms

## Commands
- `python seatwatch.py --status` — current seats for your CRNs (`--send` → Telegram)
- `python seatwatch.py --simulate` — fake changes, shows alerts you'd get (`--send` → Telegram)
- `python seatwatch.py --selftest` — checks Banner, parsing, Telegram, state file
- Telegram: send `/status` to your bot → reply on next check
- Daily summary at `daily_summary_hour` = heartbeat; no summary = something broke

## Dormant / active
- Dormant (default): `standby.yml` runs daily, alerts only when a `watch_terms` schedule posts.
- Activate: update `term` + `crns` in config.yaml, push, `gh variable set SEATWATCH_ACTIVE --body true`
- Deactivate: `gh variable set SEATWATCH_ACTIVE --body false`
