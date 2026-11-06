#!/usr/bin/env python3
"""
GT SeatWatch — Georgia Tech seat alerts via Banner 9 (no GT login needed).

  python seatwatch.py               loop forever (every interval_seconds)
  python seatwatch.py --once        one check (GitHub Actions / cron)
  python seatwatch.py --standby     dormant: only alert when a watch_terms schedule posts
  python seatwatch.py --status      print current seats for your CRNs   (+ --send → Telegram)
  python seatwatch.py --simulate    fake changes, print the alerts you'd get (+ --send → Telegram)
  python seatwatch.py --selftest    verify Banner, parsing, Telegram, state file
  python seatwatch.py --test        send one test notification
  python seatwatch.py --chat-id     print your Telegram chat ID

Telegram: send /status to your bot; it replies on the next check.
"""
import json, os, random, re, smtplib, sys, time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

BASE = "https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb"
REG_URL = f"{BASE}/registration"
HERE = Path(__file__).parent

# .env (KEY=value) — real env vars / GitHub secrets take priority.
_env = HERE / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8-sig").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

CFG = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
STATE_FILE = HERE / CFG.get("state_file", "state.json")
TZ = ZoneInfo(CFG.get("timezone", "America/New_York"))
UA = {"User-Agent": "gt-seatwatch/1.1 (personal seat alert)"}
MODE = {"dry": False, "prefix": ""}   # used by --simulate


def tg_creds():
    n = CFG.get("notify", {})
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or n.get("telegram_bot_token"),
            os.environ.get("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id"))


def watched_crns():
    """config crns: list [83179, ...] or mapping {83179: "ECE 2040 C"}."""
    c = CFG.get("crns") or {}
    if isinstance(c, list):
        return {str(x): None for x in c}
    return {str(k): (str(v) if v else None) for k, v in c.items()}


# ---------------------------------------------------------------- Banner API
class Banner:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.term = None

    def terms(self, query=""):
        r = self.s.get(f"{BASE}/classSearch/getTerms",
                       params={"searchTerm": query, "offset": 1, "max": 30}, timeout=20)
        r.raise_for_status()
        return {t["code"]: t["description"] for t in r.json()}

    def _select_term(self, term):
        if self.term == term:
            self.s.post(f"{BASE}/classSearch/resetDataForm", timeout=20)
            return
        self.s.cookies.clear()
        self.s.get(f"{BASE}/term/termSelection", params={"mode": "search"}, timeout=20)
        self.s.post(f"{BASE}/term/search", params={"mode": "search"},
                    data={"term": term}, timeout=20).raise_for_status()
        self.term = term

    def sections(self, term, subject, number):
        self._select_term(term)
        r = self.s.get(f"{BASE}/searchResults/searchResults", params={
            "txt_subject": subject, "txt_courseNumber": number, "txt_term": term,
            "pageOffset": 0, "pageMaxSize": 500,
            "sortColumn": "subjectDescription", "sortDirection": "asc"}, timeout=30)
        r.raise_for_status()
        out = {}
        for s in r.json().get("data") or []:
            out[s["courseReferenceNumber"]] = {
                "course": f'{s["subject"]} {s["courseNumber"]}', "section": s["sequenceNumber"],
                "title": s["courseTitle"],
                "seats": s["seatsAvailable"], "enrolled": s["enrollment"], "cap": s["maximumEnrollment"],
                "wl_seats": s["waitAvailable"], "wl_count": s["waitCount"], "wl_cap": s["waitCapacity"],
                "instructor": ", ".join(f["displayName"] for f in s.get("faculty", [])) or "TBA",
                "times": meeting_str(s),
            }
        return out

    def enrollment(self, term, crn):
        r = self.s.get(f"{BASE}/searchResults/getEnrollmentInfo",
                       params={"term": term, "courseReferenceNumber": crn}, timeout=20)
        r.raise_for_status()
        nums = dict(re.findall(r'status-bold">([^<]+):</span>\s*<span[^>]*>(-?\d+)', r.text))
        if not nums:
            return None
        g = lambda k: int(nums.get(k, 0))
        return {"seats": g("Enrollment Seats Available"), "enrolled": g("Enrollment Actual"),
                "cap": g("Enrollment Maximum"), "wl_seats": g("Waitlist Seats Available"),
                "wl_count": g("Waitlist Actual"), "wl_cap": g("Waitlist Capacity")}

    def course_of(self, term, crn):
        r = self.s.get(f"{BASE}/searchResults/getClassDetails",
                       params={"term": term, "courseReferenceNumber": crn}, timeout=20)
        f = lambda i: (re.search(rf'id="{i}"[^>]*>([^<]+)', r.text) or [None, "?"])[1].strip()
        return f'{f("courseTitle")} {f("sectionNumber")}'


