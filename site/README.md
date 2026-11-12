# IdentityBench leaderboard

The public leaderboard at <https://identitybench.ai>: a zero-dependency static site (plain HTML,
CSS and ES modules, hand-built SVG charts, no external requests) generated from
`reports/v41-full/summary.json`.

## Preview

From the repository root, either a plain file server:

```sh
python -m http.server -d site 8000
```

or the Cloudflare Workers runtime, which also applies `site/_headers` and `site/.assetsignore`:

```sh
cd site && npx wrangler@4 dev
```

## Adding a model

When a model is added or its results change, rebuild both committed data files and commit them
with the report update:

```sh
python site/build.py history   # first-published dates, from the summary's git history
python site/build.py           # site/data/leaderboard.json
```

A model from a vendor that is new to the leaderboard also needs a mark in
`site/assets/js/logos.js`: a single-colour 24 by 24 path, keyed by the vendor id used in
`leaderboard.json`. The JavaScript tests fail until every listed vendor has one. Existing marks
come from [`@lobehub/icons-static-svg`](https://github.com/lobehub/lobe-icons) (MIT) and
[Simple Icons](https://simpleicons.org) (CC0); they are trademarks of their owners and identify
each model's provider only.

## Type

The page uses three open-licence typefaces, self-hosted from `site/assets/fonts/` so the site
still makes no outside requests: Newsreader for headings and figures, Inter for text and tables,
Geist Mono for labels. All are Latin subsets under the SIL Open Font License
(`site/assets/fonts/LICENSE.txt`). The Newsreader file is trimmed to its display optical size and
weights 300 to 600. Chart labels are measured on a canvas, so `main.js` waits briefly for the
fonts before drawing charts.

## Checks

```sh
python site/build.py --check
uv run pytest tests/site -q
node --test "tests/site/js/*.test.mjs"
```

The `Site checks` workflow (`.github/workflows/site.yml`) runs the data drift check and the
JavaScript tests on every push and pull request that touches the site.

## Deploy

The site is a Cloudflare Worker with static assets only (`site/wrangler.jsonc`). Workers Builds,
connected to this GitHub repository with `site` as its root directory and `site/*` as its build
watch path, deploys on every push to `main` and posts a preview URL on other branches. Branch
builds run `npx wrangler preview`, which needs the `previews` block in `wrangler.jsonc` (an empty
block is enough); without it the preview build fails. Building
from `site/` keeps the build from installing the Python harness. The build variable
`PYTHON_VERSION=3.14.8` (production and previews) pins the build image to its preinstalled
Python; otherwise the repository's `.python-version` makes every deploy compile Python 3.12
from source. There is no build step: the data files are
committed, and `site/.assetsignore` keeps `build.py` and this README out of the upload.
`site/_headers` sets the content security policy and cache lifetimes.
