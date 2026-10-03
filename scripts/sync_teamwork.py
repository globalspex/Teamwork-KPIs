#!/usr/bin/env python3
"""
Pulls core live numbers from Teamwork Projects (and, where noted, Teamwork
Desk) and writes a single JSON snapshot to data/teamwork-live.json.

Scope (by design, kept intentionally small):
  - Company-wide: total open tasks, overdue tasks, due today, completed today
  - Per person: total open tasks, overdue tasks, due today

This does NOT attempt to reproduce the dashboard's more nuanced custom KPIs
(Redo rate, Ticket Resolution Time, Milestones On Time, the spam/automated-
ticket exclusion logic, etc.) -- those still need a human or an assistant
session with the full Teamwork/Desk toolset to compute. This script is
deliberately narrow so it's easy to trust and easy to extend later.

Auth: HTTP Basic Auth using a Teamwork API key (stored in the
TEAMWORK_PASSWORD secret -- despite the name) as the username, with "x" as
the password, per Teamwork's documented API auth convention. This also
works when the account has 2FA enabled, unlike plain login/password auth.
Site is TEAMWORK_SITE (e.g. "globalspex" for https://globalspex.teamwork.com).

Task overdue/due-today status is computed client-side from each task's
dueDate field compared to "today" in TEAMWORK_TIMEZONE, rather than relying
on a server-side date filter -- this avoids depending on exact filter
parameter names that can't be verified without a live test against the API.

Per-person filtering is ALSO done client-side: the whole open-task list is
fetched once, and each task's own assignee data is checked directly,
rather than trusting a server-side `assignedToUserIds`-style query
parameter. (An earlier version of this script tried that server-side
filter and it was silently ignored by the API -- every person came back
with identical, company-wide numbers. Checking each task's own assignee
field directly removes that whole class of bug, at the cost of needing to
know which field(s) Teamwork actually puts assignee data in -- see
`task_assignee_ids()` below, which tries several plausible shapes and
prints a warning if none of them match anything.)
"""

import os
import sys
import json
import datetime
import urllib.request
import urllib.error
import base64
from zoneinfo import ZoneInfo

SITE = os.environ.get("TEAMWORK_SITE", "globalspex")
API_KEY = os.environ["TEAMWORK_PASSWORD"]  # repo secret now holds the Teamwork API key, not a login password
TZ = os.environ.get("TEAMWORK_TIMEZONE", "America/Chicago")
BASE_URL = f"https://{SITE}.teamwork.com"

# Person roster: display name -> Teamwork user ID.
# Update this dict if people join/leave, or if a user ID ever changes.
PEOPLE = {
    "christina": {"name": "Christina Hawkins", "user_id": 116205},
    "javier":    {"name": "Javier López",       "user_id": 273488},
    "jennifer":  {"name": "Jennifer McNinch",    "user_id": 610482},
    "rebecaq":   {"name": "Rebeca Queiroz",      "user_id": 226032},
    "juliane":   {"name": "Juliane Roitmann",    "user_id": 304707},
    "rebecam":   {"name": "Rebeca Mesquita",     "user_id": 605403},
    "aura":      {"name": "Aura Celorico",       "user_id": 296553},
    "andressa":  {"name": "Andressa Alves",      "user_id": 604063},
}


def auth_header():
    # Teamwork's REST API authenticates via Basic Auth using the API key as
    # the username and any non-empty string as the password -- this is
    # their documented convention, and it's required when the account has
    # 2FA enabled, which also blocks plain login/password auth entirely.
    token = base64.b64encode(f"{API_KEY}:x".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def api_get(path, params=None):
    """GET against the Teamwork Projects v3 API, raising with a clear message on failure."""
    url = f"{BASE_URL}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers=auth_header())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Teamwork API error {e.code} on {url}: {body[:500]}") from e


_ASSIGNEE_FIELD_WARNED = False


