# IdentityBench v5 Leaderboard Site

The public leaderboard at <https://identitybench.ai>: a zero-dependency static site (plain HTML,
CSS, and ES modules, hand-built SVG charts, no external requests) displaying the live
**IdentityBench v5 (`v5 · core`)** leaderboard from committed `site/data/leaderboard.json` and
`site/data/history.json`.

## Data generation

`site/build.mjs` is a zero-dependency Node ES module that builds `site/data/leaderboard.json` from:

- `archive/reports/v5-python-draft5/rust-final-audit/historical/native/*/summary.json`
- `reports/v5/runs/*/summary.json` (when present; overrides matching `model_id` entries)
- `reports/v5/baselines/*/summary.json` (the 7 deterministic `v5_core` baselines)
- `datasets/v5_draft5/v5_core/SEAL.json` and `site/data/history.json`

Only protocol-complete runs (`is_complete === true` and `observed_slots === planned_slots`, `144/144`)
are included; provider-incomplete runs are strictly excluded.

```sh
node site/build.mjs
node site/build.mjs --check
```

## Preview

From `site/`, using the Cloudflare Workers runtime (which also applies `site/_headers` and `site/.assetsignore`):

```sh
cd site && npx wrangler@4 dev
```

## Updating data or logos

If a model entry is added to `site/data/leaderboard.json` from a vendor that is new to the
leaderboard, add a mark in `site/assets/scripts/logos.js`: a single-colour 24 by 24 path, keyed by
the vendor id used in `leaderboard.json`. The JavaScript tests fail until every listed vendor has a
mark. Existing marks come from [`@lobehub/icons-static-svg`](https://github.com/lobehub/lobe-icons)
(MIT) and [Simple Icons](https://simpleicons.org) (CC0); they are trademarks of their owners and
identify each model's provider only.

## Type

The page uses three open-licence typefaces, self-hosted from `site/assets/fonts/` so the site
still makes no outside requests: Newsreader for headings and figures, Inter for text and tables,
Geist Mono for labels. All are Latin subsets under the SIL Open Font License
(`site/assets/fonts/LICENSE.txt`).

## Checks

```sh
node site/build.mjs --check
node --test "site/tests/*.test.mjs"
```

Both `Site checks` (`.github/workflows/site.yml`) and `CI` (`.github/workflows/ci.yml`) verify
`build.mjs --check` and the JavaScript test suite on pushes and pull requests.

## Deploy

The site is a Cloudflare Worker with static assets only (`site/wrangler.jsonc`). Workers Builds,
connected to this GitHub repository with `site` as its root directory and `site/*` as its build
watch path, deploys on every push to `main` and posts a preview URL on other branches. `site/.assetsignore`
keeps `build.mjs`, `tests/`, and this README out of the deployed upload. `site/_headers` sets the
content security policy and cache lifetimes.
