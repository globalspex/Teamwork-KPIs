#!/usr/bin/env python3
"""
Pulls Teamwork Desk KPIs and writes data/desk-live.json.

API: Desk v1 (https://<site>.teamwork.com/desk/v1), Basic auth with the
Teamwork API key as username and "x" as password. The newer /desk/api/v2
rejects this key (401), so stay on v1.

v1 quirks learned from the probe (Oct 2026):
  - tickets.json ignores status/type/inbox filters, but DOES honor
    sortBy=createdAt&sortDir=desc and pageSize=100. So recent tickets are read
    newest-first and paging stops once we pass the lookback window.
  - tickets/search.json honors statuses[]=<numeric id> (codes like "active"
    give a 400). Results can include other statuses, so we always re-filter
    client-side on the ticket's own status.
  - Ticket "status" is the status CODE (e.g. "active", "waiting", "wordfence").
    "type" is the type NAME (e.g. "Problem"). "inboxId" is numeric.
  - responseTimes.firstResponseTime / resolutionTime are BUSINESS-HOURS
    MINUTES computed by Desk (0 means none yet).

Scope (agreed with Christina, Oct 2026):
  - Inboxes: GlobalSpex Support (699) and Moving Marketing Support (18742).
  - Times use Desk's business-hours numbers.
  - Automated noise is excluded by status AND type (see NOISE_* below),
    plus spam and merged tickets.
  - Weekly KPIs use the last full Monday-Sunday week (America/Chicago).
"""

import os, sys, json, time, base64, datetime, urllib.request, urllib.parse, urllib.error
from zoneinfo import ZoneInfo

SITE = os.environ.get("TEAMWORK_SITE", "globalspex")
KEY = os.environ["TEAMWORK_PASSWORD"]
TZ = ZoneInfo(os.environ.get("TEAMWORK_TIMEZONE", "America/Chicago"))
BASE = f"https://{SITE}.teamwork.com/desk/v1"
AUTH = "Basic " + base64.b64encode(f"{KEY}:x".encode()).decode()

INBOXES = {699, 18742}
BUSINESS_DAY_MIN = 8 * 60          # "1 business day" = 8 business hours
WEEKS_BACK = 6                     # weekly series length (full weeks)
AGING_LOOKBACK_DAYS = 30

NOISE_STATUSES = {"wordfence", "wp-remote-notifications", "knowhost-notifications", "pulsetic",
                  "technical-report", "website-performace", "spam", "merged"}
NOISE_TYPES = {"wordfence", "wp remote", "alerts/warnings", "report"}
STATUS_IDS = {"active": 1, "waiting": 3, "on-hold": 4}

# Desk user IDs (from /desk/v1/users.json) -> dashboard keys.
AGENTS = {226032: "rebecaq", 304707: "juliane", 605403: "rebecam", 604063: "andressa",
          296553: "aura", 273488: "javier", 610482: "jennifer", 116205: "christina"}

FOLLOWUP_TYPES = {"problem", "question", "request", "marketing/seo"}
DEV_RESOLUTION_TYPES = {"request", "unspecified"}
DEV_RESOLUTION_STATUSES = {"close-followup", "waiting", "on-hold", "solved", "closed"}
DEV_RESOLUTION_GOAL_MIN = 2 * BUSINESS_DAY_MIN


