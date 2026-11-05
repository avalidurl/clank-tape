# Clank Tape

**Clank Tape** is a dense, zero-dependency ASCII terminal analytics board for Clanker token launches on Base (`chain_id: 8453`).

Live board: **https://avalidurl.github.io/clank-tape/**

## Modules on the Board

1. **Daily Tape (`live`)** — Continuous top marquee + 24h trending runners (volume & log10 market-cap ASCII bars) + latest cursor-paginated Base deploys.
2. **Fee Reality Card (`live first cut`)** — Normalized bps breakdown across Clanker contract regimes (`v4_lp`, `v4_dyn`, and `tax_v0`), surfacing buy/sell bps, creator vs burn vs integrator splits, protocol skim, and anti-snipe decay windows.
3. **Creator PnL Radar (`v0 stub`)** — Aggregates recent launches by `admin` / deployer wallet to surface repeat launchers, fee regime preferences, and combined market cap / 24h volume (v1 adds realized Base fee-claim logs).
4. **Launch Cohort Board (`v0 stub`)** — UTC hourly and 15-minute deploy histograms with `v4_lp` vs `tax_v0` split and active-volume survival counts.
5. **Analytics Roadmap** — Current live vs stub vs next milestones.

## Architecture & Scheduled Refresh

- `scripts/snapshot.py` — Pure Python 3 stdlib script (`urllib`, `json`, `html`) that queries public `clanker.world/api` endpoints (cursor-paginated, no external dependencies), writes `data/latest.json`, and bakes static `index.html`.
- `style.css` — Dark terminal CRT / ticker-tape stylesheet (`IBM Plex Mono`, green/amber on near-black).
- `workflows/daily-tape.yml` — GitHub Actions workflow (runs every 6 hours UTC + `workflow_dispatch`) ready to move into `.github/workflows/daily-tape.yml` once pushed with a `workflow`-scoped GitHub token (`gh auth refresh -s workflow`).

*Independent analytics board. Data sourced from public `clanker.world/api` endpoints; not affiliated with Clanker.*
