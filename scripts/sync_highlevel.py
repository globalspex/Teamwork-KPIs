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
PROPOSALS_FILE = os.path.join(ROOT, "data", "proposals.json")
# HighLevel's Onboarding pipeline is the source for Converted from this month on.
# Before it, Onboarding wasn't used consistently, so accepted proposals stand in.
CONVERTED_CUTOVER = (2026, 9)


def load_proposals():
    """Better Proposals history (manual list for now).
    Returns (sent_dates, accepted_signed_dates). Sent = accepted + lost + outstanding,
    by date created."""
    try:
        d = json.load(open(PROPOSALS_FILE))
    except Exception:
        return [], []
    f = datetime.date.fromisoformat
    acc = d.get("accepted", [])
    sent = [f(x["created"]) for x in acc] + [f(x) for x in d.get("lost", [])] + [f(x) for x in d.get("outstanding", [])]
    return sent, [f(x["signed"]) for x in acc]


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
    # Proposals = proposals SENT (accepted + lost + outstanding) by date created, per Christina.
    # The HighLevel Proposal-stage log above is still kept, but no longer displayed.
    sent_dates, accepted_dates = load_proposals()
    prop_dates = sent_dates

    leads, props, conv = bucket(lead_dates, weeks), bucket(prop_dates, weeks), bucket(conv_dates, weeks)

    # Monthly buckets: last 12 calendar months, current month last.
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()

    def mbucket(dates):
        counts = {k: 0 for k in months}
        for d in dates:
            if (d.year, d.month) in counts:
                counts[(d.year, d.month)] += 1
        return [counts[k] for k in months]

    m_leads, m_props, m_conv_hl = mbucket(lead_dates), mbucket(prop_dates), mbucket(conv_dates)
    m_accepted = mbucket(accepted_dates)
    m_conv = [hl if ym >= CONVERTED_CUTOVER else acc for ym, hl, acc in zip(months, m_conv_hl, m_accepted)]

    def msummary(series):
        return {"this_month": series[-1], "last_month": series[-2], "last_12_months": sum(series)}

    def summary(series):
        return {"this_week": series[-1], "last_week": series[-2], "last_12_months": sum(series),
                "avg_per_week_last_12_weeks": round(sum(series[-12:]) / 12, 1)}

    return {
        "weeks": [f"{w.month}/{w.day}" for w in weeks],
        "week_start_dates": [w.isoformat() for w in weeks],
        "leads": leads, "proposals": props, "converted": conv,
        "summary": {"leads": summary(leads), "proposals": summary(props), "converted": summary(conv)},
        "months": [datetime.date(y, m, 1).strftime("%b '%y") for y, m in months],
        "monthly": {"leads": m_leads, "proposals": m_props, "converted": m_conv},
        "monthly_summary": {"leads": msummary(m_leads), "proposals": msummary(m_props), "converted": msummary(m_conv)},
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
