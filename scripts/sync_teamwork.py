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

Task overdue/due-today status is computed client-side from each task's
dueDate field compared to "today" in TEAMWORK_TIMEZONE, rather than relying
on a server-side date filter.

Per-person filtering is ALSO done client-side: the whole open-task list is
fetched once (with include=assignees), and each task's own assignee data
(the confirmed-working field is assigneeUserIds) is checked directly,
rather than trusting a server-side assignedToUserIds-style query parameter
-- an earlier version tried that and Teamwork silently ignored it, so every
person came back with identical, company-wide numbers.
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


def fetch_person_profiles(user_ids):
    """
    Fetches each person's own company/job-role/team membership, so tasks
    assigned to a ROLE, TEAM, or COMPANY (not just an individual user) can
    be correctly attributed to anyone who belongs to that role/team/company
    -- confirmed necessary by a verified CSV export where 25 of one
    person's real tasks were assigned to "Project Management" (a role) or
    "GlobalSpex, Inc." (the company) rather than to him individually.

    Returns {user_id: {"companyIds": set(...), "jobRoleIds": set(...),
    "teamIds": set(...)}}. Field names are guessed defensively (several
    plausible keys checked per concept) since this can't be tested live;
    the diagnostic print shows exactly what was found for each person, so
    a wrong guess here is visible in the log rather than silently wrong.
    """
    profiles = {uid: {"companyIds": set(), "jobRoleIds": set(), "teamIds": set()} for uid in user_ids}
    page = 1
    page_size = 200
    people_by_id = {}
    included_jobroles = {}
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
        included = data.get("included", {})
        if isinstance(included, dict):
            for key in ("jobroles", "jobRoles"):
                if key in included:
                    included_jobroles.update(included[key] or {})
        meta = data.get("meta", {}).get("page", {})
        total_pages = meta.get("pageCount") or meta.get("pages")
        if not batch or (total_pages and page >= total_pages) or len(batch) < page_size:
            break
        page += 1
        if page > 20:
            break

    # Diagnostic: dump Javier's complete raw record once, so if the field
    # guesses below are still wrong, the actual field names are visible in
    # the log instead of needing another guess-and-check round.
    diagnostic_target = people_by_id.get(273488)
    if diagnostic_target:
        print("DIAGNOSTIC -- full raw record for user 273488 (Javier):")
        print(json.dumps(diagnostic_target, indent=2))
        if included_jobroles:
            print("DIAGNOSTIC -- included jobRoles data found:")
            print(json.dumps(included_jobroles, indent=2))
        else:
            print("DIAGNOSTIC -- no included jobRoles data came back at all.")

    for uid in user_ids:
        p = people_by_id.get(uid)
        if not p:
            print(f"WARNING: no people.json record found for user id {uid}", file=sys.stderr)
            continue

        company_id = p.get("companyId")
        if company_id is not None:
            profiles[uid]["companyIds"].add(int(company_id))

        for key in ("jobRoleId", "companyRoleId"):
            val = p.get(key)
            if val:
                try:
                    profiles[uid]["jobRoleIds"].add(int(val))
                except (TypeError, ValueError):
                    pass

        for key in ("teamIds", "teams"):
            val = p.get(key)
            if isinstance(val, list):
                for v in val:
                    try:
                        tid = v["id"] if isinstance(v, dict) else v
                        profiles[uid]["teamIds"].add(int(tid))
                    except (TypeError, ValueError, KeyError):
                        pass

        print(
            f"  Profile for user {uid} ({p.get('firstName','?')} {p.get('lastName','?')}): "
            f"companyIds={profiles[uid]['companyIds']}, "
            f"jobRoleIds={profiles[uid]['jobRoleIds']}, "
            f"teamIds={profiles[uid]['teamIds']}"
        )

    return profiles


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


def task_assignee_ids(task):
    """
    Returns a set of user IDs assigned to this task. The confirmed-working
    field (verified against a real run) is 'assigneeUserIds', a flat list
    of ints directly on the task. A couple of alternate shapes are checked
    too in case Teamwork's response varies by endpoint/version, but a task
    legitimately having zero assignees is normal and NOT a sign of a bug --
    see the whole-batch check in main() for the actual "is this field name
    even right" diagnostic.
    """
    ids = set()

    for key in ("assigneeUserIds", "assignedToUserIds", "responsiblePartyIds"):
        val = task.get(key)
        if isinstance(val, list):
            for v in val:
                try:
                    ids.add(int(v))
                except (TypeError, ValueError):
                    pass

    val = task.get("assignees")
    if isinstance(val, list):
        for a in val:
            if isinstance(a, dict) and "id" in a:
                try:
                    ids.add(int(a["id"]))
                except (TypeError, ValueError):
                    pass

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

    return ids


