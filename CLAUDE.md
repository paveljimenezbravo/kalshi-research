# Notes for Claude Code sessions in this repo

Read `HANDOFF.md` first (status, routine, code map, gotchas), then `RESEARCH_LOG.md`
(every idea tested; the **Candidates** section holds the one live lead, #23).

## Rules

- **Pre-register every test.** Add a row to `RESEARCH_LOG.md` (idea, data, pass rule) and
  commit it *before* running the test. Log the result either way, with the numbers, then
  commit and push. Never change a pass rule after seeing results; a fix found afterwards
  is disclosed in the row.
- Pass bar = the log's Acceptance criteria: > +0.5c per contract after fees with a 95% range
  excluding zero, out of sample, >= 1,000 trades for settlement tests, realistic execution,
  and a written bear case. Resample whole days/events for ranges. ~35 ideas have been tried,
  so also report a strict 99.8% range for anything that passes.
- **No real-money orders without the user's explicit approval for that order.** Paper
  trading and read-only calls are fine. Don't run `autosell.py` against #23 positions.
- Secrets live only in `%LOCALAPPDATA%\kalshi-btc15\secrets`. Never print, commit or move
  them. Data, logs and the paper ledger stay out of git too.
- Use `fetch_data.get_json` for Kalshi reads (retries, and signed requests for the larger
  rate limit). Read live prices from `/markets/{t}/orderbook`, not `/markets/{t}`.
- Run Python with `%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe` and
  `PYTHONDONTWRITEBYTECODE=1`. Long downloads go in the background; they resume.
- The user is a CS student, reads plain language, and wants honest odds, not hype. Keep
  replies short, give progress lines during long work, and say clearly when something is dead.

## Style

Match the existing scripts: a docstring that says what is tested and what counts as a pass,
small functions, pandas/numpy, no new dependencies unless needed (pin them in
`requirements.txt`).
