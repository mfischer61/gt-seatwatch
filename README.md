# GT SeatWatch

Alerts on: open seat (by CRN), open waitlist spot, NEW section of a course, new term posted.

## Setup
1. `pip install -r requirements.txt`
2. Edit `config.yaml` — term, CRNs, courses.
3. Phone push: install **ntfy** app → subscribe to your `ntfy_topic` (make it random/unguessable).
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
