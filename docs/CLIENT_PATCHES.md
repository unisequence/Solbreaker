# Client executable changes

Client preparation creates a separate copy of `wotblitz.exe` and applies
build-specific executable adapters, in addition to changes to UI resources
and connection configuration. The original game executable is not modified.

The first public alpha supports build `11.21.0.1235_7376187` only. Preparation
requires the exact original executable SHA-256:

```text
5030ee4b9f6ec7db1c06efa2b92f9dd7243574be5d8d9578c118119318bc9608
```

## Included changes

All ten groups below are applied by the normal preparation path on both
Windows and Linux/Proton. There are no per-group switches in the setup wizard.

| Group | Original code/data windows | Purpose and implementation |
| --- | ---: | --- |
| Region picker | 1 | Adapt the single-region pre-login branch; `client_patch.py` |
| Damage popup timing | 5 | Change entry/hold animation constants; `client_patch.py` |
| Marker ray | 1 | Scope the marker ray dispatcher to its pinned caller; `client_patch.py` |
| Hold fire | 1 | Extend the keyboard/mouse fire controller; `client_patch.py` |
| Replay controls | 4 | Timeline, rewind and playback speed callbacks; `replay_patch.py` |
| Battle statistics | 1 | Enable the existing statistics panel; `stats_patch.py` |
| Recon bridge | 14 | Server recon messages, enemy markers/minimap opacity, silhouettes, alert lamp and armor overlays; `client_recon.py` |
| Bot battle settings | 3 | Open the settings screen and handle save replies; `client_battle_settings.py` |
| Private profile | 1 | Redirect documents to the prepared client's own profile; `windows_profile_patch.py` |
| TLS verification | 4 | Enable certificate-chain verification and select the CA bundle; `client_tls.py` |

This is **35 original code/data windows**, plus PE header changes and 11
relocation records. Some adapters add native x86 code; the assembly and C
sources are under `solbreaker/native/`. The appended executable/readable
`.mkray` section occupies 10,752 file bytes. It is not writable.

An exact comparison for the current chain changes 1,056 bytes within the
original file and appends 10,752 bytes, including section padding. File size
changes from 73,841,664 to 73,852,416 bytes. The final SHA-256 is:

```text
8f138f55456a52cf5bfb1c19f724f71d369154ee9b0e06e5e9dafb2bd5e87139
```

The window count is a count of named patch sites, not a count of individual
machine instructions or contiguous differing byte runs.

## Application and validation

[`client_setup.prepare`](../solbreaker/client_setup.py) stages a new client
directory. `install_picker` installs the region and timing changes together.
The following stages install marker, hold-fire, replay, statistics, recon and
battle-settings adapters, the private profile redirect, and TLS verification.

Each stage accepts known input fingerprints and checks the instruction windows
or PE layout it depends on. Outputs are pinned by SHA-256. Unknown inputs cause
preparation to fail. Writes use a temporary file, flush/fsync and atomic
replacement. The prepared client is activated only after setup completes.
Upgrade paths recognize already-applied versions and a bounded set of known
predecessors.

These are on-disk patches applied before launch. Ordinary `launch-client` and
`connect-client` do not add executable patches. Server addresses, public keys,
domain names and CA files are configured separately from the executable.

The release distributes patching code and its source, not an original or
patched game executable. Obtain the compatible client separately.
