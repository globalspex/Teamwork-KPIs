#!/usr/bin/env python3
"""
TEMPORARY diagnostic for Teamwork Desk. Writes data/desk-probe.json describing
the SHAPE of Desk's API (statuses, types, sources, inboxes, agents, ticket
field names, pagination meta). Delete this file and desk-probe.yml once the
real Desk sync is built.

Privacy: the repo is public, so ticket text is never written. Subjects,
bodies, previews, and customer names/emails are replaced with "<str len N>".
Only IDs, numbers, booleans, timestamps, and lookup names are kept.
"""
import os, json, re, base64, urllib.request, urllib.error, urllib.parse

SITE = os.environ.get("TEAMWORK_SITE", "globalspex")
KEY = os.environ["TEAMWORK_PASSWORD"]
ROOT = f"https://{SITE}.teamwork.com"
BASIC = "Basic " + base64.b64encode(f"{KEY}:x".encode()).decode()
BEARER = "Bearer " + KEY
# Try each known Desk auth/version combo on a simple endpoint; keep the first that works.
AUTH_TRIALS = [
    ("v2_basic", "/desk/api/v2", BASIC), ("v2_bearer", "/desk/api/v2", BEARER),
    ("v1_basic", "/desk/v1", BASIC), ("v1_bearer", "/desk/v1", BEARER),
]
auth_results = {}
BASE, AUTH = None, None
for name, prefix, hdr in AUTH_TRIALS:
    req = urllib.request.Request(f"{ROOT}{prefix}/ticketstatuses.json", headers={"Authorization": hdr, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            auth_results[name] = r.status
            if BASE is None:
                BASE, AUTH = ROOT + prefix, hdr
    except urllib.error.HTTPError as e:
        auth_results[name] = f"{e.code} {e.read().decode('utf-8','replace')[:150]}"
    except Exception as e:
        auth_results[name] = str(e)[:150]
# Also confirm the key still works for Projects (same check the main sync uses).
try:
    with urllib.request.urlopen(urllib.request.Request(f"{ROOT}/projects/api/v3/me.json", headers={"Authorization": BASIC}), timeout=40) as r:
        me = json.loads(r.read().decode()).get("person", {})
        auth_results["projects_me"] = {"status": r.status, "userType": me.get("userType"), "isAdmin": me.get("administrator")}
except Exception as e:
    auth_results["projects_me"] = str(e)[:150]
if BASE is None:
    os.makedirs("data", exist_ok=True)
    json.dump({"auth_results": auth_results}, open("data/desk-probe.json", "w"), indent=2)
    print("No auth method worked:", auth_results); raise SystemExit(0)
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")

def get(path, params=None):
    url = f"{BASE}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return 0, {"_error": str(e)[:300]}

def redact(v, depth=0):
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v if ISO.match(v) and len(v) < 40 else f"<str len {len(v)}>"
    if isinstance(v, list):
        return {"_list_len": len(v), "first": redact(v[0], depth + 1) if v else None}
    if isinstance(v, dict):
        if depth > 2:
            return {"_keys": list(v.keys())}
        return {k: redact(x, depth + 1) for k, x in v.items()}
    return str(type(v))

def lookup(path, name_keys=("name", "displayName", "title", "code")):
    status, d = get(path, {"pageSize": 200})
    out = {"http": status, "top_keys": list(d.keys())}
    for k, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            out["collection_key"] = k
            out["sample_keys"] = list(v[0].keys())
            out["items"] = [{"id": x.get("id"), **{n: x.get(n) for n in name_keys if n in x},
                             **{f: x.get(f) for f in ("state", "code", "isAgent", "role", "firstName", "lastName", "deleted", "isActive") if f in x}}
                            for x in v]
    if "_error" in d:
        out["error"] = d["_error"]
    return out

result = {"auth_results": auth_results, "base_used": BASE.replace(ROOT, ""), "stage": 2, "list_tests": {}}

KEEP = ("id", "createdAt", "updatedAt", "status", "type", "source", "inboxId", "responseTimes", "numThreads")
def slim(t):
    out = {k: t.get(k) for k in KEEP if k in t}
    a = t.get("assignedTo")
    out["assignedTo"] = a.get("id") if isinstance(a, dict) else a
    return out

tests = {
    "list_p1":            ("tickets.json", [("page", 1)]),
    "list_p2":            ("tickets.json", [("page", 2)]),
    "list_pageSize":      ("tickets.json", [("pageSize", 100)]),
    "list_sort":          ("tickets.json", [("sortBy", "createdAt"), ("sortDir", "desc")]),
    "list_orderBy":       ("tickets.json", [("orderBy", "createdAt"), ("orderMode", "desc")]),
    "list_status_brackets": ("tickets.json", [("statuses[]", "active")]),
    "list_status_ids":    ("tickets.json", [("statusIds[]", 1)]),
    "search_plain":       ("tickets/search.json", []),
    "search_status":      ("tickets/search.json", [("statuses[]", "active")]),
    "search_status_id":   ("tickets/search.json", [("statuses[]", 1)]),
    "search_date":        ("tickets/search.json", [("startDate", "2026-09-28"), ("endDate", "2026-10-03")]),
    "search_lastUpdated": ("tickets/search.json", [("lastUpdated", "2026-09-28T00:00:00Z")]),
    "search_sort":        ("tickets/search.json", [("sortBy", "createdAt"), ("sortDir", "desc")]),
    "search_inbox":       ("tickets/search.json", [("inboxes[]", 699)]),
    "search_type":        ("tickets/search.json", [("types[]", 9139)]),
    "search_combo":       ("tickets/search.json", [("statuses[]", "active"), ("inboxes[]", 699), ("sortBy", "createdAt"), ("sortDir", "desc")]),
}
for name, (path, params) in tests.items():
    url = f"{BASE}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        tk = d.get("tickets") or []
        result["list_tests"][name] = {
            "http": 200, "top_keys": list(d.keys()),
            "count": d.get("count"), "maxPages": d.get("maxPages"), "page": d.get("page"),
            "meta": d.get("meta") if isinstance(d.get("meta"), dict) else None,
            "returned": len(tk), "samples": [slim(t) for t in tk[:4]],
            "status_mix": sorted({t.get("status") for t in tk if isinstance(t.get("status"), str)}),
            "created_range": [min((t["createdAt"] for t in tk), default=None), max((t["createdAt"] for t in tk), default=None)],
        }
    except urllib.error.HTTPError as e:
        result["list_tests"][name] = {"http": e.code, "error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        result["list_tests"][name] = {"http": 0, "error": str(e)[:200]}

os.makedirs("data", exist_ok=True)
json.dump(result, open("data/desk-probe.json", "w"), indent=2)
print("wrote data/desk-probe.json")