def get(path, params):
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(int(e.headers.get("Retry-After") or 5 * (attempt + 1)))
                continue
            raise RuntimeError(f"Desk {path} HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    raise RuntimeError(f"Desk {path}: retries exhausted")


def ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def norm(t):
    a = t.get("assignedTo")
    rt = t.get("responseTimes") or {}
    return {
        "id": t["id"], "created": ts(t["createdAt"]), "updated": ts(t["updatedAt"]),
        "status": str(t.get("status") or "").lower(), "type": str(t.get("type") or "").lower(),
        "inbox": t.get("inboxId"), "assignee": a.get("id") if isinstance(a, dict) else a,
        "merged": bool(t.get("mergedToId")),
        "first_resp": rt.get("firstResponseTime") or 0, "resolution": rt.get("resolutionTime") or 0,
        "responses": rt.get("responseCount") or 0,
        "customer": (t.get("customer") or {}).get("id"),
    }


BOT_CUSTOMERS = set()
BOT_MIN_TICKETS = 15        # a sender with this many tickets in the window...
BOT_MAX_REPLY_RATE = 0.10   # ...that almost never get a human reply is automated


def find_bots(tickets):
    """Senders who open lots of tickets that nobody replies to (uptime monitors,
    plugin/host notices, form-notification relays). Closed alert tickets lose
    their telltale status, so this is the reliable noise filter."""
    stats = {}
    for t in tickets:
        c = t["customer"]
        if c is None:
            continue
        n, r = stats.get(c, (0, 0))
        stats[c] = (n + 1, r + (1 if t["responses"] > 0 else 0))
    return {c for c, (n, r) in stats.items() if n >= BOT_MIN_TICKETS and r / n <= BOT_MAX_REPLY_RATE}


def is_real(t):
    return (t["inbox"] in INBOXES and not t["merged"] and t["customer"] not in BOT_CUSTOMERS
            and t["status"] not in NOISE_STATUSES and t["type"] not in NOISE_TYPES)


OPEN_STATUSES = {"active", "waiting", "on-hold"}


def fetch_recent(since):
    """Newest-first ticket list until createdAt drops below `since`."""
    out, page, pages = [], 1, None
    while True:
        d = get("tickets.json", {"sortBy": "createdAt", "sortDir": "desc", "pageSize": 100, "page": page})
        batch = [norm(t) for t in d.get("tickets", [])]
        pages = d.get("maxPages") or pages
        out.extend(batch)
        if not batch or batch[-1]["created"] < since or (pages and page >= pages):
            break
        page += 1
        time.sleep(0.25)
    return [t for t in out if t["created"] >= since], page


def fetch_by_status(code):
    """All tickets currently in a status (search endpoint, re-filtered client-side)."""
    out, page = {}, 1
    while True:
        d = get("tickets/search.json", {"statuses[]": STATUS_IDS[code], "page": page})
        for t in d.get("tickets", []):
            n = norm(t)
            if n["status"] == code:
                out[n["id"]] = n
        if page >= (d.get("maxPages") or 1) or not d.get("tickets"):
            break
        page += 1
        time.sleep(0.25)
    return list(out.values())


def avg_hours(mins):
    return round(sum(mins) / len(mins) / 60, 1) if mins else None


def week_metrics(tickets, start, end):
    wk = [t for t in tickets if start <= t["created"] < end and is_real(t)]
    prob = [t["first_resp"] for t in wk if t["type"] == "problem" and t["first_resp"] > 0]
    req = [t["first_resp"] for t in wk if t["type"] == "request" and t["first_resp"] > 0]
    fu = [t for t in wk if t["type"] in FOLLOWUP_TYPES]
    fu_hit = sum(1 for t in fu if 0 < t["first_resp"] <= BUSINESS_DAY_MIN)
    fu_n = sum(1 for t in fu if t["first_resp"] > 0 or t["status"] in OPEN_STATUSES)
    devs = {}
    for t in wk:
        key = AGENTS.get(t["assignee"])
        if not key or t["type"] not in DEV_RESOLUTION_TYPES or t["status"] not in DEV_RESOLUTION_STATUSES:
            continue
        if t["resolution"] <= 0 or t["responses"] == 0:
            continue
        d = devs.setdefault(key, {"on_time": 0, "n": 0, "res_min": []})
        d["n"] += 1
        d["res_min"].append(t["resolution"])
        if t["resolution"] <= DEV_RESOLUTION_GOAL_MIN:
            d["on_time"] += 1
    for d in devs.values():
        d["pct_on_time"] = round(d["on_time"] / d["n"] * 100, 1) if d["n"] else None
        d["avg_resolution_hrs"] = avg_hours(d.pop("res_min"))
    return {
        "tickets": len(wk),
        "problem": {"avg_first_response_hrs": avg_hours(prob), "n": len(prob)},
        "request": {"avg_first_response_hrs": avg_hours(req), "n": len(req)},
        "followup_1day": {"hit": fu_hit, "n": fu_n, "pct": round(fu_hit / fu_n * 100, 1) if fu_n else None},
        "resolution_by_agent": devs,
    }


def main():
    now = datetime.datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - datetime.timedelta(days=today.weekday())
    week_starts = [this_monday - datetime.timedelta(weeks=i) for i in range(WEEKS_BACK, 0, -1)]
    since = min(week_starts[0], today - datetime.timedelta(days=AGING_LOOKBACK_DAYS))

    result = {"generated_at": now.isoformat(), "errors": []}
    try:
        recent, pages_read = fetch_recent(since)
        current = {code: fetch_by_status(code) for code in STATUS_IDS}
        BOT_CUSTOMERS.update(find_bots(recent))
        print(f"Read {len(recent)} recent tickets ({pages_read} pages); "
              + ", ".join(f"{k}: {len(v)}" for k, v in current.items()))

        active = [t for t in current["active"] if is_real(t)]
        waiting = [t for t in current["waiting"] if is_real(t)]
        on_hold = [t for t in current["on-hold"] if is_real(t)]
        two_days_ago = now - datetime.timedelta(days=2)
        result["company"] = {
            "open_tickets": len(active),
            "new_today": sum(1 for t in recent if t["created"] >= today and is_real(t)),
            "waiting_on_customer": len(waiting),
            "waiting_no_update_2d": sum(1 for t in waiting if t["updated"] < two_days_ago),
            "on_hold": len(on_hold),
        }
        aging_cut = today - datetime.timedelta(days=AGING_LOOKBACK_DAYS)
        result["andressa"] = {"aging_tickets": sum(
            1 for t in active + waiting
            if t["type"] in ("request", "problem") and t["assignee"] and t["created"] >= aging_cut)}

        weeks = []
        for ws in week_starts:
            m = week_metrics(recent, ws, ws + datetime.timedelta(weeks=1))
            m["week"] = f"{ws.month}/{ws.day}"
            m["range"] = f"{ws:%-m/%-d}\u2013{(ws + datetime.timedelta(days=6)):%-m/%-d}"
            weeks.append(m)
        result["weeks"] = weeks
        result["last_full_week"] = weeks[-1]
        result["debug"] = {
            "bot_senders": len(BOT_CUSTOMERS),
            "bot_tickets_in_window": sum(1 for t in recent if t["customer"] in BOT_CUSTOMERS),
            "customers_real_in_window": len({t["customer"] for t in recent if is_real(t)}),
            "recent_scanned": len(recent), "recent_real": sum(1 for t in recent if is_real(t)),
            "recent_by_inbox": {str(k): sum(1 for t in recent if t["inbox"] == k) for k in sorted({t["inbox"] for t in recent}, key=str)},
            "recent_by_status": {s: sum(1 for t in recent if t["status"] == s) for s in sorted({t["status"] for t in recent})},
            "real_by_type": {s: sum(1 for t in recent if is_real(t) and t["type"] == s) for s in sorted({t["type"] for t in recent if is_real(t)})},
        }
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        result["errors"].append(str(e))

    os.makedirs("data", exist_ok=True)
    # Daily snapshot log: Desk only knows CURRENT status, so point-in-time counts
    # (aging, open, waiting) are saved once per day to build history going forward.
    log_path = "data/desk-snapshots.json"
    try:
        snaps = json.load(open(log_path)) if os.path.exists(log_path) else []
    except Exception:
        snaps = []
    if "company" in result:
        day = now.date().isoformat()
        snaps = [x for x in snaps if x["date"] != day] + [{
            "date": day, "aging": result["andressa"]["aging_tickets"],
            "open": result["company"]["open_tickets"], "waiting": result["company"]["waiting_on_customer"],
            "waiting_stale": result["company"]["waiting_no_update_2d"], "on_hold": result["company"]["on_hold"]}]
        snaps = sorted(snaps, key=lambda x: x["date"])[-400:]
        with open(log_path, "w") as f:
            json.dump(snaps, f, indent=1)
    result["snapshots"] = snaps
    with open("data/desk-live.json", "w") as f:
        json.dump(result, f, indent=2)
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
