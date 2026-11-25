# Changelog

## 0.3.0-rc.2

- The launcher can switch between stable and preview updates.
- Open **Server benchmark** from the launcher sidebar to choose maps, scenarios,
  and load. The journal shows time remaining and CPU and memory use. You can
  stop a run from the journal.
- Completed runs produce an offline HTML report and a JSON file. The launcher
  opens the HTML report in the browser.
- Use **Submit result** after a run to share a summary in the
  [community benchmark table](https://unisequence.github.io/Solbreaker/#community).
  Submissions require confirmation and no player account. The table can filter
  by version and workload and sort by column.

## 0.3.0-rc.1 — Chat Update

- Find players by nickname and send private messages. The General channel is
  available in the hangar, and each training room has its own chat.
- Send, accept, or decline friend requests in the client. The friends-only
  private message setting persists across sign-ins.
- Battle chat supports team and all-player messages, six voiced quick
  commands, and a ground marker on E.
- Fixed Cyrillic input in chat under Proton/X11.
- Battle results are retried after an interrupted database write, including
  after a server restart.
- LAN and VPN hosts need the chat TCP port open. It defaults to 20018
  (BaseApp port + 1).

## 0.2.1

- Fixed server error 8 when entering battle with the T110E5. The armor loader
  skips faces without an assigned material. [Issue #15](https://github.com/unisequence/Solbreaker/issues/15)
- The server log now includes the original error when an arena worker fails to start.

## 0.2.0 — Core Overdrive

- The server groups input commands between processes by default. Movement,
  aiming and fire commands still run in order, with physics ticks keeping
  their place in the queue.
- Ground movement reuses static collision queries. Rapier returns compact
  contact and pose data to Python, reducing the cost of each physics step.
- Run `python -m solbreaker benchmark` to measure battle tick rate, input
  latency and UDP drops under scripted player or Classic AI load.
- The dedicated Linux archive includes the native contact collector.
- Fixed installation checks for paths that point to the same folder through
  different aliases, including paths used to prepare the Proving Ground.

## 0.1.0

- Classic bots can push together after a spotted opponent fires. They call
  off the push if another threat appears.
- Fixed detection of renamed Radmin VPN and Hamachi adapters on Windows.
- Fixed the sign-in prompt for an incorrect email or password.
- Added English fallback text for Solbreaker UI labels in other game languages.
  Fixed the untranslated Supremacy label on the battle loading screen.
- The launcher explains common setup, update, network and map-loading errors
  and tells players what to check next. The journal keeps the technical details.

## 0.1.0-rc.3

- The launcher can find LAN servers through Radmin VPN and Hamachi.
- Classic bots push forward more often. They look for a clearer firing lane
  when blocked and sometimes break off to finish a spotted low-health tank.
- Fixed tracks dipping below the ground after a rough landing.
- Setup checks the vehicle catalog early and names the missing or unreadable
  file in the launcher.

## 0.1.0-rc.2

- Added the Proving Ground, a 1 km² map with a shooting range, a suspension
  circuit, stunt ramps, a quarry, a runway, a demolition zone and an arena
  for PvP. Open it from its own hangar card or create a training room on it.
  Solbreaker builds the map from its bundled layout and the player's local
  compatible game files.
- The solo Proving Ground has three rebindable ability slots. Use **7** for
  the Gravity thruster, **8** for Mad Games Dash and **9** for High-Octane
  Fuel. Each slot has its own timer, and Dash works during a jump.
- Added a field-of-view slider. Keyboard and mouse bindings and the sniper
  zoom setting persist across sessions. Graphics settings can be opened
  during battle.
- Battle queues and loading screens show the selected mode's name. The
  hangar shows eligible Gravity abilities, and bots take routes and choose
  positions according to their vehicle class.
- Improved low-gravity contacts and recoil, and fixed tracks sinking into
  the ground when landing after sandbox flight. High-Octane Fuel lets tanks
  turn faster and slide farther.
- Camouflage can be set separately for summer, winter and desert. Added a
  replay viewer to the launcher and a link to GitHub in the hangar.
- Fixed battle startup on Windows when the collision cache is stored in a
  folder with Cyrillic characters.

## 0.1.0-rc.1

- Set up bot teams before battle or in a training room. Pick a tank and its
  modules for each slot, set filters for automatic selection, or leave it empty.
- Use the sandbox menu to pick and replace targets on the Polygon and save up
  to four presets for tank stats.
- The original reticle returns with a solid dispersion ring and a timer for
  full aim. Manual sniper zoom persists across restarts.
- Tanks slide along walls, carry momentum through teleports and coast after
  destruction. Rams can register against airborne tanks. A lethal high-speed
  ram can detach the turret, which is restored on respawn.
- High-Octane Fuel adds hull traverse and reduces lateral grip. Dash adds an
  impulse on the ground or in the air and lets the tank keep its momentum.
- The hangar shows tank abilities for Mad Games and Uprising. Uprising restores
  the spawn cloak visuals and clears velocity when an ability turns a tank.
- Steam depot downloads can resume after interruption. The launcher removes
  its temporary depot after a successful installation.

## 0.1.0-alpha.16

- The launcher shows the update download percentage, bytes transferred and
  speed. It displays extraction and verification as a separate stage.
- Download the pinned Blitz build from the setup wizard with the bundled
  DepotDownloader 3.4.0. Sign in with a Steam mobile QR code, follow the
  download percentage and verify the files by SHA-256 when it finishes.
  The Steam console is still available in the wizard.
- Linux updates rebuild server libraries only when their build inputs change.

## 0.1.0-alpha.15

- Choose and apply camouflage on regular tanks in the garage.
- The solid dispersion ring follows the server's aim and smooths changes
  between updates. As the gun settles, shells land closer to the aim point
  more often. Reticle Calibration reduces aim time.
- A track-only hit now shows the shell's blocked damage to the player who
  took the hit. Vehicle HP stays the same.
- The client preserves custom controls when the server disconnects. The
  launcher remembers LAN servers by identity and can find them at a new
  address. The server accepts connections while loading map metadata.
- Preserve team spotting in Time Stop and use the same dispersion model
  for shots fired on the private timeline.

## 0.1.0-alpha.14

- Play Uprising in solo bot battles and training rooms. Tanks get three
  lives. Earn elixir in battle and spend it on upgrades between lives.
- Use each tank's Uprising ability, such as Time Jump, Moonwalk or Hunter's
  Mark. Upgrades include Duplet and Chain Lightning.
- Team spotting and the detection lamp now work across battle modes.
  Shell dispersion accounts for aiming time, movement and gun stats.
- The aiming HUD shows the dynamic dispersion circle again. Reload and
  range share one panel below the crosshair.
- Leave Mad Games or Uprising from the pause menu. Time Stop shows a
  battle notification when active equipment prevents activation. Big Boss
  abilities continue to work after Time Stop and use the reticle's aim point.

## 0.1.0-alpha.13

- Choose Mad Games from the battle-rules menu for a solo battle with bots or
  a training room. Tanks at tiers V–X use the ability pairs from the pinned
  client catalog.
- Use the extra ability slot for Chameleon, Discharge, and other active
  abilities. Passive ability icons sit above the health bar. Tab shows the
  regular provisions.
- Spotting, ability effects, and bot behavior follow the rules of Mad Games.
  On reconnect, the client restores the current abilities and battle state.
- Keep movement going after Time Stop and apply stored damage in stages.
  The reticle groups the reload and distance readouts around the aim point.
  Switching shell type preserves the current reload progress.

## 0.1.0-alpha.11

- Try Time Stop in sandbox battles. Press F8 to enter a private timeline and
  press it again to return early. Movement is free. Shots and supported Big
  Boss abilities spend energy, and their effects apply when you return.
- Other players keep fighting. An animated clock marks your departure point,
  and brief color glitches mark your departure and return.
- Change tanks from the pause menu and rejoin a running training battle.
  The previous hull stays on the map during tank selection.
- Add camouflage selection in the garage and a unified battle reticle.

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
