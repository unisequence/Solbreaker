# Arena physics

Each live simulation has one Bullet DIRECT world. The arena owner creates the
terrain, map chunks, destructible props, vehicle sensors and airborne rigid
bodies in that world. Movement, spotting and contact queries use its body IDs.
A separate arena or a Time Stop branch that can diverge has its own world.

## Movement and contacts

The fixed step runs at 60 Hz. The Rust ground controller fits the tracks to
the terrain, integrates the suspension and sweeps the hull against nearby
bodies. It borrows the arena's Bullet command processor through the plugin
API. Its speculative poses and final pose address the actual vehicle sensor.
Other vehicles see the accepted pose on their next move. Python owns the
drivetrain, the handoff to flight, game events and damage. Bullet advances
all active rigid bodies together, including rams and low-gravity jumps.

The plugin issues one lifetime token per arena. Disconnecting invalidates it
before Bullet releases the command processor. An old controller cannot query
or close a new arena that reused the same numerical client ID. Native step
errors propagate to the arena owner.

The decoded heightmap is a packed grid of unsigned 16-bit samples. Rust takes
one copy at attachment. Immutable map triangles have a local index for ruling
out unnecessary suspension rays. Geometry queries and body lifetimes belong
to Bullet. On the proving ground a destroyed prop is parked and can be
restored by its owner when the area is clear. Both ground movement and
rigid-body contacts respect the PvP zone.

## Build and verify

The server, launcher and benchmark use the same physics profile. Build the
four native libraries for a source checkout, then run the focused tests.

```sh
python tools/build_native.py --jobs 2
python -m unittest tests.test_native_physics tests.test_unified_dynamics tests.test_static_visibility tests.test_proving_ground_rules tests.test_time_stop
```

The release workflow builds the Windows controller from the tagged source.
Setup verifies the native libraries and controller ABI. Developers can select
the historical Python ground path with `SOLBREAKER_NATIVE_PHYSICS=0`.
The separate Rapier crate and `tools/rapier_fixture.py` are retained for
offline comparison of historical fixture files.

Dedicated Linux release bundles retain the Rapier fixture library for the
updater shipped through 0.3.0, which validates its filename before installing
the new updater. Build these release inputs with
`python tools/build_native.py --jobs 2 --with-legacy-fixture`.
Live arenas use the Bullet world described above.
