# Changelog

## 0.1.0-alpha.8

- Improve track support when crossing low ledges. Resolve the map's active
  destruction index so supported props break and disappear in clients.
- In the solo sandbox, cycle target tanks, enable return fire for new targets
  and clear the range. A separate binding restores your vehicle to full HP.
- Charge equipment unlock prices by vehicle tier and equipment level.
- Add dedicated Linux and Windows server archives with a TUI and a file list
  for downloading the pinned server resources. Show the version in the TUI.

## 0.1.0-alpha.7 — 2026-11-13

- Choose sunset or fog variants of Mines, Falls Creek and Vineyards in
  training rooms and bot battles. Replays keep the map variant used in battle.
- Set unlimited respawns, a battle duration and unlimited ammunition in bot
  battles as well as training rooms. Change maps in an existing room without
  recreating it.
- With server sandbox enabled, press NumPad 5 for a 0.5-second reload or
  NumPad 6 to teleport to the aim point. The client shows confirmations.
- Keep missile camera mode armed between launches and retain vehicle markers
  in missile view and replay free camera. Adjust motion blur in Graphics.
- Restore crew callouts when you destroy an enemy or your track breaks.
- After publishing a complete release, CI updates the download link in both READMEs.

## 0.1.0-alpha.6 — 2026-11-13

- Set bot counts for both teams in the training room. The room header shows
  the selected mode, battle time and bot counts.
- Choose unlimited lives, set a time limit or turn the timer off. Respawned
  tanks can use consumables again. Players and bots stay on the scoreboard
  while team kills accumulate. Supremacy still displays capture points.
- Keep a training battle open while anyone is still playing, even if an
  entire team leaves.
- Check Git contents and run Linux tests on PRs and pushes to main. Version
  tags build both updater archives and test the Windows bundle before release.

## 0.1.0-alpha.5 — 2026-11-13

- Play Gravity Force in bot battles and training rooms. Eligible tanks get a
  thruster on Ability 1 with a 20-second HUD cooldown. The hull levels out
  after takeoff, and gun recoil can change its trajectory.
- Open 22 hidden maps and variants, including Wasteland, Sea of Tranquility
  and Everfrost. Training rooms can set battle rules and add bots to either team.
- Steer guided missiles by hand or lock on to a tank. The sandbox adds boost,
  flight and a missile camera with rebindable keys. Its solo training ground
  runs until you leave and lets you place physical targets and respawn.
- Restore T49 A textures with the optional original-resource repair. Refill
  ammunition automatically after battles, show provisions on the Tab panel
  and restore missing loadout prices.
- Reuse the prepared client when switching between a local and remote server.
  New saved-login tokens last 30 days.

## 0.1.0-alpha.4 — 2026-11-12

- Keep prepared map geometry and navigation data between battles and server
  restarts. The server prepares maps in the background and gives the
  requested map priority when a battle starts.
- Use **Prepare maps** in the desktop or terminal launcher to start your
  local server and prepare maps before opening the game.
- Build the Rapier and native ground worlds in parallel in the arena
  process. Add startup timings and cache statistics to diagnostic logs.

## 0.1.0-alpha.3 — 2026-11-12

- Allow up to 120 seconds to initialize an arena. The command timeout during
  play remains 45 seconds. This fixes error 8 when a full bot battle takes
  longer than 45 seconds to load.
- Find nearby servers from the setup wizard over IPv4 LAN or Tailscale.
  Hosts can select a network adapter in My server.
- Add a branded Windows desktop shortcut from Settings.
- Show the Steam download size and estimated time before download. Expand
  troubleshooting, LAN setup and update recovery in the guides.

## 0.1.0-alpha.2 — 2026-11-12

- The desktop launcher can download, verify and install new releases from
  GitHub. It restarts after installation and keeps settings and server data.
- On Linux, the launcher can also install a release alongside a Git checkout.
- The dependency check now uses the version declared by the project.

## 0.1.0-alpha.1 — 2026-11-11

First public alpha of Solbreaker, compatible with build 11.21.0.1235_7376187.

- Self-hosted server with accounts, garage, multiplayer rooms and battle results.
- Local 7×7 bot battles, base capture and supremacy.
- GUI/TUI setup and server management; Russian and English GUI.
- Windows portable runtime and Linux source installation.
- Rust ground controller enabled by default, Rapier dynamics and Bullet queries.
- Bot navigation and reload-time maneuvers. Retreats preserve the hull's front
  toward the enemy; close combat restores facing after a route or target change.
- Optional detailed server logs, connection profiles, HTTPS configuration and backups.

### Known limits

This is an experimental release. It does not cover every game mechanic or
original-service feature. Internal damage-module geometry has limited coverage.
Bots and collision behavior may vary between maps and vehicles. Performance
depends on hardware and arena workload; development measurements are not a
guarantee of frame time on another host.

Game files are not included. Windows client preparation requires space for a
separate full client copy. First server preparation can take noticeably longer
than subsequent launches. Some TUI glyphs may render as squares in the legacy
Windows console.
