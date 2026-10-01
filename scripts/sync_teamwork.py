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

Auth: HTTP Basic Auth using TEAMWORK_USERNAME / TEAMWORK_PASSWORD (the same
login credentials used at teamwork.com), against TEAMWORK_SITE
(e.g. "globalspex" for https://globalspex.teamwork.com).

Task overdue/due-today status is computed client-side from each task's
dueDate field compared to "today" in TEAMWORK_TIMEZONE, rather than relying
on a server-side date filter -- this avoids depending on exact filter
parameter names that can't be verified without a live test against the API.
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
USERNAME = os.environ["TEAMWORK_USERNAME"]
PASSWORD = os.environ["TEAMWORK_PASSWORD"]
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
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode("utf-8")).decode("ascii")
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


def fetch_all_open_tasks(assigned_to_user_id=None):
    """
    Fetches every incomplete task (optionally filtered to one assignee),
    paginating until exhausted. Returns a list of task dicts with at least
    an 'id' and 'dueDate' field.
    """
    tasks = []
    page = 1
    page_size = 200
    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "completed": "false",
        }
        if assigned_to_user_id is not None:
            params["assignedToUserIds"] = assigned_to_user_id
        data = api_get("/projects/api/v3/tasks.json", params)
        batch = data.get("tasks", [])
        tasks.extend(batch)
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 50:  # safety valve against an unexpected infinite loop
            print(f"WARNING: stopped paginating tasks after 50 pages (assignee={assigned_to_user_id})", file=sys.stderr)
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

    # ---- Company-wide ----
    try:
        all_open = fetch_all_open_tasks(assigned_to_user_id=None)
        total, overdue, due_today = classify_tasks(all_open, today_str)
        completed_today = fetch_completed_today_count(today_str)
        result["company"] = {
            "total_open_tasks": total,
            "overdue_tasks": overdue,
            "due_today": due_today,
            "completed_today": completed_today,
        }
    except Exception as e:
        msg = f"company-wide pull failed: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        result["errors"].append(msg)

    # ---- Per person ----
    for key, info in PEOPLE.items():
        try:
            tasks = fetch_all_open_tasks(assigned_to_user_id=info["user_id"])
            total, overdue, due_today = classify_tasks(tasks, today_str)
            result["people"][key] = {
                "name": info["name"],
                "total_open_tasks": total,
                "overdue_tasks": overdue,
                "due_today": due_today,
            }
            print(f"  {info['name']}: total={total} overdue={overdue} due_today={due_today}")
        except Exception as e:
            msg = f"{info['name']} pull failed: {e}"
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
