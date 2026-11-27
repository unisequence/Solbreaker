//! Stateful native physics and geometry for the experimental server branch.
mod bullet_api;
mod ground;
mod plugin;
mod static_triangles;
mod support;

use bullet_api::{Client, Contact};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{c_char, c_void, CStr};
use std::panic::{catch_unwind, AssertUnwindSafe};
use tfs_sim_core::{DriveInput, Motion, Pose, Terrain};

use std::sync::{Arc, RwLockReadGuard};
thread_local! { static CREATE_ERROR: RefCell<String> = const { RefCell::new(String::new()) }; }

#[repr(C)]
#[derive(Clone, Copy)]
pub struct StepInput {
    abi_version: u32,
    entity: u32,
    pose: Pose,
    road: Pose,
    motion: Motion,
    lean: [f64; 4],
    effort: [f64; 3],
    distance: f64,
    yaw_delta: f64,
    dt: f64,
    displacement: [f64; 2],
    has_displacement: u32,
    was_blocked: u32,
    constants: [f64; 8],
    support_count: u32,
    has_latch: u32,
    carrying_supported: u32,
    supports: [CachedSupport; 16],
    latch: [f64; 4],
    turret_yaw: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CachedSupport {
    body: i32,
    x: f64,
    z: f64,
    height: f64,
}

#[repr(C)]
#[derive(Default)]
pub struct StepOutput {
    pose: Pose,
    road: Pose,
    motion: Motion,
    lean: [f64; 4],
    rest: f64,
    spin: [f64; 3],
    recoil: [f64; 7],
    moved: u32,
    grounded: u32,
    blocked: u32,
    carrying_supported: u32,
    contact: i32,
    has_recoil: u32,
    event_count: u32,
    support_count: u32,
    supports: [CachedSupport; 16],
    has_latch: u32,
    latch: [f64; 4],
}

#[no_mangle]
pub extern "C" fn solbreaker_physics_abi_sizes() -> u64 {
    ((std::mem::size_of::<StepInput>() as u64) << 32) | std::mem::size_of::<StepOutput>() as u64
}

impl StepInput {
    fn drive(self) -> Result<DriveInput, String> {
        let poses = [self.pose, self.road];
        let finite = poses.iter().all(|p| {
            [p.x, p.y, p.z, p.yaw, p.pitch, p.roll]
                .iter()
                .all(|v| v.is_finite())
        }) && self
            .motion
            .velocity
            .iter()
            .chain(&self.motion.suspension)
            .chain(&self.motion.angular)
            .chain(&self.lean)
            .chain(&self.effort)
            .chain(&self.displacement)
            .chain(&self.constants)
            .all(|v| v.is_finite());
        if self.abi_version != 3
            || !self.turret_yaw.is_finite()
            || !finite
            || self.constants[..7].iter().any(|v| *v <= 0.)
            || self.constants[7] < 0.
            || self.has_displacement > 1
            || self.was_blocked > 1
            || self.support_count > self.supports.len() as u32
            || self.has_latch > 1
            || self.carrying_supported > 1
            || self
                .supports
                .iter()
                .take(self.support_count as usize)
                .any(|row| {
                    ![row.x, row.z, row.height]
                        .iter()
                        .all(|value| value.is_finite())
                })
            || (self.has_latch != 0 && !self.latch.iter().all(|value| value.is_finite()))
            || ![self.distance, self.yaw_delta, self.dt]
                .iter()
                .all(|v| v.is_finite())
            || self.dt <= 0.
            || self.dt > 1.
        {
            return Err("Invalid native physics input".into());
        }
        let travel = if self.has_displacement != 0 {
            self.displacement[0].hypot(self.displacement[1])
        } else {
            self.distance.abs()
        };
        let slices = (travel / self.constants[1])
            .ceil()
            .max((self.yaw_delta.abs() / 0.75_f64.to_radians()).ceil())
            .max(1.);
        if !slices.is_finite() || slices > 512. {
            return Err("Invalid native movement subdivision".into());
        }
        Ok(DriveInput {
            abi_version: tfs_sim_core::ABI_VERSION,
            count: 0,
            pose: self.pose,
            road: self.road,
            motion: self.motion,
            lean: self.lean,
            effort: self.effort,
            distance: self.distance,
            yaw_delta: self.yaw_delta,
            dt: self.dt,
            displacement: self.displacement,
            has_displacement: self.has_displacement,
            was_blocked: self.was_blocked,
            has_memory: 0,
            hint_count: 0,
            hints: [0; 3],
            a: std::ptr::null(),
            b: std::ptr::null(),
            p: std::ptr::null(),
            weight: std::ptr::null(),
            memory_heights: std::ptr::null(),
            memory_speeds: std::ptr::null(),
            constants: self.constants,
            terrain: Terrain {
                heights: std::ptr::null(),
                size: 0,
                min_x: 0.,
                min_z: 0.,
                width: 1.,
                depth: 1.,
                height_min: 0.,
                height_range: 1.,
            },
        })
    }
}

type StaticPairKey = ([u64; 6], u64, u64, i32);

#[derive(Default)]
struct StaticPairCache {
    contacts: BTreeMap<StaticPairKey, Vec<Contact>>,
    rows: usize,
}

const STATIC_PAIR_ENTRIES: usize = 512;
const STATIC_PAIR_ROWS: usize = 4096;

// During one synchronous drive only the probed hull changes transform. If
// its expanded box contains no obstacles, nearby probes need no new SDK call.
#[derive(Default)]
enum ContactClearance {
    #[default]
    Unknown,
    Occupied,
    Empty { pose: [f64; 6], radius: f64 },
}

impl ContactClearance {
    const PADDING: f64 = 0.75;

