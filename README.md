# Solbreaker

![Solbreaker](solbreaker/assets/launcher-cover.png)

A self-hosted multiplayer tank server with a desktop launcher and terminal interface.
**Compatible with build 11.21.0.1235_7376187.**

[Download the alpha](https://github.com/unisequence/Solbreaker/releases/tag/v0.1.0-alpha.1) ·
[Report a bug](https://github.com/unisequence/Solbreaker/issues/new/choose) ·
[Руководство на русском](README.ru.md)

Solbreaker includes accounts, a garage, multiplayer rooms, 7×7 bot battles,
base capture, supremacy and battle results. Run a local server for yourself,
host a LAN game, or connect to another server. Tanks, maps and bot settings
are selected inside the game.

This is the first public alpha. Expect bugs, incomplete mechanics and changes
between versions. Back up server data before updating. The project does not
include the game client or its assets; you need your own compatible copy.

## Start playing

### Windows x64

1. Download `Solbreaker-0.1.0-alpha.1-windows-x64.zip` from the release page.
2. Extract it into a writable folder and open **`launch-gui.vbs`**.
3. Click **Set up game**. Select your compatible game folder and a separate
   destination for the Solbreaker client.
4. Choose **On this computer** to prepare a local server, or **On another
   server** to use a server owner's connection profile. Finish setup and click
   **Play**.

The Windows archive contains a private Python runtime, GUI/TUI dependencies,
PyBullet and all five native libraries. No separate Python, Rust or WSL
installation is needed. `launch-tui.cmd` opens the terminal interface.

If you do not have the pinned game build, the setup wizard can open Steam's
console and provide the depot command. Sign in with your own Steam account,
complete the download and return to the wizard to verify the files.

### Linux x86-64

Install Python 3.11+, its development headers, a C/C++ toolchain and Rust 1.85+.
On Debian/Ubuntu, the system dependencies are:

```sh
sudo apt install python3 python3-venv python3-dev build-essential pkg-config curl
```

Install Rust using [rustup](https://rust-lang.org/tools/install/) if it is not
already available. Extract the source archive, enter its `Solbreaker` folder
and run:

```sh
./launch-gui.sh
```

The launcher installs its Python dependencies and guides you through setup.
For the Linux game client, select your Steam installation and Proton runtime.
Keep the original game folder in place: the separate client links to its assets.

### Server only

The server needs the compatible game resources even when no client is running
on the host. It does not need Steam or a graphical session.

```sh
./setup.sh --game /path/to/owned/game
./start-server.sh --state data --log-file data/server.log
```

On Windows, use `setup.ps1 -Game 'D:\Games\CompatibleBuild'` followed by
`start-server.ps1`. The portable archive supplies the runtime and native libraries.
The initial account is written to `data/private/initial-login.txt`; players can
also register an email, nickname and password inside the game.

For network hosting, configure the addresses, ports and HTTPS profile before
sharing `data/connection.json`. See the [network guide](README.ru.md#сеть-и-https)
and [example configuration](examples/network-public.toml).

## What is included

- Native ground physics in Rust, with Rapier dynamics and Bullet geometry queries.
- A 60 Hz simulation; bot decisions run at 15 Hz.
- GUI and TUI launchers, with Russian and English in the GUI.
- Local and remote server profiles, account registration and server backups.
- Bot navigation, combat maneuvers, several battle rules and post-battle results.
- Optional detailed diagnostic logging for reproducing bugs.

This is not a complete reproduction of every feature of the original service.
Internal damage-module coverage is incomplete. Bot decisions and terrain
interactions still need broader testing. See [native physics](docs/NATIVE_PHYSICS.md)
for implementation details and the limits of the recorded performance results.

## Report a problem

Open an [issue](https://github.com/unisequence/Solbreaker/issues/new/choose) with
your release version, client/server OS, map, tank and reproduction steps.
A short replay or the relevant log excerpt helps. Reports in English or
Russian are welcome. Remove passwords and tokens before attaching anything;
do not upload server databases, backups or the game files.

For a diagnostic server run:

```sh
./start-server.sh --state data --log-file data/server.log --diagnostic-log
```

Security reports go through [private vulnerability reporting](SECURITY.md).
Development and contribution notes are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License and credits

Solbreaker's own code is licensed under **AGPL-3.0-only**. See [LICENSE](LICENSE).
The source and Windows release archives include the project sources and build
scripts. Third-party components retain their own licenses, listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [licenses](licenses/README.md).

This is an independent community project. Game files remain separately obtained
resources. Compatibility research, library references and AI-assisted development
and artwork are documented in [Sources and credits](docs/SOURCES.md).
