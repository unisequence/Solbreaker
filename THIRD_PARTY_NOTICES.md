# Third-party notices

Solbreaker is an independent project for the pinned World of Tanks Blitz 11.21 client. Its server rules, launcher and compatibility adapters are maintained in this repository. The components and external materials below retain their own authorship and terms.

## Distributed native components

The Windows native package includes Solbreaker's four Rust libraries and a locally built PyBullet 3.2.7 wheel for CPython 3.12 x64.

| Component | Use | Terms and notices |
| --- | --- | --- |
| Rapier 3D f64 0.26.1, Dimforge and contributors | Free rigid-body physics | Apache-2.0. Original text in [Rapier LICENSE](licenses/rust/rapier3d-f64-0.26.1/LICENSE). |
| Rapier's Rust dependencies | Geometry, numerics, containers, serialization and solver support | Exact versions, package checksums, authors, license declarations and text checksums are recorded in [the inventory](licenses/manifest.json). Original texts are under `licenses/rust/`. |
| Bullet / PyBullet 3.2.7, Erwin Coumans, Yunfei Bai, Jasmine Hsu and contributors | Geometry queries and the Bullet physics backend | The Windows wheel retains its LICENSE.txt and AUTHORS.txt. Copies from the reference installation are under [PyBullet notices](licenses/python/pybullet-3.2.7/licenses/). The upstream license identifies exceptions for Extras and examples/ThirdPartyLibs. |

The Rust inventory follows all 69 external packages in the checked-in Rapier Cargo.lock. It also includes additional dependencies from the experimental physics_core Cargo.lock. It records the lockfile closure, including build and optional dependencies. Some packages declare alternative licenses. Their original texts and declarations are retained.

## Python interface and server dependencies

The installer obtains Python packages separately. The installer uses pinned versions and distribution hashes from requirements/lock.json. The inventory covers all locked Python versions across platform and profile selections. Notices supplied by each installed package continue to apply.

| Component | Purpose | Declared license in the reference environment |
| --- | --- | --- |
| PySide6 Essentials and Shiboken6, Qt Project and contributors | Graphical launcher | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only, with applicable upstream exceptions and third-party terms |
| Textual, Rich and their dependencies | Terminal interface and formatting | MIT and other per-package declarations in the inventory |
| NumPy | Numeric and geometry operations | BSD-3-Clause and bundled component licenses |
| cryptography and CFFI | Login keys, TLS support and native bindings | Apache-2.0 OR BSD-3-Clause for cryptography, MIT-0 for the recorded CFFI version |
| PyYAML | Structured game resources and configuration | MIT |
| python-lz4 | Packed resource decoding | BSD terms in the recorded package |

Full preserved texts are under `licenses/python/`. The inventory also records the pinned installation and optional build tools, including pip, setuptools, wheel, packaging and Cython. The self-contained Windows package records its exact CPython archive and wheel inputs in `runtime/python/solbreaker-bundle.json`. Original license files inside those wheels are preserved. Runtime notices from the matching python-build-standalone full archive are retained under `licenses/python-runtime/` and in the flat bundle. That upstream notice directory includes build and cross-platform components as well as the Windows runtime.

Qt for Python publishes additional third-party notices and distribution guidance. Those upstream documents describe the terms applicable to the bundled GUI libraries. [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html), [Qt licensing](https://doc.qt.io/qt-6/licensing.html).

## World of Tanks Blitz and Steam

World of Tanks Blitz, its client, game data and trademarks belong to their respective rightsholders. Wargaming identifies the World of Tanks Blitz and Blitz marks in its EULA. Solbreaker uses the pinned Steam build `11.21.0.1235_7376187`, app 444200, depot 444202, supplied separately by the user. The launcher can invoke Steam's depot download workflow for the user's authenticated Steam account.

Game-derived file names, sizes and SHA-256 values are included as an integrity catalog. Resource readers and compatibility patches operate on the user's local game installation. The current launcher background was generated from a text-only description, as documented below.

Applicable official terms are linked in [Blitz and Steam terms](licenses/BLITZ.md). Those agreements govern the corresponding external products. Their inclusion here records attribution and terms, and does not establish permission or approval for Solbreaker from the rightsholders.

## Research, interface adaptation and artwork

The project records client-interface research against the pinned local client. It also contains an experimental internal-geometry loader developed during private Armor Inspector investigations. Imported viewer geometry is external to the source release. WOTInspector's own terms govern its service and content.

The icon uses AI-assisted artwork based on a supplied emblem. On 10 November 2026, the project owner confirmed that this supplied logo was also AI-generated. The launcher cover was generated from a text-only prompt. Prompts, reference descriptions and output hashes are preserved in solbreaker/assets.

Sources, their roles and the limits of the recorded provenance are listed in [Sources and credits](docs/SOURCES.md). Solbreaker's own code is licensed under AGPL-3.0-only, selected by the project owner on 10 November 2026. See the root LICENSE. The third-party components listed here retain their respective licenses.

## Bundled Windows Python

The self-contained Windows distribution uses CPython 3.12.16 x64 from Astral
python-build-standalone release 20261026. The CPython license, Microsoft
Distributable Code restrictions and upstream dependency notices are retained
unchanged. The package also includes the runtime's original `LICENSE.txt`.
The exact archive SHA-256 is pinned by the assembler. [Python Build Standalone](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026).
