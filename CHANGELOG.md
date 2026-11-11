# Changelog

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
