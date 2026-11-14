# Solbreaker Workshop

[Руководство на русском](TANK_WORKSHOP.ru.md)

Workshop is an early alpha SDK for building tanks for Solbreaker. Pick a tank
from your game files, swap its parts, or import your own model. A project saves
as `tank.toml` and exports to a `.tank.zip` that a server and its players can install.

## Open the editor

Extract the [Workshop source archive](https://github.com/unisequence/Solbreaker/releases/tag/workshop-v0.1.0-alpha.1)
into its own directory. Use your own copy of game build `11.21.0.1235_7376187`.
You'll also need Blender on your `PATH` if you want to import GLB models.

On Linux, open a terminal in the extracted `Solbreaker` folder. You'll need Python
3.11+, venv support, Python headers and a C/C++ toolchain.

```sh
python3 -m venv .venv
./.venv/bin/python tools/install_dependencies.py --profile gui
./.venv/bin/python -m solbreaker tank-workshop --language en
```

To open the editor on Windows, use Python 3.12 x64 and PowerShell.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe tools\install_dependencies.py --profile gui
.\.venv\Scripts\python.exe -m solbreaker tank-workshop --language en
```

Pass `--language ru` for Russian or omit the option to use the launcher language.

## Assemble a tank

1. On the first screen, select your game folder and pick a base tank.
2. In **Assembly**, click the hull, turret, gun or running gear slot. Search by tank
   name or nation and pick a donor to preview it. **Undo swap** restores the previous
   assembly. Clear the search to select **From original model**.
3. Drag the handles to move a part. Enter precise coordinates in **Mounts**
   or adjust the running gear in **Tracks**. The **Fit** button adjusts a measured
   turret-ring or mantlet gap in the current pose.
4. In **Stats**, set the name, tier, HP, speed, engine power, reload and primary
   shell damage. Base tank data supplies the remaining equipment and weapon behavior.

## Try it in Proving Ground

On Linux, open `./launch-gui.sh` from the Workshop folder and use it to prepare a
local server and Proton client in dedicated directories. Server setup needs Rust/Cargo
1.85+ and the [Linux build dependencies](INSTALL.ru.md#установка-на-linux).

Close the game and stop the local server. In Workshop, click **Test in game**.
The editor installs the current tank into the configured server and client and
opens Proving Ground. Sign in to Solbreaker if prompted. For launch errors,
check `private/launcher-client.log` in the client folder and `server.log` in the
server folder. The in-game workflow in this alpha targets Linux with Proton. To remove the preview, stop its server and client and remove the dedicated Workshop installation and its test directories. Keep any projects you want to reuse.

## Import a custom model

Use **Import GLB** on the start screen or in **Assets**. The same panel accepts
SC2/SCG pairs for visual and collision meshes and assigns PNG, JPEG or DDS
textures by material name.

For GLB, use parent objects `hull`, `turret_01` and `gun_01` in Blender. Place an
empty named `HP_gunFire_01` at the muzzle. Provide low-poly `collision_hull` and
`collision_turret_01` meshes. `collision_gun_01` is optional. Use Z up and +Y
forward in Blender and export as glTF Binary with Blender's default axis
conversion. Workshop's coordinates are metres with X right, Y up and Z forward.
For custom static tracks, provide both `chassis_track_L` and `chassis_track_R`.
GLB collision meshes use one armor material per part.

## Share a build

Export a `.tank.zip`. With the server and game stopped, use **Install…** to install
the same package on the server and on each player's prepared client. Donor
references are resolved from each installation's local game files. On servers
that grant all vehicles, the new tank appears under the selected nation and tier.
Keep the same package and vehicle IDs for subsequent installs.

The Workshop preview was developed with OpenAI Codex. The donor builder takes
inspiration from [BlitzInfo](https://blitzinfo.app/wg/11.21.0_1/build/).
See the repository [license](../LICENSE) for the Workshop code.

## Feedback

If you hit a snag, tell us your OS, what you clicked and what happened. For fit
or import problems, include the base tank and donor names or the file format.
[Report a bug or suggest a change](https://github.com/unisequence/Solbreaker/issues/new/choose)
