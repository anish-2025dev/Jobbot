# Building `data/companies.csv`

Run from this folder (network access needed; every call is to a public, keyless job-board API):

```bash
python harvest.py            # boards linked from the live feeds and a year of HN "Who is hiring?" threads
python verify.py             # candidates.py names + known Workday/Oracle boards
python verify.py --harvested # the harvested boards
python build_csv.py          # results.jsonl -> ../../data/companies.csv
```

`verify.py` keeps a board only if it currently has postings, and counts open jobs, tech jobs,
India jobs and entry-level tech jobs (intern, graduate, trainee, junior, SDE I...). Re-running
appends to `results.jsonl` and skips what it already checked; delete that file for a fresh run.
Add employers to `candidates.py` (names, or exact `KNOWN` boards for Workday/Oracle) and re-run.
