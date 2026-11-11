# Experimental native physics

The `experiment/native-physics` branch adds a stateful Rust ground controller.
Enable it with `SOLBREAKER_NATIVE_PHYSICS=1`. It requires the sprung tracked
controller and separate track colliders in the vehicle proxies.

Build the experimental library with the other native libraries.

```sh
python tools/build_native.py --physics-core --jobs 2
SOLBREAKER_NATIVE_PHYSICS=1 ./start-server.sh --state /path/to/server-state
```

On the development host, Rust libraries are built under the `claude-ops` OS
account as required by `AGENTS.md`. The source and tests support the existing
Linux/Windows build selection. Runtime verification of this experimental
backend has been performed on Linux x86-64.

## Execution

Rust owns the heightfield, support layout, suspension state, measured contact
supports, recoil latch and one native Bullet query world. It calls the public
Bullet C API exported by the installed PyBullet library through `libloading`.
The ground solver samples terrain, queries geometry, fits support, subdivides
movement, resolves obstacles and calculates launch state within one native call.
Repeated contact queries at identical poses share a bounded cache within that
call. Destruction invalidates the cache.

The Python adapter sends fixed-layout binary commands and receives states and
contact events. Sensor synchronization sends changed poses and turret angles.
The adapter mirrors measured support and recoil state for the remaining physical
handoff code and diagnostics. Native errors propagate through the arena owner.

The drivetrain, short-flight handling, Rapier activation/landing/recovery and
ground-contact collection still have Python orchestration. This branch is an
intermediate stage of the physics migration. The established speculative drive
worker pool is bypassed while the native owner is active.

## Verification and limits

```sh
python -m unittest tests.test_native_physics -v
```

Tests exercise the normal `CollisionWorld.move` entry point, including walls,
props, external placement, turret updates, rejected commands, shutdown and
low-rock contacts. A regression test replaces the Python drive, support and
contact functions with failures while the native movement loop runs.

Simple ground, wall and prop trajectories agree with the established solver.
Degenerate support systems use a native least-squares fallback. A six-second
synthetic low-rock drive differs by up to 0.30 m between backends. Numerical
contact behavior and the remaining handoff code require further acceptance.

The geometry copy increases resident memory. In the initial 14-connection
capacity comparison, the full isolated test scope peaked at about 1290 MiB,
compared with 875 MiB for the established backend. The final five-minute run
with 14 UDP connections, server CPU affinity 4/6 and generator CPU 12 recorded
zero dropped simulation time. Full arena steps had p99 15.02 ms and maximum
22.97 ms. Scope memory samples peaked at 1291.8 MiB.

A full seven-minute mixed-roster battle was also exercised in the pinned client
under Proton. The final ABI v2 build was checked in a further client battle,
with server metrics confirming the native owner and increasing native step
counts. The graphical checks used the host's ordinary CPU affinity.
