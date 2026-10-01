# Teamwork-KPIs

Daily sync of core Teamwork numbers (company-wide + per-person Total Open
Tasks / Overdue / Due Today) into `data/teamwork-live.json`, so they can be
read without depending on an active Teamwork MCP connection.

## How it works

1. `.github/workflows/sync-teamwork.yml` runs once a day (and can also be
   triggered manually from the **Actions** tab -> **Sync Teamwork KPIs** ->
   **Run workflow**).
2. It runs `scripts/sync_teamwork.py`, which logs into Teamwork with the
   `TEAMWORK_USERNAME` / `TEAMWORK_PASSWORD` repository secrets (already
   added) and pulls open-task data for the company and for each person
   listed in the `PEOPLE` dict near the top of the script.
3. The script writes `data/teamwork-live.json`, and the workflow commits it
   back to the repo if anything changed.

## Before the first real run -- please double check this

I wrote this script against Teamwork's documented v3 API conventions
(`/projects/api/v3/tasks.json`, `assignedToUserIds`, `completed`,
`dueDate`), but I don't have network access to teamwork.com myself, so I
could not test it against your actual account. The most likely things to
need a small fix on the first run:

- **Field names**: if `dueDate` comes back under a different key, or in a
  different date format, the overdue/due-today math will be silently wrong
  (not crash -- just wrong). Check the first `data/teamwork-live.json`
  output against what you see in Teamwork's UI for one or two people.
- **Pagination fields**: `meta.page.pageCount` is my best guess for where
  the total page count lives; if pagination doesn't terminate correctly
  you'll see it hit the 50-page safety cap and print a warning in the
  Action's log.
- **Auth**: Basic Auth with your login username/password should work, but
  if Teamwork rejects it, you may need an API key instead (Teamwork
  Profile -> Edit My Details -> API & Mobile Key), used as the username
  with any value as the password.

If the first run's numbers don't match what you see in Teamwork, share the
Action's log output and I can adjust the script.

## Adding more metrics later

This intentionally only covers the "core live numbers" already on the
dashboard. The more custom KPIs (Redo rate, Ticket Resolution Time,
Milestones On Time, automated-ticket/spam exclusion for ticket counts,
HighLevel lead data, etc.) aren't included here -- they rely on logic
that's harder to hand off to an unattended script safely. Those can be
added incrementally once this baseline is confirmed working.