    fn covers(&self, probe: &[f64; 6]) -> bool {
        let Self::Empty { pose, radius } = self else { return false };
        let displacement = (0..3).map(|i| (pose[i] - probe[i]).abs()).fold(0., f64::max);
        // Arc length bounds the displacement under each Euler rotation.
        let angle: f64 = (3..6).map(|i| (pose[i] - probe[i]).abs()).sum();
        displacement + radius * angle < Self::PADDING
    }
}

struct Body {
    links: i32,
    turret_bits: std::cell::Cell<u64>,
    static_contacts: std::cell::RefCell<StaticPairCache>,
}

struct World {
    client: Client,
    bodies: BTreeMap<i32, Body>,
    query_bodies: BTreeSet<i32>,
    static_mask: i32,
    disabled_during_step: Vec<i32>,
    entities: BTreeMap<u32, i32>,
    map_bodies: BTreeSet<i32>,
    solid_map_bodies: BTreeSet<i32>,
    collision_zone: Option<[f64; 3]>,
    inside_zone: BTreeSet<i32>,
    static_triangles: Option<static_triangles::Tree>,
    last_probe: std::cell::Cell<Option<(i32, [f64; 3], [f64; 4])>>,
    terrain_bodies: BTreeSet<i32>,
    breakables: BTreeMap<String, Vec<i32>>,
    parked_props: BTreeMap<String, Vec<i32>>,
    error: String,
    poisoned: bool,
    heightfield: ground::Heightfield,
    ground: BTreeMap<u32, ground::State>,
    contact_events: Vec<(u32, tfs_sim_core::Pose, Vec<Contact>)>,
    destroyed_events: Vec<(String, f64, f64)>,
}

struct WorldHandle {
    session: Arc<plugin::Session>,
    world: World,
}

impl WorldHandle {
    fn access(&mut self) -> Result<(&mut World, RwLockReadGuard<'_, bool>), ()> {
        match self.session.guard() {
            Ok(guard) => Ok((&mut self.world, guard)),
            Err(reason) => {
                self.world.error = reason.into();
                Err(())
            }
        }
    }
}

fn integer(value: &Value, field: &str) -> Result<i32, String> {
    value[field].as_i64().and_then(|v| i32::try_from(v).ok())
        .filter(|v| *v >= 0).ok_or_else(|| format!("Invalid {field}"))
}

fn vector<const N: usize>(value: &Value) -> Result<[f64; N], String> {
    let array = value.as_array().ok_or("Expected a numeric array")?;
    if array.len() != N {
        return Err("Invalid vector size".into());
    }
    let mut result = [0.; N];
    for (destination, source) in result.iter_mut().zip(array) {
        *destination = source.as_f64().ok_or("Expected a number")?;
        if !destination.is_finite() {
            return Err("Nonfinite vector".into());
        }
    }
    Ok(result)
}

impl World {
    fn new(fixture: &Value, library: &str, handle: bullet_api::Handle, heights: &[u16]) -> Result<Self, String> {
        let mut world = Self {
            client: Client::borrow(library, handle, integer(fixture, "static_group")?)?,
            bodies: BTreeMap::new(),
            query_bodies: BTreeSet::new(),
            static_mask: integer(fixture, "static_mask")?,
            disabled_during_step: Vec::new(),
            entities: BTreeMap::new(),
            map_bodies: BTreeSet::new(),
            solid_map_bodies: BTreeSet::new(),
            collision_zone: fixture.get("zone").map(vector::<3>).transpose()?,
            inside_zone: BTreeSet::new(),
            static_triangles: None,
            last_probe: std::cell::Cell::new(None),
            terrain_bodies: BTreeSet::new(),
            breakables: BTreeMap::new(),
            parked_props: BTreeMap::new(),
            error: String::new(),
            poisoned: false,
            heightfield: ground::Heightfield::new(&fixture["terrain"], heights)?,
            ground: BTreeMap::new(),
            contact_events: Vec::new(),
            destroyed_events: Vec::new(),
        };
        let mut static_paths = Some(Vec::new());
        for entry in fixture["static"]
            .as_array()
            .ok_or("Missing static geometry")?
        {
            let original = integer(entry, "body")?;
            if !world.query_bodies.insert(original) {
                return Err("Duplicate static body".into());
            }
            let kind = entry["kind"].as_str().ok_or("Missing geometry kind")?;
            if kind != "terrain" && kind != "breakable" {
                match (kind, entry.get("center"), entry["path"].as_str()) {
                    ("map", None, Some(path)) => {
                        if let Some(paths) = &mut static_paths {
                            paths.push(std::path::Path::new(path));
                        }
                    }
                    _ => static_paths = None,
                }
            }
            if kind == "terrain" {
                world.terrain_bodies.insert(original);
            } else {
                world.map_bodies.insert(original);
            }
            if kind == "breakable" {
                let key = entry["key"].to_string();
                world.breakables.entry(key).or_default().push(original);
            }
        }
        world.static_triangles = static_paths.and_then(|paths| static_triangles::Tree::from_paths(paths.into_iter()));
        world.solid_map_bodies = world.map_bodies.clone();
        for body in world.breakables.values().flatten() {
            world.solid_map_bodies.remove(body);
        }
        for entry in fixture["entities"]
            .as_array()
            .ok_or("Missing vehicle geometry")?
        {
            world.install_vehicle(entry)?;
        }
        Ok(world)
    }

