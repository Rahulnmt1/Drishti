# Indian banking knowledge-base — daily report (Cowork scheduled task)

This is the source-of-truth prompt for the Cowork scheduled task
`banking-kb-daily-refresh`. The fetch itself runs on Rahul's Mac via a
macOS LaunchAgent (`_engine/_scheduler/com.rahul.banking-kb-daily-refresh.plist`)
about 30 minutes before this report fires. Cowork's only job is to summarize
the latest run log — no network access required, no engine invocation.

When the prompt below changes, also update the Cowork scheduled task via
`mcp__scheduled-tasks__update_scheduled_task` so the two stay in sync. The
prompt is intentionally agnostic to how many banks are configured — adding
a 21st bank doesn't require editing this file.

---

## Prompt

Read the newest engine run log and produce a short status report. Do not run
the engine itself — fetching is handled by a macOS LaunchAgent on the user's
Mac before this task fires.

Steps:

1. List `/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_logs/`
   and pick the newest `run_YYYY-MM-DD_HHMM.json` file.

2. Check freshness. If the log's mtime is more than 18 hours old, the Mac
   LaunchAgent likely didn't fire (Mac asleep, off, or not on AC power for
   `RunAtLoad`-style catch-up). In that case:
     - Report `STALE: last run was <N> hours ago` with the log path.
     - Also tail the newest `wrapper_*.log` in the same folder if one
       exists from today — that's where launchd / shell errors land.
     - Stop. Don't fabricate "no new docs" — this is a missed run, not a
       quiet one.

3. If the log is fresh (mtime within 18h), parse its JSON and report:
     - Total `new_downloads` summed across every entry in `banks[]` — do
       not hardcode a bank count; the config has whatever it has.
     - Per-bank breakdown for any bank with `new_downloads > 0`
       (format: `HDFC_Bank: 1 investor_presentation, SBI: 2 press_releases`).
       Get the doc-type breakdown from the `new_docs` array if present;
       otherwise just give counts.
     - Any errors from the per-bank `errors` arrays — list verbatim so
       Rahul can investigate (don't summarize them away).
     - The full path to the JSON log.

4. If total `new_downloads` is 0 across all banks and there are no errors,
   reply with just `No new docs today` plus the log path. Don't pad.

5. Keep the whole message under 200 words.

Note: This task does not need outbound network access — it only reads local
files. If the folder isn't mounted in this session, request it with
`mcp__cowork__request_cowork_directory(path="/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase")`.
