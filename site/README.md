# IdentityBench v5.3 leaderboard site

`identitybench.ai` serves the **96-scenario `v53_full` leaderboard (`k = 1`)**. The site builds from `reports/v53/confirmatory.json` and `datasets/v53/v53_full/SEAL.json`.

## Rebuild & verify

```sh
node scripts/import-v53-run.mjs --confirmatory <native-full-leaderboard-dir> --published-on YYYY-MM-DD
node site/build.mjs
node site/build.mjs --check
node --test "site/tests/*.test.mjs"
```

Preview locally from `site/` with `npx wrangler@4 dev`. Deployment is unchanged: Cloudflare Workers Builds deploys `site/` on every push to `main` (and preview URLs on branches).