    fn install_vehicle(&mut self, entry: &Value) -> Result<(), String> {
        let entity = entry["id"].as_u64().ok_or("Missing entity id")? as u32;
        let original = integer(entry, "body")?;
        let ground = entry.get("ground").map(ground::Configuration::new).transpose()?;
        let body = Body {
            links: integer(entry, "links")?,
            turret_bits: std::cell::Cell::new(0),
            static_contacts: RefCell::new(StaticPairCache::default()),
        };
        if self.query_bodies.contains(&original) || self.bodies.contains_key(&original) {
            return Err("Body already registered in arena".into());
        }
        if let Some(old) = self.entities.insert(entity, original) {
            self.query_bodies.remove(&old);
            self.bodies.remove(&old);
        }
        self.query_bodies.insert(original);
        self.bodies.insert(original, body);
        if let Some(config) = ground {
            self.ground.insert(entity, ground::State::new(config));
        }
        Ok(())
    }

    fn set_pose(
        &self,
        original: i32,
        position: [f64; 3],
        rotation: [f64; 4],
    ) -> Result<(), String> {
        // Only reuse a transform within one synchronous step. Python can
        // relocate the same body between steps, including flight handoffs.
        let probe = Some((original, position, rotation));
        if self.last_probe.get() == probe {
            return Ok(());
        }
        unsafe {
            let command = (self.client.api.pose_init)(self.client.handle, original);
            (self.client.api.position)(command, position[0], position[1], position[2]);
            (self.client.api.orientation)(
                command,
                rotation[0],
                rotation[1],
                rotation[2],
                rotation[3],
            );
            self.client.execute(command)?;
        }
        self.last_probe.set(probe);
        Ok(())
    }

