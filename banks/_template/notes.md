# <Bank_Name> — notes

A living troubleshooting log for this bank's specific quirks. Update as you learn.

## Page structure

- IR landing URL(s):
- Year-pagination widget: (native `<select>` / custom React dropdown / pagination buttons / none)
- PDF link source: (anchors in initial HTML / loaded via XHR after click / hidden behind login)

## Why the `adapter.py` exists (delete this section if no adapter)

(Document what the generic engine path couldn't handle and what the adapter does
instead. Keep this in sync with `adapter.py` so future-you doesn't have to
reverse-engineer the workaround.)

## Known issues / history

- YYYY-MM-DD: (what changed on the bank's site, what we did about it)

## URLs tried

- ✅ (working URL)
- ❌ (URL that 404'd / returned login wall / changed)