def meeting_str(s):
    parts = []
    for m in s.get("meetingsFaculty", []):
        mt = m.get("meetingTime") or {}
        days = "".join(d for d, k in zip("MTWRFSU", ["monday", "tuesday", "wednesday",
                       "thursday", "friday", "saturday", "sunday"]) if mt.get(k))
        if days and mt.get("beginTime"):
            parts.append(f'{days} {mt["beginTime"]}-{mt["endTime"]}')
    return "; ".join(parts) or "TBA"


# ------------------------------------------------------------ Notifications
def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


URGENT_MARKS = ("🟢", "🟡 WAITLIST")   # seat open / waitlist spot open = act now


def level_for(ev):
    return "urgent" if any(e.startswith(URGENT_MARKS) for e in ev) else "info"


def notify(title, body, pre=False, level="urgent"):
    """level: urgent  → Telegram + email + ntfy(urgent) + Discord   (act now)
              summary → Telegram only                             (daily heartbeat, /status)
              info    → ntfy low priority only                    (FULL, seat freed behind waitlist)"""
    title = MODE["prefix"] + title
    print(f"\n🔔 [{level}] {title}\n{body}\n", flush=True)
    if MODE["dry"]:
        return
    n = dict(CFG.get("notify", {}) or {})
    # env vars / GitHub secrets override config.yaml (keep secrets out of the repo)
    n["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or n.get("ntfy_topic")
    e_cfg = dict(n.get("email") or {})
    for k, env in (("to", "EMAIL_TO"), ("user", "EMAIL_USER"), ("app_password", "EMAIL_APP_PASSWORD")):
        e_cfg[k] = os.environ.get(env) or e_cfg.get(k)
    n["email"] = e_cfg
    tok, chat = tg_creds()
    if tok and chat and level in ("urgent", "summary"):
        text = f"<b>{esc(title)}</b>\n" + (f"<pre>{esc(body)}</pre>" if pre else esc(body))
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={
                "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": "Open registration", "url": REG_URL}]]}},
                timeout=10)
            if not r.ok: print("telegram failed:", r.text)
        except Exception as e: print("telegram failed:", e)
    if n.get("ntfy_topic") and level in ("urgent", "info"):
        try:
            requests.post(f'https://ntfy.sh/{n["ntfy_topic"]}', data=body.encode(),
                          headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                                   "Priority": "urgent" if level == "urgent" else "low",
                                   "Tags": "rotating_light" if level == "urgent" else "information_source", "Click": REG_URL}, timeout=10)
        except Exception as e: print("ntfy failed:", e)
    if n.get("discord_webhook") and level == "urgent":
        try:
            requests.post(n["discord_webhook"], json={"content": f"**{title}**\n{body}"}, timeout=10)
        except Exception as e: print("discord failed:", e)
    if (n.get("email") or {}).get("to") and level == "urgent":
        e = n["email"]
        try:
            msg = EmailMessage(); msg["Subject"] = title; msg["From"] = e["user"]; msg["To"] = e["to"]
            msg.set_content(f"{body}\n\n{REG_URL}")
            with smtplib.SMTP_SSL(e.get("server", "smtp.gmail.com"), 465) as s:
                s.login(e["user"], e["app_password"]); s.send_message(msg)
        except Exception as ex: print("email failed:", ex)


# ------------------------------------------------------------------- Logic
def load_state():
    try: st = json.loads(STATE_FILE.read_text())
    except Exception: st = {}
    for k, v in (("crns", {}), ("courses", {}), ("terms", []), ("errors", 0)):
        st.setdefault(k, v)
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=1))


FIELDS = (("seats", "open seats"), ("enrolled", "enrolled"), ("cap", "capacity"),
          ("wl_count", "waitlist"), ("wl_cap", "WL capacity"))


