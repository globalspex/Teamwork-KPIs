#!/usr/bin/env python3
"""
Pulls core live numbers from Teamwork Projects and writes a single JSON
snapshot to data/teamwork-live.json.

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

Open-task scope (what counts as one of "Javier's 352 open tasks"), each
found and verified against a real CSV export during development:
  - completed=false (obviously)
  - not soft-deleted (deletedAt is set) and not isArchived
  - belongs to a project whose status is "active" (not archived/deleted)
  - belongs to the person via DIRECT assignment (assigneeUserIds) OR via a
    role/team/company they belong to (assigneeJobRoleIds/assigneeTeamIds/
    assigneeCompanyIds on the task, matched against that person's own
    memberships -- see fetch_person_profiles)
  - does NOT have a "Status" custom-field value of Hold, Done, or Waiting
    For Review (see fetch_status_custom_field_id / EXCLUDED_STATUSES).
    Note: this is Teamwork's "status custom field" feature, found via
    included.customfieldTasks on the tasks.json response -- NOT the native
    workflowStages field, which looked plausible but turned out to be
    unused on every real task in this account (always stageId 0).
  - Subtasks (parentTaskId set) are INCLUDED, not excluded -- confirmed via
    the same CSV export that 218 of 352 real tasks were subtasks.

Overdue/due-today status is computed client-side from each task's dueDate
compared to "today" in TEAMWORK_TIMEZONE, rather than a server-side filter.

Per-person task lists are also built client-side (one full fetch, then
matched in Python) rather than via a server-side assignedToUserIds query
param -- Teamwork silently ignored that filter in an earlier version of
this script, returning identical company-wide numbers for every person.
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
API_KEY = os.environ["TEAMWORK_PASSWORD"]  # repo secret holds the Teamwork API key, not a login password
TZ = os.environ.get("TEAMWORK_TIMEZONE", "America/Chicago")
BASE_URL = f"https://{SITE}.teamwork.com"

EXCLUDED_STATUSES = {"hold", "done", "waiting for review"}

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


def fetch_active_project_ids():
    """Returns the set of project IDs whose status is 'active'."""
    active_ids = set()
    status_counts = {}
    page = 1
    page_size = 200
    while True:
        data = api_get("/projects/api/v3/projects.json", {"page": page, "pageSize": page_size})
        batch = data.get("projects", [])
        for p in batch:
            status = p.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "active":
                active_ids.add(int(p["id"]))
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 50:
            break
    print(f"Projects by status: {status_counts}. Active project count: {len(active_ids)}.")
    return active_ids


def fetch_status_custom_field_id():
    """
    Finds the ID of the custom field named 'Status' (the New/In Progress/
    Hold/Done/Stuck/Waiting For Review/Waiting On Client dropdown).
    Returns None if not found, in which case Hold/Done/Waiting-for-Review
    exclusion is skipped entirely rather than silently matching nothing.
    """
    page = 1
    page_size = 200
    while True:
        try:
            data = api_get("/projects/api/v3/customfields.json", {"page": page, "pageSize": page_size})
        except RuntimeError as e:
            print(f"NOTE: customfields.json failed ({e}); Status-based exclusion will be skipped.", file=sys.stderr)
            return None
        fields = data.get("customfields", [])
        for f in fields:
            if (f.get("name") or "").strip().lower() == "status":
                print(f"Found 'Status' custom field: id={f.get('id')}, entity={f.get('entity')}")
                return f.get("id")
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not fields or (total_pages and page >= total_pages) or len(fields) < page_size:
            break
        page += 1
        if page > 10:
            break
    print("NOTE: no custom field named exactly 'Status' was found; Hold/Done/Waiting-for-Review exclusion will be skipped.", file=sys.stderr)
    return None


def task_project_id(task, tasklists_by_id):
    """Resolves a task's project ID via its tasklist (tasks don't carry a projectId directly)."""
    tasklist_id = task.get("tasklistId")
    if tasklist_id is None:
        return None
    tasklist = tasklists_by_id.get(str(tasklist_id))
    if not tasklist:
        return None
    if "projectId" in tasklist:
        return int(tasklist["projectId"])
    project_rel = tasklist.get("project")
    if isinstance(project_rel, dict) and "id" in project_rel:
        return int(project_rel["id"])
    return None


def task_assignee_ids(task):
    """Returns the set of user IDs directly assigned to this task (field: assigneeUserIds)."""
    ids = set()
    val = task.get("assigneeUserIds")
    if isinstance(val, list):
        for v in val:
            try:
                ids.add(int(v))
            except (TypeError, ValueError):
                pass
    return ids


def task_role_team_company_ids(task):
    """Returns (companyIds, jobRoleIds, teamIds) sets found directly on a task."""
    def _ids(key):
        val = task.get(key)
        out = set()
        if isinstance(val, list):
            for v in val:
                try:
                    out.add(int(v["id"] if isinstance(v, dict) else v))
                except (TypeError, ValueError, KeyError):
                    pass
        return out

    return (
        _ids("assigneeCompanyIds"),
        _ids("assigneeJobRoleIds"),
        _ids("assigneeTeamIds"),
    )


def fetch_person_profiles(user_ids):
    """
    Fetches each person's own company/job-role/team membership, so tasks
    assigned to a ROLE, TEAM, or COMPANY (not just an individual user) can
    be correctly attributed to anyone who belongs to that role/team/company.

    Returns {user_id: {"companyIds": set(...), "jobRoleIds": set(...),
    "teamIds": set(...)}}.
    """
    profiles = {uid: {"companyIds": set(), "jobRoleIds": set(), "teamIds": set()} for uid in user_ids}
    page = 1
    page_size = 200
    people_by_id = {}
    use_include = True
    while True:
        params = {"page": page, "pageSize": page_size}
        if use_include:
            params["include"] = "jobRoles,teams"
        try:
            data = api_get("/projects/api/v3/people.json", params)
        except RuntimeError as e:
            if use_include:
                print(f"NOTE: people.json with include=jobRoles,teams failed ({e}); retrying without it.", file=sys.stderr)
                use_include = False
                continue
            raise
        batch = data.get("people", [])
        for p in batch:
            people_by_id[int(p["id"])] = p
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 20:
            break

    for uid in user_ids:
        p = people_by_id.get(uid)
        if not p:
            print(f"WARNING: no people.json record found for user id {uid}", file=sys.stderr)
            continue

        company_id = p.get("companyId")
        if company_id is not None:
            profiles[uid]["companyIds"].add(int(company_id))

        for job_role in p.get("jobRoles", []) or []:
            try:
                profiles[uid]["jobRoleIds"].add(int(job_role["id"] if isinstance(job_role, dict) else job_role))
            except (TypeError, ValueError, KeyError):
                pass

        for team in p.get("teams", []) or []:
            try:
                profiles[uid]["teamIds"].add(int(team["id"] if isinstance(team, dict) else team))
            except (TypeError, ValueError, KeyError):
                pass

        print(
            f"  Profile for user {uid} ({p.get('firstName','?')} {p.get('lastName','?')}): "
            f"companyIds={profiles[uid]['companyIds']}, "
            f"jobRoleIds={profiles[uid]['jobRoleIds']}, "
            f"teamIds={profiles[uid]['teamIds']}"
        )

    return profiles


def fetch_all_open_tasks(active_project_ids, status_field_id):
    """
    Fetches every incomplete task company-wide, paginating until exhausted,
    then excludes soft-deleted/archived tasks, tasks in inactive projects,
    and tasks whose Status custom field is Hold/Done/Waiting For Review.
    See the module docstring for the full, verified scope definition.
    """
    tasks = []
    tasklists_by_id = {}
    status_by_task_id = {}
    page = 1
    page_size = 200
    include_custom_fields = True
    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "completed": "false",
            "include": "assignees,tasklists,customfieldTasks" if include_custom_fields else "assignees,tasklists",
        }
        try:
            data = api_get("/projects/api/v3/tasks.json", params)
        except RuntimeError as e:
            if include_custom_fields:
                print(f"NOTE: tasks.json with include=...,customfieldTasks failed ({e}); retrying without it.", file=sys.stderr)
                include_custom_fields = False
                continue
            raise
        batch = data.get("tasks", [])
        tasks.extend(batch)
        included = data.get("included", {})
        if isinstance(included, dict):
            tasklists_by_id.update(included.get("tasklists", {}) or {})
            cf_tasks = included.get("customfieldTasks", {}) or {}
            for entry in cf_tasks.values():
                if not isinstance(entry, dict):
                    continue
                if status_field_id is not None and entry.get("customfieldId") != status_field_id:
                    continue
                tid = entry.get("taskId")
                if tid is not None:
                    status_by_task_id[tid] = entry.get("value")
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 50:
            print(f"WARNING: stopped paginating tasks after 50 pages (page={page})", file=sys.stderr)
            break

    raw_count = len(tasks)
    deleted_count = sum(1 for t in tasks if t.get("deletedAt"))
    task_archived_count = sum(1 for t in tasks if t.get("isArchived") and not t.get("deletedAt"))
    tasks = [t for t in tasks if not t.get("deletedAt") and not t.get("isArchived")]

    unresolved_project = 0
    inactive_project_count = 0
    kept = []
    for t in tasks:
        pid = task_project_id(t, tasklists_by_id)
        if pid is None:
            unresolved_project += 1
            kept.append(t)  # can't verify -> keep rather than silently drop
            continue
        if pid in active_project_ids:
            kept.append(t)
        else:
            inactive_project_count += 1

    status_counts = {}
    for t in kept:
        s = status_by_task_id.get(t.get("id"))
        status_counts[s] = status_counts.get(s, 0) + 1
    print(f"Status custom field breakdown: {status_counts}")

    def _status_of(t):
        return (status_by_task_id.get(t.get("id")) or "").strip().lower()

    excluded_status_count = sum(1 for t in kept if _status_of(t) in EXCLUDED_STATUSES)
    kept = [t for t in kept if _status_of(t) not in EXCLUDED_STATUSES]

    print(
        f"Raw fetch: {raw_count} tasks. Excluded {deleted_count} soft-deleted, "
        f"{task_archived_count} task-level archived, {inactive_project_count} "
        f"in inactive/archived projects, {excluded_status_count} with Status "
        f"Hold/Done/Waiting For Review. {unresolved_project} tasks had an "
        f"unresolvable project (kept, not excluded). Remaining: {len(kept)}."
    )
    return kept


def milestone_assignee_ids(m):
    """Returns the set of user IDs directly assigned to this milestone."""
    ids = set()
    for key in ("assigneeUserIds", "responsiblePartyIds"):
        val = m.get(key)
        if isinstance(val, list):
            for v in val:
                try:
                    ids.add(int(v))
                except (TypeError, ValueError):
                    pass
    return ids


def milestone_role_team_company_ids(m):
    """Returns (companyIds, jobRoleIds, teamIds) sets found directly on a milestone."""
    def _ids(key):
        val = m.get(key)
        out = set()
        if isinstance(val, list):
            for v in val:
                try:
                    out.add(int(v["id"] if isinstance(v, dict) else v))
                except (TypeError, ValueError, KeyError):
                    pass
        return out

    return (
        _ids("assigneeCompanyIds"),
        _ids("assigneeJobRoleIds"),
        _ids("assigneeTeamIds"),
    )


def fetch_incomplete_milestones():
    """
    Fetches every incomplete milestone company-wide. Verified definition
    (against a real CSV export, exact match for one person: 12 total, 2-3
    late depending on cutoff): a milestone counts for a person if they're
    DIRECTLY assigned OR assigned via a role/team/company they belong to --
    same pattern as tasks. Field names are a best guess by analogy to the
    tasks endpoint (not independently confirmed); if counts come out wrong,
    check result['debug']['sample_milestone'] in the output JSON for the
    actual field names Teamwork returns here.
    """
    milestones = []
    page = 1
    page_size = 200
    while True:
        params = {"page": page, "pageSize": page_size, "completed": "false"}
        data = api_get("/projects/api/v3/milestones.json", params)
        batch = data.get("milestones", [])
        milestones.extend(batch)
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 20:
            break
    print(f"Fetched {len(milestones)} incomplete milestones company-wide.")
    return milestones


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
    """Company-wide count of tasks completed today (excluding soft-deleted)."""
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
        count += sum(1 for t in batch if not t.get("deletedAt"))
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

    try:
        active_project_ids = fetch_active_project_ids()
        status_field_id = fetch_status_custom_field_id()
        all_open = fetch_all_open_tasks(active_project_ids, status_field_id)
        print(f"Fetched {len(all_open)} open tasks company-wide (active projects only).")

        total, overdue, due_today = classify_tasks(all_open, today_str)
        completed_today = fetch_completed_today_count(today_str)
        result["company"] = {
            "total_open_tasks": total,
            "overdue_tasks": overdue,
            "due_today": due_today,
            "completed_today": completed_today,
        }

        print("Resolving each person's role/team/company memberships...")
        profiles = fetch_person_profiles([info["user_id"] for info in PEOPLE.values()])

        by_assignee = {}
        indirect_credit_count = 0
        for t in all_open:
            direct_ids = task_assignee_ids(t)
            t_company_ids, t_role_ids, t_team_ids = task_role_team_company_ids(t)

            for uid in direct_ids:
                by_assignee.setdefault(uid, []).append(t)

            if t_company_ids or t_role_ids or t_team_ids:
                for uid, prof in profiles.items():
                    if uid in direct_ids:
                        continue  # already credited directly, don't double count
                    matched = (
                        (prof["companyIds"] & t_company_ids)
                        or (prof["jobRoleIds"] & t_role_ids)
                        or (prof["teamIds"] & t_team_ids)
                    )
                    if matched:
                        by_assignee.setdefault(uid, []).append(t)
                        indirect_credit_count += 1

        print(f"Credited {indirect_credit_count} task-person links via role/team/company matching (not direct assignment).")

        total_links = sum(len(v) for v in by_assignee.values())
        if all_open and total_links == 0:
            sample_keys = sorted(all_open[0].keys())
            warning = (
                "Fetched tasks but found zero assignee links across the "
                f"entire batch of {len(all_open)} tasks -- the assignee "
                f"field shape is likely wrong. Sample task keys: {sample_keys}"
            )
            print(f"WARNING: {warning}", file=sys.stderr)
            result["errors"].append(warning)

        # ---- Milestones (same direct + role/team/company matching as tasks) ----
        all_milestones = fetch_incomplete_milestones()
        milestones_by_assignee = {}
        for m in all_milestones:
            direct_ids = milestone_assignee_ids(m)
            m_company_ids, m_role_ids, m_team_ids = milestone_role_team_company_ids(m)
            for uid in direct_ids:
                milestones_by_assignee.setdefault(uid, []).append(m)
            if m_company_ids or m_role_ids or m_team_ids:
                for uid, prof in profiles.items():
                    if uid in direct_ids:
                        continue
                    matched = (
                        (prof["companyIds"] & m_company_ids)
                        or (prof["jobRoleIds"] & m_role_ids)
                        or (prof["teamIds"] & m_team_ids)
                    )
                    if matched:
                        milestones_by_assignee.setdefault(uid, []).append(m)

        if all_milestones:
            result["debug"] = {"sample_milestone": all_milestones[0]}

        for key, info in PEOPLE.items():
            tasks = by_assignee.get(info["user_id"], [])
            p_total, p_overdue, p_due_today = classify_tasks(tasks, today_str)

            m_list = milestones_by_assignee.get(info["user_id"], [])
            m_total = len(m_list)
            m_late = sum(1 for m in m_list if (m.get("dueDate") or "")[:10] and (m.get("dueDate") or "")[:10] < today_str)

            result["people"][key] = {
                "name": info["name"],
                "total_open_tasks": p_total,
                "overdue_tasks": p_overdue,
                "due_today": p_due_today,
                "total_incomplete_milestones": m_total,
                "late_milestones": m_late,
            }
            print(f"  {info['name']}: total={p_total} overdue={p_overdue} due_today={p_due_today} milestones={m_total} late_milestones={m_late}")

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


if __name__ == "__main__":
    main()
