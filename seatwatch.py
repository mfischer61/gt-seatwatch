#!/usr/bin/env python3
"""
GT SeatWatch — alerts when a seat opens in a Georgia Tech section (by CRN)
or when a NEW section of a course appears.

Uses Banner 9 (registration.banner.gatech.edu). No GT login needed.
The old OSCAR endpoint gtcoursewatch used (bwckschd.p_disp_detail_sched) now 404s.

Run:  pip install requests pyyaml
      python seatwatch.py            # loop forever
      python seatwatch.py --once     # one pass (for cron / GitHub Actions)
      python seatwatch.py --test     # send a test notification
"""
import json, os, random, re, smtplib, sys, time
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

BASE = "https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb"
HERE = Path(__file__).parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text())
STATE_FILE = HERE / CFG.get("state_file", "state.json")
UA = {"User-Agent": "gt-seatwatch/1.0 (personal seat alert)"}


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
        # Banner search needs a cookie session bound to a term.
        if self.term == term:
            self.s.post(f"{BASE}/classSearch/resetDataForm", timeout=20)
            return
        self.s.cookies.clear()
        self.s.get(f"{BASE}/term/termSelection", params={"mode": "search"}, timeout=20)
        self.s.post(f"{BASE}/term/search", params={"mode": "search"},
                    data={"term": term}, timeout=20).raise_for_status()
        self.term = term

    def sections(self, term, subject, number):
        """All sections of SUBJ NUM -> {crn: info}. Includes live seat counts."""
        self._select_term(term)
        r = self.s.get(f"{BASE}/searchResults/searchResults", params={
            "txt_subject": subject, "txt_courseNumber": number, "txt_term": term,
            "pageOffset": 0, "pageMaxSize": 500,
            "sortColumn": "subjectDescription", "sortDirection": "asc"}, timeout=30)
        r.raise_for_status()
        out = {}
        for s in r.json().get("data") or []:
            out[s["courseReferenceNumber"]] = {
                "course": f'{s["subject"]} {s["courseNumber"]}',
                "section": s["sequenceNumber"],
                "title": s["courseTitle"],
                "seats": s["seatsAvailable"], "enrolled": s["enrollment"],
                "cap": s["maximumEnrollment"],
                "wl_seats": s["waitAvailable"], "wl_count": s["waitCount"],
                "wl_cap": s["waitCapacity"],
                "instructor": ", ".join(f["displayName"] for f in s.get("faculty", [])) or "TBA",
                "times": meeting_str(s),
            }
        return out

    def enrollment(self, term, crn):
        """Seat counts for one CRN. Stateless endpoint — fastest check."""
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
        return f'{f("courseNumber")} {f("sectionNumber")} — {f("courseTitle")}'


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
def notify(title, body, priority="high"):
    n = CFG.get("notify", {})
    print(f"\n🔔 {title}\n{body}\n", flush=True)
    reg = f"https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/registration"
    if n.get("ntfy_topic"):            # phone push — install the ntfy app, subscribe to topic
        try:
            requests.post(f'https://ntfy.sh/{n["ntfy_topic"]}', data=body.encode(),
                          headers={"Title": title, "Priority": priority, "Tags": "rotating_light",
                                   "Click": reg}, timeout=10)
        except Exception as e: print("ntfy failed:", e)
    if n.get("discord_webhook"):
        try:
            requests.post(n["discord_webhook"], json={"content": f"**{title}**\n{body}\n{reg}"}, timeout=10)
        except Exception as e: print("discord failed:", e)
    if n.get("pushover_user") and n.get("pushover_token"):
        try:
            requests.post("https://api.pushover.net/1/messages.json", data={
                "token": n["pushover_token"], "user": n["pushover_user"], "title": title,
                "message": body, "priority": 1, "url": reg}, timeout=10)
        except Exception as e: print("pushover failed:", e)
    if n.get("email", {}).get("to"):
        e = n["email"]
        try:
            msg = EmailMessage(); msg["Subject"] = title; msg["From"] = e["user"]; msg["To"] = e["to"]
            msg.set_content(f"{body}\n\n{reg}")
            with smtplib.SMTP_SSL(e.get("server", "smtp.gmail.com"), 465) as s:
                s.login(e["user"], e["app_password"]); s.send_message(msg)
        except Exception as ex: print("email failed:", ex)