def fetch_active_project_ids():
    """
    Returns the set of project IDs whose status is 'active' (i.e. not
    archived, not deleted). Used to filter tasks down to active projects
    only, matching what Teamwork's UI shows by default.
    """
    active_ids = set()
    status_counts = {}
    page = 1
    page_size = 200
    while True:
        params = {"page": page, "pageSize": page_size}
        data = api_get("/projects/api/v3/projects.json", params)
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


def task_project_id(task, tasklists_by_id):
    """
    Resolves a task's project ID via its tasklist (tasks don't carry a
    projectId directly; tasklists do). tasklists_by_id is the 'included'
    tasklists data keyed by string ID, the same dict-of-dicts-by-type
    shape this Teamwork account's API has consistently used elsewhere.
    Returns None if it can't be resolved, rather than guessing.
    """
    tasklist_id = task.get("tasklistId")
    if tasklist_id is None:
        return None
    tasklist = tasklists_by_id.get(str(tasklist_id))
    if not tasklist:
        return None
    # Try a couple of plausible shapes for where the project id lives.
    if "projectId" in tasklist:
        return int(tasklist["projectId"])
    project_rel = tasklist.get("project")
    if isinstance(project_rel, dict) and "id" in project_rel:
        return int(project_rel["id"])
    return None


EXCLUDED_WORKFLOW_STAGES = {"hold", "done", "waiting for review"}


def fetch_workflow_stage_names():
    """
    Returns {(workflowId, stageId): "stage name"}. A task's own
    workflowStages field only carries numeric IDs (confirmed via a real
    sample: {"workflowId": 10690, "stageId": 0, ...}, no name anywhere) --
    the readable name lives in a separate workflow-definition endpoint,
    the same kind of lookup job roles needed. Tries a couple of plausible
    endpoints/shapes defensively; returns an empty dict (not a crash) if
    none of them work, in which case Hold/Done/Waiting-for-Review
    exclusion will just not filter anything that run -- visible in the log
    rather than silently wrong.
    """
    lookup = {}
    for path in ("/projects/api/v3/workflows.json", "/projects/api/v2/workflows.json"):
        try:
            data = api_get(path, {"pageSize": 200})
        except RuntimeError as e:
            print(f"NOTE: {path} failed ({e}); trying next option.", file=sys.stderr)
            continue
        workflows = data.get("workflows", [])
        if not workflows:
            continue
        for wf in workflows:
            wf_id = wf.get("id")
            try:
                wf_id = int(wf_id)
            except (TypeError, ValueError):
                pass
            stages = wf.get("stages", [])
            for stage in stages:
                if isinstance(stage, dict) and "id" in stage:
                    stage_id = stage["id"]
                    try:
                        stage_id = int(stage_id)
                    except (TypeError, ValueError):
                        pass
                    lookup[(wf_id, stage_id)] = stage.get("name")
        if lookup:
            print(f"Resolved {len(lookup)} (workflowId, stageId) -> name pairs from {path}.")
            # Targeted check: does the known real task's (10690, 0) key match
            # AT ALL, and if not, is it a type mismatch (str vs int) or is
            # workflow 10690 just missing entirely from what we fetched?
            sample_keys = list(lookup.keys())[:5]
            print(f"DIAGNOSTIC -- sample keys from stage_lookup (showing types): {sample_keys}")
            print(f"DIAGNOSTIC -- direct lookup of (10690, 0): {lookup.get((10690, 0))!r}")
            print(f"DIAGNOSTIC -- direct lookup of ('10690', 0): {lookup.get(('10690', 0))!r}")
            print(f"DIAGNOSTIC -- direct lookup of (10690, '0'): {lookup.get((10690, '0'))!r}")
            matching_workflow = [k for k in lookup if str(k[0]) == "10690"]
            print(f"DIAGNOSTIC -- all keys where workflowId==10690 in any type: {matching_workflow}")
            return lookup
    print("WARNING: could not resolve workflow stage names from any known endpoint. "
          "Hold/Done/Waiting-for-Review exclusion will not filter anything this run.", file=sys.stderr)
    return lookup


