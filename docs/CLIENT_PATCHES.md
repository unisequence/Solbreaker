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

All twelve groups below are applied by the normal preparation path on both
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
| ATGM target lock | 2 | Enable the native target-lock input with missile ammunition selected and keep the lock on ammo changes; `client_atgm_aim.py` |
| Sandbox controls | 11 | Extend keyboard settings with sandbox bindings, send sandbox input, and render a view from an owned missile; `client_sandbox.py` |

This is **48 original code/data windows**, three calls in the added recon
bridge, plus PE header changes and 11
relocation records. Some adapters add native x86 code; the assembly and C
sources are under `solbreaker/native/`. The appended executable/readable
`.mkray` section occupies 16,384 file bytes.

An exact comparison for the current chain changes 1,091 bytes within the
original file and appends 16,384 bytes, including section padding. File size
changes from 73,841,664 to 73,858,048 bytes. The final SHA-256 is:

```text
30fe28798626af150dd0b81861c9888fdec91e2a22d39fda43ed2816b6040a6a
```

The window count is a count of named patch sites, not a count of individual
machine instructions or contiguous differing byte runs.

## Application and validation

[`client_setup.prepare`](../solbreaker/client_setup.py) stages a new client
directory. `install_picker` installs the region and timing changes together.
The following stages install marker, hold-fire, replay, statistics, recon and
battle-settings adapters, the private profile redirect, TLS verification and
ATGM target lock and the sandbox bridge.

Each stage accepts known input fingerprints and checks the instruction windows
or PE layout it depends on. Outputs are pinned by SHA-256. Unknown inputs cause
preparation to fail. Writes use a temporary file, flush/fsync and atomic
replacement. The prepared client is activated only after setup completes.
Upgrade paths recognize already-applied versions and a bounded set of known
predecessors.

These patches are applied on disk before the game starts. Launching an
existing client through the updated launcher also installs the ATGM
target-lock and sandbox patches on the previous supported executable. Server addresses,
public keys, domain names and CA files are configured separately.

The release distributes patching code and its source, not an original or
patched game executable. Obtain the compatible client separately.
