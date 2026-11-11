# Locked release dependencies

`lock.json` pins the Python package versions and SHA-256 values of accepted distributions. The Windows native wheel has its own accepted digest. `native-artifacts.json` pins the four accepted DLLs and that wheel for release assembly.

The GUI, TUI and server setup scripts call `tools/install_dependencies.py`. The installer works inside the project's private virtual environment.

1. Install the locked bootstrap tools, including pip 26.3.
2. Install only the selected profile's pinned packages, with `--require-hashes`. Other dependencies cannot be fetched implicitly.
3. For an upstream PyBullet build, create a separate temporary environment containing only the locked build tools. Verify the upstream input hashes, build a wheel, then verify the wheel's local hash during installation. Runtime NumPy is excluded from that build environment.
4. Install the local project with dependency resolution and build isolation disabled, then run `pip check`.
5. Commit the environment receipt after all steps succeed. A failed or interrupted install can be retried. Concurrent modifications are excluded by the environment's dependency lock.

A matching receipt and installed versions make subsequent starts local checks. When adding a profile, already verified packages can be reused. Download index settings are isolated from user pip environment variables. The selected remote index is PyPI over HTTPS. Native Windows physics uses the packaged wheel directory without an index.

## Profiles and platforms

- `server` installs the runtime and physics dependencies.
- `gui` adds PySide6 Essentials and Shiboken6. On Windows, a present native wheel selects the native server dependencies too. A source-only remote-client GUI does not require PyBullet on Windows.
- `tui` adds Textual and its formatting dependencies.
- `support` adds the optional Cython build tool.

`tools/build_native.py` also tries to compile the contact-query collector using
the C compiler and Python development headers. This small extension uses
CPython's stable ABI from 3.11 and requires no extra runtime package. If the
compiler is unavailable, the server retains its Python query path. To build
only this accelerator, run `python tools/build_contact_queries.py`; Windows
source builds need a Visual Studio developer shell and `libs/python3.lib`.

Linux uses NumPy 1.26.4 on CPython 3.11 and 2.5.4 on CPython 3.12+. Windows uses 1.26.4 on CPython 3.11/3.12. The native Windows bundle requires CPython 3.12 x64. The source Windows server supports 3.11/3.12. Source-only Windows GUI/TUI profiles on newer Python select NumPy 2.5.4. Available wheels and upstream Python-version bounds still apply.

These locks pin package inputs. Native output also depends on the operating system and compiler. Linux source installation retains its C/C++ toolchain and Python-header prerequisites. The self-contained Windows package includes CPython 3.12.16 x64 from Astral python-build-standalone and the GUI, TUI and server wheels. Startup only checks the local bundle receipt and installed versions. A damaged bundle fails with a re-extraction instruction. Source-only and native-DLL-only archives retain their Python prerequisites.

## Maintaining the locks

Update versions deliberately, obtain distribution SHA-256 values from the corresponding PyPI release metadata, review applicable advisories and license changes, and exercise clean installation on supported platforms. Keep the original license notices current. Refresh `release-files.txt` when adding or removing release files.

Direct contributor `pip install` commands follow pyproject.toml's compatibility ranges. The supported release entry points use this lock instead. For an explicit profile installation, use the project environment Python.

```sh
.venv/bin/python tools/install_dependencies.py --profile gui
.venv/bin/python tools/install_dependencies.py --profile gui --check
```

## Building the self-contained Windows package

Place the exact CPython archive pinned in `tools/windows_bundle.py` and one
compatible Windows x64 wheel per locked GUI/TUI/server dependency in a private
input directory. Include the accepted CPython 3.12 PyBullet wheel. Downloads
are a build step. Release startup does not invoke pip or fetch dependencies.

```sh
python3 tools/package_source.py --output /path/to/Solbreaker-windows.zip \
  --windows-native /path/to/accepted-dlls \
  --windows-wheel /path/to/pybullet-3.2.7-cp312-cp312-win_amd64.whl \
  --windows-runtime /path/to/portable-inputs
```

The assembler verifies archive and wheel SHA-256 values, rejects unsafe paths
and duplicate files, installs wheel contents into a private site-packages tree,
and records provenance in `runtime/python/solbreaker-bundle.json`. CPython's
`python312._pth` uses relative paths and excludes registry paths, PYTHONPATH and
user-site packages. Original runtime and wheel license files remain included.
