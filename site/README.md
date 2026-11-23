# IdentityBench leaderboard site (archived v4.1 snapshot)

`identitybench.ai` temporarily serves the **archived IdentityBench v4.1 leaderboard** (`v41_full`, 37 models, as published on 2026-11-13) while IdentityBench v5.2 is calibrated and frozen. When v5.2 publishes, this directory will be replaced by the v5.2 board.

This is a **frozen static snapshot** restored from commit `70180cd`. The original Python build script (`site/build.py`) is not restored, because the repository tracks no Python files. The data in `site/data/` is the published v4.1 output, and the report links point into `archive/`. CI only checks that the snapshot's data files parse and that the entry points exist.

Preview locally from `site/` with `npx wrangler@4 dev`. Deployment is unchanged: Cloudflare Workers Builds deploys `site/` on every push to `main`.
