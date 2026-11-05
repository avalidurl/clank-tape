---
name: clank-tape
description: Read and refresh Clank Tape — a static ASCII Clanker trader analytics board on Base. Use when the user asks about Clank Tape, Daily Tape, Fee Reality, Creator PnL Radar, Launch Cohort Board, Clanker launch analytics, or wants to snapshot/update avalidurl.github.io/clank-tape.
---

# Clank Tape

## What it is

**Clank Tape** is a zero-dependency ASCII terminal board for Clanker launches on Base (`chain_id` 8453). Live board: https://avalidurl.github.io/clank-tape/ · Docs: https://avalidurl.github.io/clank-tape/docs/ · Repo: https://github.com/avalidurl/clank-tape

Panels:

1. **Daily Tape** (live) — trending runners + recent deploys with ASCII bars
2. **Fee Reality Card** (live) — normalized buy/sell bps across `v4_lp` / `v4_dyn` / `tax_v0`
3. **Creator PnL Radar** (v0) — launches aggregated by `admin` deployer
4. **Launch Cohort Board** (v0) — UTC hour / 15m deploy histograms

## Stack (do not invent a heavier one)

| Layer | Choice |
| --- | --- |
| Board | Static HTML + CSS (`IBM Plex Mono`), Unicode box-drawing |
| Build | Python 3.12 **stdlib only** — `scripts/snapshot.py` |
| Data | `data/latest.json` committed; board baked into `index.html` |
| Host | GitHub Pages from `main` root (`.nojekyll`) |
| Refresh | GitHub Actions cron every 6h (`workflows/daily-tape.yml`) |

No npm, no framework, no database, no runtime API server.

## Aeon — when to use it

**Clank Tape does not use [Aeon](https://www.aeon.fun/docs) for the board refresh.** Snapshotting is deterministic fetch → render → commit. A plain Actions workflow is enough.

Use Aeon only if you want an **LLM agent** on GitHub Actions that *reasons* over the tape (written briefs, alerts, skill cron with model keys). Do not migrate the snapshot job into Aeon unless the user asks for agentic analysis.

## Refresh the board

```bash
cd clank-tape   # or clone https://github.com/avalidurl/clank-tape.git
python3 scripts/snapshot.py
git add data/latest.json index.html
git commit -m "chore(tape): snapshot"
git push origin main
```

CI (when `.github/workflows/daily-tape.yml` is enabled with a `workflow`-scoped token):

- cron `17 */6 * * *` + `workflow_dispatch`
- runs `python3 scripts/snapshot.py` and commits `data/latest.json` + `index.html`

## Clanker API (public)

Base: `https://www.clanker.world/api`  
User-Agent: `ClankTape/0.1 (+https://avalidurl.github.io/clank-tape/)`

| Endpoint | Notes |
| --- | --- |
| `GET /tokens?sortBy=deployed-at&sort=desc` | Paginate with `cursor=` (~10/page). **Never** pass `limit=` (400). |
| `GET /tokens?sortBy=market-cap&sort=desc` | Works. |
| `GET /tokens/trending` | `{trending, tokens}` — lowercase-match token map keys. |

### Fee normalization

- **`tax_v0`** (`extensions.tax.fees`): `buyTaxRate`/`sellTaxRate` in 1e-4 bps (`1_000_000` = 100 bps = 1%). Split shares in 1e8 (`100_000_000` = 100%).
- **`v4_*`** (`extensions.fees`): `clankerFee`/`pairedFee` (and `baseFee`/`maxFee`) in 1e-2 bps (`10_000` = 100 bps). `recipients[].bps` sum to `10_000`.

Always normalize to standard basis points before showing traders.

## Agent rules

- Prefer reading `data/latest.json` or the live board over re-scraping HTML.
- Do not add frameworks or npm to regenerate the board.
- Do not invent tickers, contract addresses, or affiliation with Clanker.
- Product focus: analytics board. Do not add token-stake marketing unless the user asks.
- Source of truth for API quirks: this skill + `scripts/snapshot.py`.

## Useful URLs

- Board: https://avalidurl.github.io/clank-tape/
- Snapshot JSON: https://avalidurl.github.io/clank-tape/data/latest.json
- Docs: https://avalidurl.github.io/clank-tape/docs/
- This skill: https://avalidurl.github.io/clank-tape/skill/skill.md
- Clanker SDK skill (deploy, not analytics): https://clanker.world/skill/skill.md
