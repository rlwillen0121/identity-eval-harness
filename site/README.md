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
npx wrangler@4 dev
```

## Adding a model

When a model is added or its results change, rebuild both committed data files and commit them
with the report update:

```sh
python site/build.py history   # first-published dates, from the summary's git history
python site/build.py           # site/data/leaderboard.json
```

## Checks

```sh
python site/build.py --check
uv run pytest tests/site -q
node --test "tests/site/js/*.test.mjs"
```

The `Site checks` workflow (`.github/workflows/site.yml`) runs the data drift check and the
JavaScript tests on every push and pull request that touches the site.

## Deploy

The site is a Cloudflare Worker with static assets only (`wrangler.jsonc` at the repository root
points at `site/`). Workers Builds, connected to this GitHub repository, deploys on every push to
`main` and posts a preview URL on other branches. There is no build step: the data files are
committed, and `site/.assetsignore` keeps `build.py` and this README out of the upload.
`site/_headers` sets the content security policy and cache lifetimes.
