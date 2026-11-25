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