def seat_events(prev, cur, waitlist_alerts, any_change):
    """Alert lines for prev → cur. First sighting (prev None) is a silent baseline."""
    if prev is None:
        return []
    ev = []
    was_open = prev["seats"] > 0 and prev["wl_count"] == 0
    now_open = cur["seats"] > 0 and cur["wl_count"] == 0
    if now_open and not was_open:
        ev.append(f'🟢 OPEN SEAT — {cur["seats"]} available. Register now.')
    elif cur["seats"] > 0 and cur["wl_count"] > 0 and prev["seats"] <= 0:
        ev.append(f'🟡 Seat freed, but {cur["wl_count"]} on waitlist (they get it first).')
    if waitlist_alerts and cur["seats"] <= 0 and cur["wl_seats"] > 0 and cur["wl_seats"] > prev["wl_seats"]:
        if prev["wl_seats"] <= 0:
            ev.append(f'🟡 WAITLIST SPOT — {cur["wl_seats"]} open. Join waitlist.')
        else:   # more spots: waitlist expanded (cap raised) or someone left it
            why = f' (WL cap {prev["wl_cap"]}→{cur["wl_cap"]})' if cur["wl_cap"] > prev["wl_cap"] else ""
            ev.append(f'🟡 WAITLIST SPOTS UP — {prev["wl_seats"]}→{cur["wl_seats"]} open{why}. Join waitlist.')
    if was_open and cur["seats"] <= 0:
        ev.append("🔴 Section now FULL.")
    d = [f"{lbl} {prev[k]}→{cur[k]}" for k, lbl in FIELDS if prev.get(k) != cur.get(k)]
    if d and (ev or any_change):
        ev.append("Δ " + ", ".join(d))
    return ev


def now_line(d):
    return f'Now: {d["seats"]} open · {d["enrolled"]}/{d["cap"]} enrolled · WL {d["wl_count"]}/{d["wl_cap"]}'


def status_text(snap):
    rows = [f'{"Class":<14}{"CRN":<7}{"Open":>5} {"Enr/Cap":>8} {"WL":>6}']
    for crn, d in snap.items():
        if not d:
            rows.append(f'{"?":<14}{crn:<7}  not found'); continue
        flag = "OPEN" if d["seats"] > 0 else ("WL" if d["wl_seats"] > 0 else "FULL")
        rows.append(f'{d["name"][:13]:<14}{crn:<7}{d["seats"]:>5} {str(d["enrolled"])+"/"+str(d["cap"]):>8} '
                    f'{str(d["wl_count"])+"/"+str(d["wl_cap"]):>6} {flag}')
    return "\n".join(rows)


def snapshot(api, term):
    out = {}
    for crn, label in watched_crns().items():
        d = api.enrollment(term, crn)
        if d:
            d["name"] = label or api.course_of(term, crn)
        out[crn] = d
        time.sleep(0.3)
    return out


def handle_telegram_commands(api, st):
    """Reply to /status sent to the bot (answered on the next check)."""
    tok, chat = tg_creds()
    if not tok or not chat or MODE["dry"]:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates",
                         params={"offset": st.get("tg_offset", 0), "timeout": 0}, timeout=15).json()
    except Exception:
        return
    want = False
    for u in r.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == str(chat) and (m.get("text") or "").startswith("/status"):
            want = True
    if want:
        notify("📊 Current seats", status_text(snapshot(api, str(CFG["term"]))), pre=True, level="summary")


def check_terms(api, st):
    for t in map(str, CFG.get("watch_terms") or []):
        if t not in st["terms"] and t in api.terms(t[:4]):
            st["terms"].append(t)
            notify(f"📅 Term {t} schedule posted",
                   f"Banner now lists term {t}.\nTo activate: set term: {t} + CRNs in config.yaml, push, then\n"
                   f"gh variable set SEATWATCH_ACTIVE --body true")


def run_once(api, st):
    term = str(CFG["term"])
    wl = CFG.get("waitlist_alerts", True)
    if st.get("term") != term:          # new term → fresh baseline, no stale comparisons
        st.update(term=term, crns={}, courses={})

    check_terms(api, st)

    for crn, label in watched_crns().items():
        cur = api.enrollment(term, crn)
        if cur is None:
            print(f"CRN {crn}: no data (wrong term or CRN?)"); continue
        prev = st["crns"].get(crn)
        cur["name"] = label or (prev or {}).get("name") or api.course_of(term, crn)
        ev = seat_events(prev, cur, wl, CFG.get("alert_on_any_change", True))
        if ev:
            notify(f'{cur["name"]} · CRN {crn}', "\n".join(ev) + "\n" + now_line(cur), level=level_for(ev))
        st["crns"][crn] = cur
        time.sleep(random.uniform(0.3, 1.0))

    for c in CFG.get("courses") or []:
        subj, num = c.split()
        key = f"{term}:{subj} {num}"
        secs = api.sections(term, subj, num)
        if not secs: continue
        known = st["courses"].get(key)
        if known is not None:
            for crn in set(secs) - set(known):
                s = secs[crn]
                notify(f"🆕 New section: {s['course']} {s['section']}",
                       f"{s['title']}\nCRN {crn} · {s['instructor']}\n{s['times']}\n{now_line(s)}")
            if CFG.get("course_seat_alerts", True):
                for crn, s in secs.items():
                    if crn in known and crn not in watched_crns():   # CRN loop already alerts these
                        ev = seat_events(known[crn], s, wl, CFG.get("course_any_change", False))
                        if ev:
                            notify(f"{s['course']} {s['section']} · CRN {crn}",
                                   f"{s['title']} · {s['instructor']}\n{s['times']}\n" + "\n".join(ev), level=level_for(ev))
        st["courses"][key] = secs
        time.sleep(random.uniform(0.3, 1.0))

    handle_telegram_commands(api, st)

    h = CFG.get("daily_summary_hour")
    now = datetime.now(TZ)
    if h is not None and now.hour >= int(h) and st.get("last_summary") != now.date().isoformat():
        st["last_summary"] = now.date().isoformat()
        snap = {c: st["crns"].get(c) for c in watched_crns()}
        notify(f"📊 Daily seat summary · {now:%a %b %d}", status_text(snap) + "\n(SeatWatch is alive)", pre=True, level="summary")


