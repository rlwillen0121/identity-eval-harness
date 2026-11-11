# IdentityBench leaderboard

Preview the site locally from the repository root:

```sh
python -m http.server -d site 8000
```

When a model is added or its results change, derive its history from the full v4.1 report history and rebuild both committed data files:

```sh
python site/build.py history
python site/build.py
```

Commit the updated `site/data/history.json` and `site/data/leaderboard.json` with the source report update. The Pages workflow checks that generated data is current, then deploys the `site/` directory when `main` changes.
