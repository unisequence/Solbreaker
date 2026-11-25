# Solbreaker Benchmarks

The GitHub Pages source is `docs/` on the `benchmarks` branch. The comparison
uses Solbreaker's published x86-64 Linux server packages and one fixed test
host. Each public result includes the Git tag, commit SHA and release archive
digest. The detailed HTML can also be downloaded and opened offline.

The first profile runs six scripted players per arena on Mines, with one and
two concurrent arenas, five seconds of warm-up and twenty seconds of
measurement. Reports from a different host, game build or workload need
their own comparison profile.

On the Linux benchmark host, run the collector with its Solbreaker Python
environment and the absolute paths to the pinned game, the current Solbreaker
source checkout and the private native lab helper.

```bash
python tools/publish.py sync \
  --game /absolute/path/to/Blitz \
  --engine-tools /absolute/path/to/Solbreaker \
  --native-lab /absolute/path/to/native_lab.py
```

The collector checks published releases, verifies their Linux archive against
the GitHub asset SHA-256, runs any missing tests, and pushes the derived reports.
GitHub Pages publishes those pushes. The collector requires local access to the
pinned game and GitHub credentials with permission to push `benchmarks`. Only
published packages from this repository run on the test host. Public JSON is
generated from a field allowlist and excludes local paths and worker PIDs.

## Community uploads

The launcher sends an allowlisted summary to the public Worker after an
explicit confirmation. The Worker validates and deduplicates the summary,
limits incoming submissions, and lets the community tab query its SQLite
database. The uploaded table labels the measurements as self-reported.

To enable the production endpoint, authenticate Wrangler as the Cloudflare
operator, create the `solbreaker-benchmarks` D1 database, replace the
placeholder UUID in `community/wrangler.toml`, apply the migration remotely,
and deploy the Worker. Add a discovery document at
`docs/data/community-api.json` containing the HTTPS Worker origin.

```json
{"base_url":"https://solbreaker-benchmarks.example.workers.dev"}
```

The launcher and the Pages tab both read this document. The Worker accepts
POST requests from the launcher and public read-only queries from Pages.
Players do not need a Cloudflare or GitHub account. The app does not upload
raw reports. The Worker uses the incoming IP for throttling and does not
store it alongside results.