# --------------------------------------------------------------- Simulation
class SimBanner:
    """Wraps the real API; returns real data with scripted changes applied."""
    def __init__(self, real, cur_crns, course_secs):
        self.real, self.cur_crns, self.course_secs = real, cur_crns, course_secs
    def __getattr__(self, n): return getattr(self.real, n)
    def enrollment(self, term, crn): return dict(self.cur_crns[crn]) if crn in self.cur_crns else None
    def sections(self, term, subj, num): return self.course_secs.get(f"{subj} {num}", {})


def scenario(i, d):
    """Return (scenario name, fake previous, fake current) built from real capacity."""
    cap, wlc = d["cap"], d["wl_cap"]
    base = dict(d)
    k = i % 5
    if k == 1 and wlc == 0: k = 2
    if k == 2 and d["seats"] <= 0 and d["wl_cap"] <= d["wl_count"]: k = 0
    if k == 0:
        return "someone drops a full class", {**base, "seats": 0, "enrolled": cap, "wl_count": 0, "wl_seats": wlc}, \
               {**base, "seats": 1, "enrolled": cap - 1, "wl_count": 0, "wl_seats": wlc}
    if k == 1:
        return "waitlist spot opens", {**base, "seats": 0, "enrolled": cap, "wl_count": wlc, "wl_seats": 0}, \
               {**base, "seats": 0, "enrolled": cap, "wl_count": wlc - 1, "wl_seats": 1}
    if k == 2 and d["seats"] > 0:
        return "one more student enrolls", base, \
               {**base, "seats": d["seats"] - 1, "enrolled": d["enrolled"] + 1}
    if k == 2:
        return "someone joins the waitlist", base, \
               {**base, "wl_count": d["wl_count"] + 1, "wl_seats": d["wl_seats"] - 1}
    if k == 3:
        return "last seat taken", {**base, "seats": 1, "enrolled": cap - 1, "wl_count": 0}, \
               {**base, "seats": 0, "enrolled": cap, "wl_count": 0}
    return "seat frees but waitlist exists", {**base, "seats": 0, "enrolled": cap, "wl_count": 3, "wl_seats": max(wlc - 3, 0)}, \
           {**base, "seats": 1, "enrolled": cap - 1, "wl_count": 3, "wl_seats": max(wlc - 3, 0)}


def simulate(api):
    MODE["dry"] = "--send" not in sys.argv
    MODE["prefix"] = "[SIMULATED] "
    term = str(CFG["term"])
    print("Fetching real data...", flush=True)
    real = {c: d for c, d in snapshot(api, term).items() if d}
    st = {"crns": {}, "courses": {}, "terms": [str(t) for t in CFG.get("watch_terms") or []],
          "errors": 0, "last_summary": datetime.now(TZ).date().isoformat(), "tg_offset": 0}
    cur = {}
    print("\nScenarios:")
    for i, (crn, d) in enumerate(real.items()):
        name, p, c = scenario(i, d)
        st["crns"][crn], cur[crn] = p, c
        print(f"  CRN {crn} ({d['name']}): {name}")
    course_secs = {}
    for c in CFG.get("courses") or []:
        subj, num = c.split()
        secs = api.sections(term, subj, num)
        st["courses"][f"{term}:{c}"] = secs
        fake = dict(next(iter(secs.values()))) if secs else {"course": c, "title": "?", "cap": 30, "wl_cap": 10}
        fake.update(section="ZZ", seats=30, enrolled=0, cap=30, wl_count=0, wl_seats=fake.get("wl_cap", 10),
                    instructor="Simulated Prof", times="TR 0930-1045")
        course_secs[c] = {**secs, "99999": fake}
        print(f"  {c}: new section CRN 99999 appears")
    print("\n—— Alerts you would get ——")
    run_once(SimBanner(api, cur, course_secs), st)
    notify("📊 Current seats (REAL data)", status_text(real), pre=True, level="summary")
    print("(dry run — nothing sent; add --send to push these to Telegram)" if MODE["dry"] else "(sent to Telegram)")


