# License inventory

`manifest.json` records the reference inventory collected on 10 November 2026.

- `rust/` retains original license, copyright, author and notice files for all 69 external versions in Rapier's Cargo.lock. Package SHA-256 values identify the locked crates. For profiling, profiling-procmacros and rstar, whose crate archives omit license texts, files were fetched from each package's recorded upstream Git commit. Those source URLs are included per text.
- `python/` retains license and author files for all 24 package versions in requirements/lock.json, including installation and optional build tools. PySide6 and Shiboken texts were obtained from the matching upstream v6.12.0 tag. The full reference scope and package versions are recorded in the manifest.
- `BLITZ.md` points to official game, Steam and viewer terms.

Each copied text has a SHA-256 entry. The packaging regression verifies those files and exact coverage of Rust lockfile packages. Preserve upstream text contents when updating the inventory, including copyright holders and any exceptions.

The Windows wheel separately retains PyBullet's original license and authors. Source installations obtain Python dependencies through the user-facing bootstrap. The self-contained Windows archive ships CPython, Qt and the locked server/TUI dependencies. The release installer selects their exact versions and verifies accepted distribution hashes. `python-runtime/` preserves the 20 notice files shipped in the matching python-build-standalone full archive, including notices for build and cross-platform components. The runtime archive and wheel hashes are recorded in the assembled bundle receipt. Review the inventory again when changing those inputs.

The project owner selected AGPL-3.0-only for Solbreaker's own code on 10 November 2026. The original full text is in the root LICENSE. Dependency notices retain their original terms.

`BUNDLED_LICENSES.txt` combines the complete original texts with component and source-path headings. Python wheels carry this flat bundle and the inventory to keep Windows build paths short. The separate original text files remain available in source archives.