# ------------------------------------------------------------------- Logic
def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {"crns": {}, "courses": {}, "terms": []}

def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=1))

def seat_events(prev, cur, waitlist_mode):
    """Return alert text if this snapshot is newly 'open' vs the last one."""
    ev = []
    if cur["seats"] > 0 and cur["wl_count"] == 0 and (prev is None or prev["seats"] <= 0 or prev["wl_count"] > 0):
        ev.append(f'OPEN SEAT: {cur["seats"]} available ({cur["enrolled"]}/{cur["cap"]})')
    elif cur["seats"] > 0 and cur["wl_count"] > 0 and (prev is None or prev["seats"] <= 0):
        ev.append(f'Seat opened but {cur["wl_count"]} on waitlist — goes to waitlist first')
    if waitlist_mode and cur["seats"] <= 0 and cur["wl_seats"] > 0 and (prev is None or prev["wl_seats"] <= 0):
        ev.append(f'WAITLIST SPOT: {cur["wl_seats"]} open ({cur["wl_count"]}/{cur["wl_cap"]})')
    return ev

def run_once(api, st):
    term = str(CFG["term"])
    wl = CFG.get("waitlist_alerts", True)

    # 0. Term watch — e.g. alert the moment Spring 2027 schedule posts
    for t in CFG.get("watch_terms", []) or []:
        t = str(t)
        if t not in st["terms"] and t in api.terms(t[:4]):
            st["terms"].append(t)
            notify(f"📅 Term {t} schedule posted", f"Banner now lists term {t}. Update config.term.")

    # 1. Specific CRNs — stateless enrollment endpoint
    for crn in map(str, CFG.get("crns", []) or []):
        cur = api.enrollment(term, crn)
        if cur is None: continue
        prev = st["crns"].get(crn)
        if prev is None or "name" not in prev:
            cur["name"] = api.course_of(term, crn)
        else:
            cur["name"] = prev["name"]
        for e in seat_events(prev, cur, wl):
            notify(f'CRN {crn}: {cur["name"]}', f'{e}\nCRN {crn} — register now.')
        st["crns"][crn] = cur
        time.sleep(random.uniform(0.5, 1.5))

    # 2. Whole courses — detect NEW sections + any section opening
    for c in CFG.get("courses", []) or []:
        subj, num = c.split()
        key = f"{term}:{subj} {num}"
        secs = api.sections(term, subj, num)
        if not secs: continue
        known = st["courses"].get(key)
        if known is not None:
            for crn in set(secs) - set(known):
                s = secs[crn]
                notify(f"🆕 New section: {s['course']} {s['section']}",
                       f"{s['title']}\nCRN {crn} · {s['instructor']}\n{s['times']}\n"
                       f"Seats {s['seats']}/{s['cap']} · WL {s['wl_count']}/{s['wl_cap']}")
            if CFG.get("course_seat_alerts", True):
                for crn, s in secs.items():
                    if crn in known:
                        for e in seat_events(known[crn], s, wl):
                            notify(f"{s['course']} {s['section']} (CRN {crn})",
                                   f"{s['title']} · {s['instructor']}\n{s['times']}\n{e}")
        st["courses"][key] = secs
        time.sleep(random.uniform(0.5, 1.5))


def main():
    if "--test" in sys.argv:
        notify("GT SeatWatch test", "Notifications working."); return
    api, st = Banner(), load_state()
    interval = CFG.get("interval_seconds", 60)
    while True:
        try:
            run_once(api, st); save_state(st)
            print(time.strftime("%H:%M:%S"), "checked", flush=True)
        except Exception as e:
            print("error:", e, flush=True); api = Banner()
        if "--once" in sys.argv: break
        time.sleep(interval + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    main()
