# Sources and credits

## Game compatibility research

The implementation targets a privately installed, pinned World of Tanks Blitz 11.21 client. Local resource inspection and client-interface research informed resource codecs, entity and message identifiers, UI adapters, vehicle definitions and client patch locations. The source files pin the supported executable hash and validate relevant instruction windows before patching.

The release includes our readers, adapters and an integrity catalog of file names, sizes and hashes. The game executable, textures, maps, sounds and vehicle meshes are obtained separately. Game authorship and applicable terms are recorded in [Blitz and Steam terms](../licenses/BLITZ.md).

The historical TFS ABI and protocol prefixes remain visible in some internal interfaces. Tanks from Scratch and Solbreaker are successive names of this project.

## Libraries and technical references

| Source | Role in Solbreaker |
| --- | --- |
| [Rapier](https://github.com/dimforge/rapier) and [documentation](https://rapier.rs/docs/) | Rigid-body solver used by the experimental and accepted Rapier backend |
| [Bullet / PyBullet](https://github.com/bulletphysics/bullet3) | Geometry queries and Bullet backend |
| [Qt for Python](https://doc.qt.io/qtforpython-6/) | GUI widgets and desktop integration |
| [Textual](https://github.com/Textualize/textual) | Terminal management interface |
| [NumPy](https://numpy.org/doc/) | Numeric arrays and geometry operations |
| [cryptography](https://cryptography.io/en/latest/) | RSA keys and cryptographic primitives |
| [PyYAML](https://pyyaml.org/) and [python-lz4](https://github.com/python-lz4/python-lz4) | YAML resources and LZ4 decoding |
| [Linux IP_PKTINFO documentation](https://man7.org/linux/man-pages/man2/IP_PKTINFO.2const.html) | Datagram address handling, referenced directly in datagram.py |

Exact library versions, authorship metadata and original license texts are collected in [the license inventory](../licenses/manifest.json). These rows identify the components and documented reference used. They do not attribute unverified source-code copying to a particular article or repository.

## Internal-module experiments

Private investigations used Armor Inspector / WOTInspector pages serving Blitz 11.21 viewer data, including data labelled as transferred from Tanks Blitz. The internal_geometry reader preserves explicit source classification, configuration and provenance when loading such external documents. Source URLs, capture timestamps and evidence hashes belong with each imported document. Imported mesh data is excluded from the current source release.

Publication or reuse of viewer content requires a separate assessment of the applicable permissions. [WOTInspector Terms of Service](https://wotinspector.com/en/terms/).

## Artwork and AI assistance

The project icon was generated with OpenAI image_gen using an emblem supplied and selected by the project owner. The prompt, reference description and selected-output hash are recorded in [icon-source.json](../solbreaker/assets/icon-source.json). On 10 November 2026, the project owner confirmed that the supplied logo was also AI-generated. The reference generator was not specified.

The current launcher cover was generated with OpenAI image_gen from a text-only description. It shows the Solbreaker name, dark fractured stone and a narrow lavender light seam. The project owner selected concept 06 on 10 November 2026. No input images were supplied for this generation. The prompt and output SHA-256 are recorded in [launcher-cover-source.json](../solbreaker/assets/launcher-cover-source.json). This replaces the earlier Sheridan screenshot-derived cover.

Development and review used AI coding assistance through Codex and Kimi. The pre-release security review also used Strix with Kimi K3.1 Pro and a separate Terra 6.1 review of two fixes. Those reviews had bounded coverage recorded in private audit evidence. The maintainer reviews and integrates the resulting changes.

## Portable Python

The Windows self-contained package uses CPython 3.12.16 x64 from Astral
python-build-standalone release 20261026. The assembler pins the install-only
archive SHA-256. Original runtime notices were extracted from the matching
PGO full archive, whose SHA-256 is recorded in the license inventory.
The build receipt records every installed wheel and its SHA-256. [Upstream release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026).