def task_assignee_ids(task):
    """
    Returns a set of user IDs assigned to this task, trying several known
    Teamwork API response shapes since the exact one can't be confirmed
    without a live test. Logs one warning (not per-task) if nothing
    recognizable is found anywhere, so a silent "everyone shows 0" bug is
    at least visible in the Action's log instead of failing quietly.
    """
    global _ASSIGNEE_FIELD_WARNED
    ids = set()

    # Shape 1: a flat list of IDs directly on the task.
    for key in ("assigneeUserIds", "assignedToUserIds", "responsiblePartyIds"):
        val = task.get(key)
        if isinstance(val, list):
            for v in val:
                try:
                    ids.add(int(v))
                except (TypeError, ValueError):
                    pass

    # Shape 2: a list of assignee objects, each with an "id" field.
    val = task.get("assignees")
    if isinstance(val, list):
        for a in val:
            if isinstance(a, dict) and "id" in a:
                try:
                    ids.add(int(a["id"]))
                except (TypeError, ValueError):
                    pass

    # Shape 3: JSON:API-style relationships block.
    rel = task.get("relationships", {})
    if isinstance(rel, dict):
        for rel_key in ("assignees", "assignedTo"):
            rel_data = rel.get(rel_key, {})
            if isinstance(rel_data, dict):
                items = rel_data.get("data", [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and "id" in item:
                            try:
                                ids.add(int(item["id"]))
                            except (TypeError, ValueError):
                                pass

    if not ids and not _ASSIGNEE_FIELD_WARNED:
        _ASSIGNEE_FIELD_WARNED = True
        sample_keys = sorted(task.keys())
        warning = (
            "Could not find assignee data on any task using the known field "
            f"shapes. Top-level keys on a sample task: {sample_keys}. "
            "Per-person counts are likely all 0 -- share this message so the "
            "field name can be corrected."
        )
        print(f"WARNING: {warning}", file=sys.stderr)
        _RESULT_ERRORS_SINK.append(warning)

    return ids


# Populated by main() with a reference to result["errors"] so
# task_assignee_ids() can surface its one-time warning into the output JSON
# too, not just the Action's log (which is easy to not think to check).
_RESULT_ERRORS_SINK = []


def fetch_all_open_tasks():
    """
    Fetches every incomplete task company-wide, paginating until exhausted.
    Includes assignee relationship data so per-person filtering can be done
    client-side afterward (see task_assignee_ids()).
    """
    tasks = []
    page = 1
    page_size = 200
    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "completed": "false",
            "include": "assignees",
        }
        data = api_get("/projects/api/v3/tasks.json", params)
        batch = data.get("tasks", [])
        tasks.extend(batch)
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 50:  # safety valve against an unexpected infinite loop
            print(f"WARNING: stopped paginating tasks after 50 pages (page={page})", file=sys.stderr)
            break
    return tasks


def classify_tasks(tasks, today_str):
    """Given a list of open tasks, return (total, overdue, due_today) counts."""
    total = len(tasks)
    overdue = 0
    due_today = 0
    for t in tasks:
        due = t.get("dueDate")  # expected format: "YYYY-MM-DD" or None
        if not due:
            continue
        due_date_only = due[:10]
        if due_date_only < today_str:
            overdue += 1
        elif due_date_only == today_str:
            due_today += 1
    return total, overdue, due_today


def fetch_completed_today_count(today_str):
    """Company-wide count of tasks completed today."""
    count = 0
    page = 1
    page_size = 200
    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "completed": "true",
            "completedAfter": today_str,
        }
        data = api_get("/projects/api/v3/tasks.json", params)
        batch = data.get("tasks", [])
        count += len(batch)
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 50:
            break
    return count


def main():
    tz = ZoneInfo(TZ)
    now = datetime.datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    print(f"Syncing Teamwork data for {SITE}.teamwork.com as of {now.isoformat()}")

    result = {
        "generated_at": now.isoformat(),
        "timezone": TZ,
        "company": {},
        "people": {},
        "errors": [],
    }
    global _RESULT_ERRORS_SINK
    _RESULT_ERRORS_SINK = result["errors"]

    # ---- Fetch once, derive both company-wide and per-person from it ----
    try:
        all_open = fetch_all_open_tasks()
        print(f"Fetched {len(all_open)} open tasks company-wide.")

        total, overdue, due_today = classify_tasks(all_open, today_str)
        completed_today = fetch_completed_today_count(today_str)
        result["company"] = {
            "total_open_tasks": total,
            "overdue_tasks": overdue,
            "due_today": due_today,
            "completed_today": completed_today,
        }

        # Index tasks by assignee once, so each person is just a dict lookup
        # rather than a fresh filter pass (and definitely not a fresh API call).
        by_assignee = {}
        for t in all_open:
            for uid in task_assignee_ids(t):
                by_assignee.setdefault(uid, []).append(t)

        for key, info in PEOPLE.items():
            tasks = by_assignee.get(info["user_id"], [])
            p_total, p_overdue, p_due_today = classify_tasks(tasks, today_str)
            result["people"][key] = {
                "name": info["name"],
                "total_open_tasks": p_total,
                "overdue_tasks": p_overdue,
                "due_today": p_due_today,
            }
            print(f"  {info['name']}: total={p_total} overdue={p_overdue} due_today={p_due_today}")

    except Exception as e:
        msg = f"task pull/classification failed: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        result["errors"].append(msg)

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "teamwork-live.json")
    out_path = os.path.abspath(out_path)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {out_path}")

    if result["errors"]:
        print(f"Completed with {len(result['errors'])} error(s) -- see above.", file=sys.stderr)
        # Don't fail the whole workflow on partial errors; the JSON still
        # has whatever succeeded, and the 'errors' list makes gaps visible.


if __name__ == "__main__":
    main()