    fn contact_points(
        &self,
        entity: u32,
        skin: f64,
        ground_pose: Option<[u64; 6]>,
        clearance: Option<&mut ContactClearance>,
    ) -> Result<Vec<Contact>, String> {
        let original = *self.entities.get(&entity).ok_or("Unknown entity")?;
        let body = &self.bodies[&original];
        let mut low = [f64::INFINITY; 3];
        let mut high = [f64::NEG_INFINITY; 3];
        unsafe {
            let status = self.client.execute((self.client.api.collision_info)(
                self.client.handle,
                original,
            ))?;
            for link in -1..body.links {
                let mut minimum = [0.; 3];
                let mut maximum = [0.; 3];
                if (self.client.api.aabb)(status, link, minimum.as_mut_ptr(), maximum.as_mut_ptr())
                    == 0
                {
                    return Err("Bullet did not return a body AABB".into());
                }
                for axis in 0..3 {
                    low[axis] = low[axis].min(minimum[axis] - skin);
                    high[axis] = high[axis].max(maximum[axis] + skin);
                }
            }
        }
        if let (Some(bits), Some(state @ ContactClearance::Unknown)) = (ground_pose, clearance) {
            let room_low = low.map(|v| v - ContactClearance::PADDING);
            let room_high = high.map(|v| v + ContactClearance::PADDING);
            let room = self.client.overlapping(room_low, room_high)?;
            if room.iter().all(|row| row.body == original || !self.query_bodies.contains(&row.body)
                || self.terrain_bodies.contains(&row.body))
            {
                let pose = bits.map(f64::from_bits);
                let radius = (0..3).map(|i| (low[i] - pose[i]).abs().max((high[i] - pose[i]).abs()).powi(2))
                    .sum::<f64>().sqrt();
                *state = ContactClearance::Empty { pose, radius };
                return Ok(Vec::new());
            }
            *state = ContactClearance::Occupied;
        }
        let near = self.client.overlapping(low, high)?;
        let candidates: BTreeSet<_> = near
            .iter()
            .map(|row| row.body)
            .filter(|id| *id != original && self.query_bodies.contains(id))
            .collect();
        let mut points = Vec::new();
        for other in candidates {
            if self.terrain_bodies.contains(&other) {
                continue;
            }
            // Probes in the same Bullet world can reuse exact contacts against
            // immutable map geometry. Vehicle and breakable pairs stay live.
            let key = ground_pose.filter(|_| self.solid_map(other))
                .map(|pose| (pose, body.turret_bits.get(), skin.to_bits(), other));
            if let Some(key) = key {
                let mut cached = body.static_contacts.borrow_mut();
                if let Some(previous) = cached.contacts.get(&key) {
                    points.extend(previous.iter().copied());
                } else {
                    let result = self.client.closest(original, other, skin)?;
                    points.extend(result.iter().copied());
                    if result.len() <= STATIC_PAIR_ROWS {
                        if cached.contacts.len() >= STATIC_PAIR_ENTRIES
                            || cached.rows + result.len() > STATIC_PAIR_ROWS
                        {
                            cached.contacts.clear();
                            cached.rows = 0;
                        }
                        cached.rows += result.len();
                        cached.contacts.insert(key, result);
                    }
                }
            } else {
                points.extend(self.client.closest(original, other, skin)?);
            }
        }
        Ok(points)
    }

