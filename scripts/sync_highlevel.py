#!/usr/bin/env python3
"""
Pulls Christina's sales KPIs from HighLevel and writes data/highlevel-live.json.

Definitions (agreed with Christina, Oct 2026):
  - Leads:     every opportunity in the "FU Leads" pipeline, counted in the
               week it was CREATED, no matter what stage it's in today.
  - Proposals: FU Leads opportunities counted in the week they MOVED INTO the
               "Proposal" stage.
  - Converted: every opportunity in the "Onboarding" pipeline, counted in the
               week it was CREATED, no matter what stage it's in today.
  Leads and Converted are deduplicated by contact (a contact with two
  opportunities counts once, on the week of their earliest one).

Why there's a proposal log:
  HighLevel only stores an opportunity's MOST RECENT stage change
  (lastStageChangeAt). Once a proposal moves on to Won/Lost, the date it
  entered Proposal is gone. So every run, any opportunity sitting in Proposal
  gets recorded in data/highlevel-proposal-log.json with the date it entered
  that stage. Entries are never removed. From the day this started running,
  proposals are exact. Before that, only proposals still sitting in Proposal
  on the first run could be dated.

Auth: a HighLevel Private Integration token (sub-account level) with the
"View Opportunities" scope, stored in the HIGHLEVEL_TOKEN repo secret.
"""

import os, sys, json, datetime, urllib.request, urllib.parse, urllib.error
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("HIGHLEVEL_TOKEN", "")
LOCATION_ID = os.environ.get("HIGHLEVEL_LOCATION_ID", "rPYX5zSqcTbMLPgErcSD")
TZ = ZoneInfo(os.environ.get("HIGHLEVEL_TIMEZONE", "America/Chicago"))
BASE = "https://services.leadconnectorhq.com"
WEEKS = 52

# IDs confirmed from GET /opportunities/pipelines on Oct 2, 2026.
FU_LEADS_PIPELINE = "eRSoddqG3NtcgSzf4ltI"
PROPOSAL_STAGE = "7ec4c1aa-59e1-482a-959b-ca5d744779ce"
ONBOARDING_PIPELINE = "U2rdmyCuXA37fuAV1tP7"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(ROOT, "data", "highlevel-live.json")
LOG_FILE = os.path.join(ROOT, "data", "highlevel-proposal-log.json")


def fetch_pipeline(pipeline_id):
    """Every opportunity in one pipeline, following HighLevel's cursor paging."""
    opps, params = [], {"location_id": LOCATION_ID, "pipeline_id": pipeline_id, "limit": 100}
    for _ in range(100):
        url = f"{BASE}/opportunities/search?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOKEN}", "Version": "2021-07-28",
            "Accept": "application/json", "User-Agent": "globalspex-kpi-sync"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HighLevel API error {e.code}: {e.read().decode('utf-8','replace')[:400]}") from e
        batch = data.get("opportunities", [])
        opps.extend(batch)
        meta = data.get("meta", {})
        if len(batch) < params["limit"] or not meta.get("startAfterId"):
            break
        params["startAfter"], params["startAfterId"] = meta["startAfter"], meta["startAfterId"]
    return opps


def local_date(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).date()


def week_start(d):
    return d - datetime.timedelta(days=d.weekday())  # Monday


def first_per_contact(opps):
    """Earliest opportunity per contact (falls back to opp id if no contact)."""
    best = {}
    for o in opps:
        key = o.get("contactId") or o["id"]
        if key not in best or o["createdAt"] < best[key]["createdAt"]:
            best[key] = o
    return list(best.values())


def bucket(dates, weeks):
    counts = {w: 0 for w in weeks}
    for d in dates:
        w = week_start(d)
        if w in counts:
            counts[w] += 1
    return [counts[w] for w in weeks]


def compute(fu_opps, onboarding_opps, log, today):
    # 1. Record anything sitting in Proposal right now (never removed later).
    for o in fu_opps:
        if o.get("pipelineStageId") == PROPOSAL_STAGE and o["id"] not in log:
            entered = o.get("lastStageChangeAt") or o.get("updatedAt") or o["createdAt"]
            log[o["id"]] = {"entered": local_date(entered).isoformat(), "name": o.get("name", "")}

    this_week = week_start(today)
    weeks = [this_week - datetime.timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]

    lead_dates = [local_date(o["createdAt"]) for o in first_per_contact(fu_opps)]
    conv_dates = [local_date(o["createdAt"]) for o in first_per_contact(onboarding_opps)]
    prop_dates = [datetime.date.fromisoformat(v["entered"]) for v in log.values()]

    leads, props, conv = bucket(lead_dates, weeks), bucket(prop_dates, weeks), bucket(conv_dates, weeks)

    def summary(series):
        return {"this_week": series[-1], "last_week": series[-2], "last_12_months": sum(series),
                "avg_per_week_last_12_weeks": round(sum(series[-12:]) / 12, 1)}

    return {
        "weeks": [f"{w.month}/{w.day}" for w in weeks],
        "week_start_dates": [w.isoformat() for w in weeks],
        "leads": leads, "proposals": props, "converted": conv,
        "summary": {"leads": summary(leads), "proposals": summary(props), "converted": summary(conv)},
    }


def main():
    now = datetime.datetime.now(TZ)
    result = {"generated_at": now.isoformat(), "timezone": str(TZ), "christina": None, "errors": []}
    try:
        log = json.load(open(LOG_FILE)) if os.path.exists(LOG_FILE) else {}
    except Exception:
        log = {}
    try:
        if not TOKEN:
            raise RuntimeError("HIGHLEVEL_TOKEN secret is not set.")
        fu = fetch_pipeline(FU_LEADS_PIPELINE)
        onb = fetch_pipeline(ONBOARDING_PIPELINE)
        print(f"Fetched {len(fu)} FU Leads and {len(onb)} Onboarding opportunities.")
        result["christina"] = compute(fu, onb, log, now.date())
        with open(LOG_FILE, "w") as f:
            json.dump(log, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        result["errors"].append(str(e))
        # Keep the last good numbers on screen rather than blanking the dashboard.
        if os.path.exists(OUT_FILE):
            try:
                result["christina"] = json.load(open(OUT_FILE)).get("christina")
            except Exception:
                pass
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