# ---------------------------------------------------------------- Self-test
def selftest(api):
    ok = True
    def check(name, fn):
        nonlocal ok
        try:
            msg = fn(); print(f"PASS  {name}" + (f" — {msg}" if msg else ""))
        except Exception as e:
            ok = False; print(f"FAIL  {name} — {e}")
    term = str(CFG["term"])
    check("Banner reachable + term exists", lambda: (lambda t: t[term] if term in t else (_ for _ in ()).throw(
        Exception(f"term {term} not listed")))(api.terms(term[:4])))
    def crns():
        bad = [c for c in watched_crns() if not api.enrollment(term, c)]
        if bad: raise Exception(f"no data for {bad}")
        return f"{len(watched_crns())} CRNs parsed"
    check("CRN seat parsing", crns)
    for c in CFG.get("courses") or []:
        check(f"Course search {c}", lambda c=c: f"{len(api.sections(term, *c.split()))} sections")
    def tg():
        tok, chat = tg_creds()
        if not tok or not chat: raise Exception("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing (.env or secrets)")
        me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
        if not me.get("ok"): raise Exception(f"bad token: {me}")
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": "✅ SeatWatch self-test passed Telegram check."}, timeout=10).json()
        if not r.get("ok"): raise Exception(f"send failed: {r.get('description')}")
        return f"@{me['result']['username']} → chat {chat}"
    check("Telegram token + delivery", tg)
    def statefile():
        save_state(load_state()); return str(STATE_FILE.name)
    check("State file writable", statefile)
    def logic():
        a = {"seats": 0, "enrolled": 10, "cap": 10, "wl_count": 0, "wl_seats": 5, "wl_cap": 5}
        assert any("OPEN" in e for e in seat_events(a, {**a, "seats": 1, "enrolled": 9}, True, True))
        assert seat_events(None, a, True, True) == []
        assert seat_events(a, a, True, True) == []
        return "open / baseline / no-change cases"
    check("Alert logic unit checks", logic)
    print("\nALL GOOD" if ok else "\nFix the FAIL lines above."); return ok


def telegram_chat_id():
    tok, _ = tg_creds()
    if not tok: sys.exit("Set TELEGRAM_BOT_TOKEN in .env first.")
    ups = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10).json().get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
             for u in ups if "message" in u}
    if not chats: sys.exit("No messages yet. Send your bot any message in Telegram, then rerun.")
    for cid, name in chats.items(): print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def main():
    a = sys.argv
    if "--chat-id" in a: return telegram_chat_id()
    if "--test" in a: return notify("GT SeatWatch test", "Notifications working.")
    api = Banner()
    if "--selftest" in a: sys.exit(0 if selftest(api) else 1)
    if "--simulate" in a: return simulate(api)
    if "--status" in a:
        MODE["dry"] = "--send" not in a
        return notify("📊 Current seats", status_text(snapshot(api, str(CFG["term"]))), pre=True, level="summary")

    st = load_state()
    if "--standby" in a:                 # dormant mode: only watch for new term schedules
        check_terms(api, st); save_state(st)
        print(time.strftime("%H:%M:%S"), "standby check done", flush=True)
        return
    interval = CFG.get("interval_seconds", 60)
    # --minutes N : loop for N minutes then exit (GitHub Actions near-real-time mode)
    stop_at = None
    if "--minutes" in a:
        stop_at = time.time() + float(a[a.index("--minutes") + 1]) * 60
    while True:
        try:
            first = not st["crns"]
            run_once(api, st)
            st["errors"] = 0
            print(time.strftime("%H:%M:%S"), "checked" + (" (baseline saved — alerts start next check)" if first else ""), flush=True)
        except Exception as e:
            st["errors"] += 1
            print("error:", e, flush=True)
            if st["errors"] == 3:
                notify("⚠️ SeatWatch failing", f"3 checks in a row failed.\nLast error: {e}")
            api = Banner()
            if "--once" in a:
                save_state(st); sys.exit(1)
        save_state(st)
        if "--once" in a: break
        if stop_at and time.time() + interval >= stop_at: break
        time.sleep(interval + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    main()