    fn sensor_enabled(&mut self, entity: u32, enabled: bool) -> Result<(), String> {
        let original = *self.entities.get(&entity).ok_or("Unknown entity")?;
        if enabled {
            self.query_bodies.insert(original);
        } else {
            self.query_bodies.remove(&original);
        }
        Ok(())
    }

    fn query(&mut self, request: &Value) -> Result<Value, String> {
        match request["operation"]
            .as_str()
            .ok_or("Missing query operation")?
        {
            "movement_limits" => {
                let entity = request["entity"].as_u64().and_then(|id| u32::try_from(id).ok())
                    .ok_or("Invalid movement entity")?;
                let bounds = request.get("bounds").map(vector::<4>).transpose()?;
                let circle = request.get("circle").map(vector::<3>).transpose()?;
                self.ground.get_mut(&entity).ok_or("Unknown movement entity")?.set_limits(bounds, circle)?;
                Ok(Value::Null)
            }
            "zone_members" => {
                let members = request["bodies"].as_array().ok_or("Missing zone members")?
                    .iter().map(|v| v.as_i64().and_then(|n| i32::try_from(n).ok())
                        .filter(|id| self.bodies.contains_key(id)).ok_or("Invalid zone member"))
                    .collect::<Result<BTreeSet<_>, _>>()?;
                self.inside_zone = members;
                Ok(Value::Null)
            }
            "restore_prop" => {
                let key = request["key"].to_string();
                // The owner parks destructibles in range arenas. Their original IDs
                // remain allocated until the arena closes and may be restored.
                let bodies = self.parked_props.remove(&key).ok_or("Unknown parked prop")?;
                self.query_bodies.extend(&bodies);
                self.map_bodies.extend(&bodies);
                self.breakables.insert(key, bodies);
                Ok(Value::Null)
            }
            "replace_vehicle" => {
                self.install_vehicle(&request["vehicle"])?;
                Ok(Value::Null)
            }
            "destroy" => {
                self.forget_object(&request["key"].to_string());
                Ok(Value::Null)
            }
            "state" => {
                let entity = request["entity"].as_u64().ok_or("Missing state entity")? as u32;
                Ok(self
                    .ground
                    .get(&entity)
                    .ok_or("Unknown state entity")?
                    .snapshot())
            }
            "events" => {
                let contacts=self.contact_events.iter().map(|(entity,pose,points)| json!({
                    "entity":entity,"pose":[pose.x,pose.y,pose.z,pose.yaw,pose.pitch,pose.roll],
                    "points":points.iter().map(contact_json).collect::<Vec<_>>()
                })).collect::<Vec<_>>();
                let destroyed = self
                    .destroyed_events
                    .iter()
                    .map(|(key, dx, dz)| {
                        json!({
                            "key":serde_json::from_str::<Value>(key).unwrap(),"direction":[dx,dz]
                        })
                    })
                    .collect::<Vec<_>>();
                Ok(json!({"contacts":contacts,"destroyed":destroyed}))
            }
            "contacts" => {
                let entity = request["entity"].as_u64().ok_or("Missing query entity")? as u32;
                let skin = request["skin"].as_f64().ok_or("Missing contact skin")?;
                if !skin.is_finite() || skin < 0. {
                    return Err("Invalid contact skin".into());
                }
                let points = self.contact_points(entity, skin, None, None)?;
                Ok(json!(points
                    .into_iter()
                    .map(|point| json!([
                        point.flags,
                        point.body_a,
                        point.body_b,
                        point.link_a,
                        point.link_b,
                        point.point_a,
                        point.point_b,
                        point.normal,
                        point.distance,
                        point.force,
                        point.friction_force_1,
                        point.friction_direction_1,
                        point.friction_force_2,
                        point.friction_direction_2,
                    ]))
                    .collect::<Vec<_>>()))
            }
            "rays" => {
                let rays = request["rays"]
                    .as_array()
                    .ok_or("Missing rays")?
                    .iter()
                    .map(|row| Ok((vector::<3>(&row[0])?, vector::<3>(&row[1])?)))
                    .collect::<Result<Vec<_>, String>>()?;
                let hits = self.client.cast_rays(&rays)?;
                Ok(json!(hits
                    .into_iter()
                    .map(|hit| json!([
                        hit.body,
                        hit.link,
                        hit.fraction,
                        hit.point,
                        hit.normal,
                    ]))
                    .collect::<Vec<_>>()))
            }
            _ => Err("Unknown query operation".into()),
        }
    }

