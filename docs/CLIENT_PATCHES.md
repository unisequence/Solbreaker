# Client executable patches

Solbreaker prepares a separate copy of `wotblitz.exe` and patches it on disk
before launch. The Windows launcher and Linux launcher with Proton use the same
patch chain. The original Blitz installation stays intact.

## Supported build

Solbreaker 0.1.0-rc.2 supports Blitz build `11.21.0.1235_7376187`. Setup checks
the original executable against the SHA-256 below before preparing the client.

| File | Size in bytes | SHA-256 |
| --- | ---: | --- |
| Original `wotblitz.exe` | 73,841,664 | `5030ee4b9f6ec7db1c06efa2b92f9dd7243574be5d8d9578c118119318bc9608` |
| Prepared RC2 `wotblitz.exe` | 73,901,056 | `7222581ad0ae5db6fe240667c8089a065140c64860b7cb3f9ee617f39b3c4770` |

A byte-for-byte comparison found 1,318 changed bytes in the original file
range and a 59,392-byte appended `.mkray` section, including padding. The
section contains the added x86 code and data. The corresponding assembly
and C sources live in [`solbreaker/native`](../solbreaker/native/).

## What changes in the executable

| Change | Implementation |
| --- | --- |
| Local region picker, damage popup timing, gun-marker ray and keyboard/mouse hold-fire | [`client_patch.py`](../solbreaker/client_patch.py) |
| Replay timeline, rewind and playback speed | [`replay_patch.py`](../solbreaker/replay_patch.py) |
| In-battle statistics panel | [`stats_patch.py`](../solbreaker/stats_patch.py) |
| Server visibility updates, enemy markers, minimap, silhouettes, detection light and armor overlays | [`client_recon.py`](../solbreaker/client_recon.py) |
| Battle settings and its server replies | [`client_battle_settings.py`](../solbreaker/client_battle_settings.py) |
| Redirect game documents to the prepared client's private profile on Windows and under Proton | [`windows_profile_patch.py`](../solbreaker/windows_profile_patch.py) |
| Verify TLS certificate chains with the configured CA bundle | [`client_tls.py`](../solbreaker/client_tls.py) |
| Lock a target with missile ammunition selected and preserve the lock when changing ammunition | [`client_atgm_aim.py`](../solbreaker/client_atgm_aim.py) |
| Sandbox input and missile camera, aiming circle, field of view, control and sniper zoom persistence, graphics settings during battle, and battle HUD | [`client_sandbox.py`](../solbreaker/client_sandbox.py) and [`sandbox_client.c`](../solbreaker/native/sandbox_client.c) |

Setup also patches local UI resources, including labels and control bindings,
and writes connection settings for the selected server. The Proving Ground
uses its own map data and UI resources. RC2 builds the map locally from the
bundled layout and the compatible Blitz files. The server handles battle rules
and ability cooldowns. The three default ability keys there are **7**, **8**
and **9**, and can be rebound in Controls.

## Installation and upgrades

[`client_setup.prepare`](../solbreaker/client_setup.py) stages a fresh
client and applies the executable patches. Each stage checks the executable
fingerprint or the PE layout and bytes it depends on. Setup records the
resulting fingerprint in the client's manifest. It writes a replacement
executable to a temporary file before moving it into place.

The Solbreaker updater replaces the launcher and patching code. The next
**Play** from the launcher runs
[`launch_client`](../solbreaker/management_tasks.py), which upgrades a
prepared executable from a recognized previous revision, refreshes local
resources and then starts the game. Your client keeps its existing
`documents/DAVAProject` profile.

A different upstream Blitz build needs its own validation and patch chain.
The GitHub archives contain the patching code and Solbreaker resources. Setup
gets the Blitz executable and donor resources from the compatible game files
you provide.
