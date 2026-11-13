# Contributing

Bug reports and small, reproducible fixes are welcome. English and Russian
are both fine.

Before opening an issue, check whether the same problem has already been
reported. Include the release or commit, server/client OS, map and vehicle,
what happened and the steps that trigger it. A replay or a short relevant log
excerpt is more useful than an entire server directory.

Do not attach account databases, backups, passwords, tokens or proprietary
game files. For a security issue, use [private reporting](SECURITY.md).

## Working on the code

The [installation guide](README.ru.md) covers dependencies and local setup.
Native server components are built with:

```sh
python tools/build_native.py --jobs 2
```

Run the tests related to your change, and describe what you verified in the
pull request. For example:

```sh
python -m unittest tests.test_bot_combat -v
python -m unittest tests.test_native_physics -v
```

Physics and protocol changes also need a check through the real server path;
an isolated helper test alone does not establish gameplay behavior. Document
the map, vehicle and scenario used, and avoid claiming a performance gain from
runs with different workloads.

Keep unrelated changes out of a fix. Add new distributable files to
`release-files.txt`; `tools/package_source.py` rejects unreviewed inputs.
Do not commit local state, binary build outputs, game resources or captures.
Contributions to the project code are under AGPL-3.0-only.

## Cutting a release

GitHub Actions runs the repository audit and Linux tests on pull requests
and pushes to `main`. It checks the allowlist and compares Git blobs with
the pinned client catalog, including files added and removed within the
same push or PR. Keep original game files outside the checkout. To run the
audit before your local Git sends objects to the remote, install the
repository's hook with `git config --local core.hooksPath .github/hooks`.

Update `pyproject.toml` and `solbreaker/__init__.py` together. For example,
`0.1.0a6` maps to `v0.1.0-alpha.6`. Once the release commit is on `main`,
create an annotated tag with the release notes and push that tag. The
workflow builds both updater archives, tests the bundled runtime on Windows,
and publishes the archives and `SHA256SUMS` after the checks pass.

The Windows archive uses the accepted native binaries from the release
pinned in `tools/build_release.py`. If their sources change, build and
validate the Windows binaries, publish the reviewed input archive using the
manual packaging procedure, and update its tag, commit and archive hash in
`tools/build_release.py` along with the accepted binary hashes in
`requirements/native-artifacts.json`.