    fn solid_map(&self, original: i32) -> bool {
        self.solid_map_bodies.contains(&original)
    }

    fn forget_object(&mut self, key: &str) {
        if let Some(bodies) = self.breakables.remove(key) {
            for body in &bodies {
                self.query_bodies.remove(body);
                self.map_bodies.remove(body);
            }
            if self.collision_zone.is_some() {
                self.parked_props.insert(key.to_owned(), bodies);
            }
        }
    }

    fn filter_static(&self, body: i32, group: i32, mask: i32) -> Result<(), String> {
        unsafe {
            let command = (self.client.api.filter_init)(self.client.handle);
            (self.client.api.filter_mask)(command, body, -1, group, mask);
            self.client.execute(command)?;
        }
        Ok(())
    }

    fn destroy_object(&mut self, key: &str) -> Result<(), String> {
        // The same drive can retry through the prop. Exclude it from rays now.
        // Python commits game damage and removes (or parks) the body once.
        if let Some(bodies) = self.breakables.get(key) {
            for &body in bodies {
                self.filter_static(body, 0, 0)?;
                self.disabled_during_step.push(body);
            }
        }
        self.forget_object(key);
        Ok(())
    }

    fn cast(&self, rays: &[([f64; 3], [f64; 3])]) -> Result<Vec<bullet_api::RayHit>, String> {
        self.client.cast_rays(rays)
    }

}

fn contact_json(point: &Contact) -> Value {
    json!([
        point.flags,
        point.body_a,
        point.body_b,
        point.link_a,
        point.link_b,
        point.point_a,
        point.point_b,
        point.normal,
        point.distance,
        point.force,
        point.friction_force_1,
        point.friction_direction_1,
        point.friction_force_2,
        point.friction_direction_2
    ])
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_step(
    handle: *mut c_void,
    input: *const StepInput,
    output: *mut StepOutput,
) -> i32 {
    if handle.is_null()
        || input.is_null()
        || output.is_null()
        || !input.is_aligned()
        || !output.is_aligned()
    {
        return -1;
    }
    let Ok((world, _lease)) = (&mut *handle.cast::<WorldHandle>()).access() else { return -2 };
    if world.poisoned {
        return -2;
    }
    // Reject malformed commands before touching authoritative state.
    let command = *input;
    let drive = match command.drive() {
        Ok(drive) => drive,
        Err(error) => {
            world.error = error;
            return -3;
        }
    };
    if !world.ground.contains_key(&command.entity) {
        world.error = "Missing native vehicle configuration".into();
        return -3;
    }
    world.bodies[&world.entities[&command.entity]].turret_bits.set(command.turret_yaw.to_bits());
    world.last_probe.set(None);
    world.disabled_during_step.clear();
    world.contact_events.clear();
    world.destroyed_events.clear();
    let result = catch_unwind(AssertUnwindSafe(|| {
        let (state, outcome) = ground::step(world, command.entity, drive, &command)?;
        let events = (world.contact_events.len() + world.destroyed_events.len()) as u32;
        let value = state.output(&outcome, events);
        world.set_pose(
            world.entities[&command.entity],
            [value.pose.x, value.pose.y, value.pose.z],
            ground::quaternion(value.pose),
        )?;
        world.ground.insert(command.entity, state);
        Ok::<_, String>(value)
    }));
    if !matches!(result, Ok(Ok(_))) {
        // Restore the physical arena even if this controller becomes unusable.
        world.last_probe.set(None);
        let pose = command.pose;
        let _ = world.set_pose(world.entities[&command.entity],
            [pose.x, pose.y, pose.z], ground::quaternion(pose));
        for &body in &world.disabled_during_step {
            let _ = world.filter_static(body, world.client.static_group, world.static_mask);
        }
    }
    match result {
        Ok(Ok(value)) => {
            *output = value;
            0
        }
        Ok(Err(error)) => {
            world.error = error;
            world.poisoned = true;
            -3
        }
        Err(_) => {
            world.error = "Panic stepping native physics".into();
            world.poisoned = true;
            -4
        }
    }
}

unsafe fn json_argument(pointer: *const c_char) -> Result<Value, String> {
    if pointer.is_null() {
        return Err("Missing JSON argument".into());
    }
    serde_json::from_slice(CStr::from_ptr(pointer).to_bytes()).map_err(|error| error.to_string())
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_sensor_enabled(
    handle: *mut c_void, entity: u32, enabled: bool,
) -> i32 {
    if handle.is_null() {
        return -1;
    }
    let Ok((world, _lease)) = (&mut *handle.cast::<WorldHandle>()).access() else { return -2 };
    if world.poisoned {
        return -2;
    }
    match catch_unwind(AssertUnwindSafe(|| world.sensor_enabled(entity, enabled))) {
        Ok(Ok(())) => 0,
        Ok(Err(error)) => {
            world.error = error;
            -3
        }
        Err(_) => {
            world.error = "Panic changing native sensor presence".into();
            world.poisoned = true;
            -4
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_attach_grid(
    token: i32,
    fixture: *const c_char,
    library: *const c_char,
    heights: *const u16,
    count: usize,
) -> *mut c_void {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let fixture = json_argument(fixture)?;
        if library.is_null() {
            return Err("Missing Bullet library path".to_string());
        }
        if heights.is_null() || !heights.is_aligned() || count == 0 || count > 8192 * 8192 {
            return Err("Invalid terrain grid buffer".to_string());
        }
        let session = plugin::session(token).ok_or("Unknown Bullet arena")?;
        let guard = session.guard()?;
        let world = World::new(
            &fixture,
            CStr::from_ptr(library)
                .to_str()
                .map_err(|error| error.to_string())?,
            session.client(),
            std::slice::from_raw_parts(heights, count),
        )?;
        drop(guard);
        Ok(WorldHandle { session, world })
    }));
    match result {
        Ok(Ok(world)) => Box::into_raw(Box::new(world)).cast(),
        Ok(Err(error)) => {
            CREATE_ERROR.with(|slot| *slot.borrow_mut() = error);
            std::ptr::null_mut()
        }
        Err(_) => {
            CREATE_ERROR.with(|slot| *slot.borrow_mut() = "Panic creating physics world".into());
            std::ptr::null_mut()
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_query(
    handle: *mut c_void,
    request: *const c_char,
    output: *mut u8,
    capacity: usize,
) -> i32 {
    if handle.is_null() || output.is_null() || capacity == 0 {
        return -1;
    }
    let Ok((world, _lease)) = (&mut *handle.cast::<WorldHandle>()).access() else { return -2 };
    if world.poisoned {
        return -2;
    }
    let result = catch_unwind(AssertUnwindSafe(|| world.query(&json_argument(request)?)));
    let value = match result {
        Ok(Ok(value)) => value,
        Ok(Err(error)) => {
            world.error = error;
            return -3;
        }
        Err(_) => {
            world.error = "Panic querying native physics".into();
            world.poisoned = true;
            return -4;
        }
    };
    let bytes = serde_json::to_vec(&value).unwrap();
    if bytes.len() >= capacity {
        return -5;
    }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len());
    bytes.len() as i32
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_close(handle: *mut c_void) {
    if !handle.is_null() {
        drop(Box::from_raw(handle.cast::<WorldHandle>()));
    }
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_error(
    handle: *mut c_void,
    output: *mut u8,
    capacity: usize,
) -> usize {
    if output.is_null() || capacity == 0 {
        return 0;
    }
    let error = if handle.is_null() {
        CREATE_ERROR.with(|slot| slot.borrow().clone())
    } else {
        (&*handle.cast::<WorldHandle>()).world.error.clone()
    };
    let length = error.len().min(capacity - 1);
    std::ptr::copy_nonoverlapping(error.as_ptr(), output, length);
    *output.add(length) = 0;
    length
}