def task_workflow_stage_name(task, stage_lookup):
    """Resolves a task's current workflow stage to a readable name via stage_lookup."""
    val = task.get("workflowStages")
    if not isinstance(val, list) or not val:
        return None
    entry = val[0]
    if not isinstance(entry, dict):
        return None
    wf_id, stage_id = entry.get("workflowId"), entry.get("stageId")
    try:
        wf_id = int(wf_id)
    except (TypeError, ValueError):
        pass
    try:
        stage_id = int(stage_id)
    except (TypeError, ValueError):
        pass
    return stage_lookup.get((wf_id, stage_id))


def fetch_all_open_tasks(active_project_ids, stage_lookup):
    """
    Fetches every incomplete task company-wide, paginating until exhausted,
    then excludes: soft-deleted tasks, tasks marked isArchived, and --
    the actual fix for the real discrepancy found this session (API said
    327/44 for a person whose Teamwork UI showed 151/37) -- tasks whose
    PROJECT isn't active. Task-level isArchived/deletedAt turned out NOT
    to explain that gap (numbers were unchanged after adding that filter),
    which is what pointed at project-level archival instead.
    """
    tasks = []
    tasklists_by_id = {}
    page = 1
    page_size = 200
    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "completed": "false",
            "include": "assignees,tasklists",
        }
        data = api_get("/projects/api/v3/tasks.json", params)
        batch = data.get("tasks", [])
        tasks.extend(batch)
        included = data.get("included", {})
        if isinstance(included, dict):
            tasklists_by_id.update(included.get("tasklists", {}) or {})
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

    # Diagnostic: dump the raw workflowStages value (and a couple of other
    # fields for context) from a handful of real tasks, in case the shape
    # task_workflow_stage_name() assumes is wrong -- same kind of guess
    # that needed a raw dump to resolve for job roles earlier.
    print("DIAGNOSTIC -- raw workflowStages field from up to 5 sample tasks:")
    for t in kept[:5]:
        print(json.dumps({"id": t.get("id"), "name": t.get("name"), "workflowStages": t.get("workflowStages")}, indent=2))

    stage_counts = {}
    for t in kept:
        stage = task_workflow_stage_name(t, stage_lookup)
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    print(f"Workflow stage breakdown (before Hold/Done exclusion): {stage_counts}")

    excluded_stage_count = sum(
        1 for t in kept
        if (task_workflow_stage_name(t, stage_lookup) or "").strip().lower() in EXCLUDED_WORKFLOW_STAGES
    )
    kept = [
        t for t in kept
        if (task_workflow_stage_name(t, stage_lookup) or "").strip().lower() not in EXCLUDED_WORKFLOW_STAGES
    ]

    # NOTE: subtasks are intentionally NOT excluded. An earlier version of
    # this script excluded them based on a since-debunked hypothesis (a
    # person's "open tasks" UI view appeared much smaller than the API
    # total, which looked like a subtask-counting difference). A verified
    # CSV export later confirmed the real total DOES include subtasks
    # (218 of 352 real, confirmed-open tasks for that person were
    # subtasks) -- the original UI number was just wrong (truncated by
    # pagination, never actually fully loaded). Leaving this note so the
    # subtask question doesn't get re-litigated without re-reading this.

    print(
        f"Raw fetch: {raw_count} tasks. Excluded {deleted_count} soft-deleted, "
        f"{task_archived_count} task-level archived, {inactive_project_count} "
        f"in inactive/archived projects, {excluded_stage_count} with workflow "
        f"stage Hold/Done. {unresolved_project} tasks had an unresolvable "
        f"project (kept, not excluded). Remaining: {len(kept)}."
    )
    return kept


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
        stage_lookup = fetch_workflow_stage_names()
        all_open = fetch_all_open_tasks(active_project_ids, stage_lookup)
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

            # Credit this task to anyone whose own company/role/team matches
            # the task's, even if they're not individually named -- this is
            # what closed the verified 25-task gap for the person we tested
            # against (role- and company-assigned tasks).
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

        # Whole-batch sanity check: if there ARE open tasks but literally none
        # of them produced any assignee link at all, that's the real signal
        # the field shape is wrong -- as opposed to any single task simply
        # being unassigned, which is normal and not worth flagging.
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


if __name__ == "__main__":
    main()
