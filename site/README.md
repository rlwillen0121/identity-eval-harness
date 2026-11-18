# IdentityBench leaderboard

The public leaderboard at <https://identitybench.ai>: a zero-dependency static site (plain HTML,
CSS and ES modules, hand-built SVG charts, no external requests) displaying the archived
`v4.1 · full` leaderboard from committed `site/data/leaderboard.json` and `site/data/history.json`
(derived from `archive/reports/v41-full/summary.json`).

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
(`site/assets/fonts/LICENSE.txt`). The Newsreader file is trimmed to its display optical size and
weights 300 to 600. Chart labels are measured on a canvas, so `main.js` waits briefly for the
fonts before drawing charts.

## Checks

```sh
node --test "site/tests/*.test.mjs"
```

The `Site checks` workflow (`.github/workflows/site.yml`) runs the JavaScript tests on every push
and pull request that touches `site/`.

## Deploy

The site is a Cloudflare Worker with static assets only (`site/wrangler.jsonc`). Workers Builds,
connected to this GitHub repository with `site` as its root directory and `site/*` as its build
watch path, deploys on every push to `main` and posts a preview URL on other branches. Branch
builds run `npx wrangler preview`, which needs the `previews` block in `wrangler.jsonc` (an empty
block is enough); without it the preview build fails. There is no build step: the data files are
committed, and `site/.assetsignore` keeps `tests/` and this README out of the upload.
`site/_headers` sets the content security policy and cache lifetimes.
