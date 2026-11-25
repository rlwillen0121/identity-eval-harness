# IdentityBench v5.3 leaderboard site

`identitybench.ai` serves the live **IdentityBench v5.3 (`v53_core`, 48 private scenarios, `k = 1`)** static leaderboard built deterministically from published aggregate artifacts under `reports/v53/` and `datasets/v53/v53_core/SEAL.json`.

## Rebuild & verify

```sh
node scripts/import-v53-run.mjs --leaderboard <leaderboard-dir>
node site/build.mjs
node site/build.mjs --check
node --test "site/tests/*.test.mjs"
```

Preview locally from `site/` with `npx wrangler@4 dev`. Deployment is unchanged: Cloudflare Workers Builds deploys `site/` on every push to `main` (and preview URLs on branches).
