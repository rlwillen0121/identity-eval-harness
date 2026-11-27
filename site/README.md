# IdentityBench v5.3 leaderboard site

`identitybench.ai` serves the **sole current public ranking**, the 96-scenario `v53_full` board (`k = 1`) adopted under the [v5.3.1 publication rules](../docs/v531-scoring-addendum.md#full-suite-publication). The site builds from `reports/v53/confirmatory.json` and `datasets/v53/v53_full/SEAL.json`. The original 48-scenario `v53_core` board is historical; see [`reports/v53/README.md`](../reports/v53/README.md).

## Rebuild & verify

```sh
node scripts/import-v53-run.mjs --confirmatory <native-full-leaderboard-dir> --published-on YYYY-MM-DD
node site/build.mjs
node site/build.mjs --check
node --test "site/tests/*.test.mjs"
```

Preview locally from `site/` with `npx wrangler@4 dev`. Deployment is unchanged: Cloudflare Workers Builds deploys `site/` on every push to `main` (and preview URLs on branches).
