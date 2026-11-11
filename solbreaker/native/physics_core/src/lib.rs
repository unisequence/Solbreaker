//! Stateful native physics and geometry for the experimental server branch.
mod bullet_api;
mod ground;
mod support;

use bullet_api::{Client, Contact};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{c_char, c_void, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use tfs_sim_core::{DriveInput, Motion, Pose, Terrain};

const ZERO: [f64; 3] = [0.; 3];
const IDENTITY: [f64; 4] = [0., 0., 0., 1.];
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

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SensorState {
    entity: u32,
    pose: Pose,
    turret: f64,
}

#[no_mangle]
pub extern "C" fn solbreaker_physics_abi_sizes() -> u64 {
    ((std::mem::size_of::<StepInput>() as u64) << 32)
        | ((std::mem::size_of::<StepOutput>() as u64) << 16)
        | std::mem::size_of::<SensorState>() as u64
}

#[no_mangle]
pub unsafe extern "C" fn solbreaker_physics_sync(
    handle: *mut c_void,
    states: *const SensorState,
    count: usize,
) -> i32 {
    if handle.is_null() || states.is_null() || !states.is_aligned() || count > 1024 {
        return -1;
    }
    let world = &mut *handle.cast::<World>();
    if world.poisoned {
        return -2;
    }
    let states = std::slice::from_raw_parts(states, count);
    if states.iter().any(|row| {
        !world.entities.contains_key(&row.entity)
            || ![
                row.pose.x,
                row.pose.y,
                row.pose.z,
                row.pose.yaw,
                row.pose.pitch,
                row.pose.roll,
                row.turret,
            ]
            .iter()
            .all(|v| v.is_finite())
    }) {
        world.error = "Invalid native sensor state".into();
        return -3;
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        for row in states {
            world.set_pose(
                world.entities[&row.entity],
                [row.pose.x, row.pose.y, row.pose.z],
                ground::quaternion(row.pose),
                Some(row.turret),
            )?;
        }
        Ok::<_, String>(())
    }));
    match result {
        Ok(Ok(())) => 0,
        Ok(Err(error)) => {
            world.error = error;
            world.poisoned = true;
            -3
        }
        Err(_) => {
            world.error = "Panic synchronizing physics".into();
            world.poisoned = true;
            -4
        }
    }
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
        if self.abi_version != 2
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

struct Body {
    native_id: i32,
    links: i32,
    turret: Option<i32>,
}

struct World {
    client: Client,
    bodies: BTreeMap<i32, Body>,
    original_ids: BTreeMap<i32, i32>,
    entities: BTreeMap<u32, i32>,
    map_bodies: BTreeSet<i32>,
    terrain_bodies: BTreeSet<i32>,
    breakables: BTreeMap<String, Vec<i32>>,
    error: String,
    poisoned: bool,
    heightfield: Option<ground::Heightfield>,
    ground: BTreeMap<u32, ground::State>,
    contact_events: Vec<(u32, tfs_sim_core::Pose, Vec<Contact>)>,
    destroyed_events: Vec<(String, f64, f64)>,
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
    fn new(fixture: &Value, library: &str) -> Result<Self, String> {
        let mut world = Self {
            client: Client::open(library)?,
            bodies: BTreeMap::new(),
            original_ids: BTreeMap::new(),
            entities: BTreeMap::new(),
            map_bodies: BTreeSet::new(),
            terrain_bodies: BTreeSet::new(),
            breakables: BTreeMap::new(),
            error: String::new(),
            poisoned: false,
            heightfield: fixture
                .get("terrain")
                .map(ground::Heightfield::new)
                .transpose()?,
            ground: BTreeMap::new(),
            contact_events: Vec::new(),
            destroyed_events: Vec::new(),
        };
        for entry in fixture["static"]
            .as_array()
            .ok_or("Missing static geometry")?
        {
            let shape = world.shape(entry, true)?;
            let body = world.create_body(shape, &[], ZERO, IDENTITY)?;
            unsafe {
                let command = (world.client.api.filter_init)(world.client.handle);
                (world.client.api.filter_mask)(command, body.native_id, -1, 1, -1);
                world.client.execute(command)?;
            }
            let original = entry["body"].as_i64().ok_or("Missing static body id")? as i32;
            world.original_ids.insert(body.native_id, original);
            let kind = entry["kind"].as_str().ok_or("Missing geometry kind")?;
            if kind == "terrain" {
                world.terrain_bodies.insert(original);
            } else {
                world.map_bodies.insert(original);
            }
            if kind == "breakable" {
                let key = entry["key"].to_string();
                world.breakables.entry(key).or_default().push(original);
            }
            world.bodies.insert(original, body);
        }
        // Match the query world's static broad-phase preparation.
        for _ in 0..10 {
            world
                .client
                .execute(unsafe { (world.client.api.detect)(world.client.handle) })?;
        }
        for entry in fixture["entities"]
            .as_array()
            .ok_or("Missing vehicle geometry")?
        {
            let entity = entry["id"].as_u64().ok_or("Missing entity id")? as u32;
            let original = entry["body"].as_i64().ok_or("Missing vehicle body id")? as i32;
            let parts = entry["parts"].as_array().ok_or("Missing vehicle parts")?;
            let hull = parts
                .iter()
                .position(|part| part["name"] == "hull")
                .ok_or("Missing hull part")?;
            let shape = world.shape(&parts[hull], false)?;
            let mut links = Vec::new();
            let mut turret = None;
            for (index, part) in parts.iter().enumerate() {
                if index == hull {
                    continue;
                }
                let shape = world.shape(part, false)?;
                let moving = part["turret"].as_bool().unwrap_or(false);
                if moving {
                    turret = Some(links.len() as i32);
                }
                links.push((shape, vector::<3>(&part["offset"])?, moving));
            }
            let mut body = world.create_body(
                shape,
                &links,
                vector::<3>(&entry["position"])?,
                vector::<4>(&entry["orientation"])?,
            )?;
            body.turret = turret;
            unsafe {
                let command = (world.client.api.filter_init)(world.client.handle);
                for link in -1..body.links {
                    (world.client.api.filter_mask)(command, body.native_id, link, 2, -1);
                }
                world.client.execute(command)?;
                let command = (world.client.api.dynamics_init)(world.client.handle);
                (world.client.api.collision_margin)(command, body.native_id, 0.001);
                world.client.execute(command)?;
            }
            world.original_ids.insert(body.native_id, original);
            world.entities.insert(entity, original);
            world.bodies.insert(original, body);
            if let Some(value) = entry.get("ground") {
                world.ground.insert(
                    entity,
                    ground::State::new(ground::Configuration::new(value)?),
                );
            }
        }
        Ok(world)
    }

    fn shape(&self, value: &Value, concave: bool) -> Result<i32, String> {
        unsafe {
            let command = (self.client.api.shape_init)(self.client.handle);
            let kind = value["kind"].as_str().unwrap_or("mesh");
            let index = match kind {
                "mesh" | "map" | "terrain" | "breakable" | "prism" => {
                    let path = CString::new(value["path"].as_str().ok_or("Missing mesh path")?)
                        .map_err(|error| error.to_string())?;
                    (self.client.api.mesh)(command, path.as_ptr(), [1., 1., 1.].as_ptr())
                }
                "box" => (self.client.api.box_shape)(
                    command,
                    vector::<3>(&value["half_extents"])?.as_ptr(),
                ),
                "cylinder" => (self.client.api.cylinder)(
                    command,
                    value["radius"].as_f64().ok_or("Missing cylinder radius")?,
                    value["length"].as_f64().ok_or("Missing cylinder length")?,
                ),
                _ => return Err("Unsupported query shape".into()),
            };
            if concave {
                (self.client.api.shape_flags)(command, index, 1);
            }
            if let Some(center) = value.get("center") {
                (self.client.api.shape_transform)(
                    command,
                    index,
                    vector::<3>(center)?.as_ptr(),
                    IDENTITY.as_ptr(),
                );
            }
            let status = self.client.execute(command)?;
            let shape = (self.client.api.shape_id)(status);
            if shape < 0 {
                return Err("Bullet shape creation failed".into());
            }
            Ok(shape)
        }
    }

    fn create_body(
        &self,
        shape: i32,
        links: &[(i32, [f64; 3], bool)],
        position: [f64; 3],
        orientation: [f64; 4],
    ) -> Result<Body, String> {
        unsafe {
            let command = (self.client.api.body_init)(self.client.handle);
            (self.client.api.body_base)(
                command,
                0.,
                shape,
                -1,
                position.as_ptr(),
                orientation.as_ptr(),
                ZERO.as_ptr(),
                IDENTITY.as_ptr(),
            );
            for (shape, offset, turret) in links {
                (self.client.api.body_link)(
                    command,
                    0.,
                    *shape as f64,
                    -1.,
                    offset.as_ptr(),
                    IDENTITY.as_ptr(),
                    ZERO.as_ptr(),
                    IDENTITY.as_ptr(),
                    0,
                    if *turret { 0 } else { 4 },
                    [0., 1., 0.].as_ptr(),
                );
            }
            let status = self.client.execute(command)?;
            let native_id = (self.client.api.body_id)(status);
            if native_id < 0 {
                return Err("Bullet body creation failed".into());
            }
            Ok(Body {
                native_id,
                links: links.len() as i32,
                turret: None,
            })
        }
    }

    fn set_pose(
        &self,
        original: i32,
        position: [f64; 3],
        rotation: [f64; 4],
        turret: Option<f64>,
    ) -> Result<(), String> {
        let body = self.bodies.get(&original).ok_or("Unknown body")?;
        unsafe {
            let command = (self.client.api.pose_init)(self.client.handle, body.native_id);
            (self.client.api.position)(command, position[0], position[1], position[2]);
            (self.client.api.orientation)(
                command,
                rotation[0],
                rotation[1],
                rotation[2],
                rotation[3],
            );
            if let (Some(joint), Some(angle)) = (body.turret, turret) {
                (self.client.api.joint_position)(self.client.handle, command, joint, angle);
            }
            self.client.execute(command)?;
        }
        Ok(())
    }

    fn contact_points(&self, entity: u32, skin: f64) -> Result<Vec<Contact>, String> {
        let original = *self.entities.get(&entity).ok_or("Unknown entity")?;
        let body = &self.bodies[&original];
        let mut low = [f64::INFINITY; 3];
        let mut high = [f64::NEG_INFINITY; 3];
        unsafe {
            let status = self.client.execute((self.client.api.collision_info)(
                self.client.handle,
                body.native_id,
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
        let near = self.client.overlapping(low, high)?;
        let candidates: BTreeSet<_> = near
            .iter()
            .map(|row| row.body)
            .filter(|id| *id != body.native_id && self.original_ids.contains_key(id))
            .collect();
        let mut points = Vec::new();
        for other in candidates {
            let other_original = self.original_ids[&other];
            if self.terrain_bodies.contains(&other_original) {
                continue;
            }
            for mut point in self.client.closest(body.native_id, other, skin)? {
                point.body_a = original;
                point.body_b = other_original;
                points.push(point);
            }
        }
        Ok(points)
    }

    fn query(&mut self, request: &Value) -> Result<Value, String> {
        match request["operation"]
            .as_str()
            .ok_or("Missing query operation")?
        {
            "sync" => {
                let mut states = Vec::new();
                for row in request["entities"]
                    .as_array()
                    .ok_or("Missing sensor states")?
                {
                    let id = row["id"].as_u64().ok_or("Missing entity id")? as u32;
                    let original = *self.entities.get(&id).ok_or("Unknown entity")?;
                    states.push((
                        original,
                        vector::<3>(&row["position"])?,
                        vector::<4>(&row["orientation"])?,
                        row["turret"].as_f64(),
                    ));
                }
                for (original, position, orientation, turret) in states {
                    if let Err(error) = self.set_pose(original, position, orientation, turret) {
                        self.poisoned = true;
                        return Err(error);
                    }
                }
                Ok(Value::Null)
            }
            "destroy" => {
                self.destroy_object(&request["key"].to_string())?;
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
                let points = self.contact_points(entity, skin)?;
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
                        self.original_ids.get(&hit.body).copied().unwrap_or(-1),
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
        self.map_bodies.contains(&original)
            && !self
                .breakables
                .values()
                .any(|bodies| bodies.contains(&original))
    }

    fn destroy_object(&mut self, key: &str) -> Result<(), String> {
        let Some(originals) = self.breakables.get(key).cloned() else {
            return Ok(());
        };
        if originals
            .iter()
            .any(|original| !self.bodies.contains_key(original))
        {
            return Err("Missing breakable body".into());
        }
        for original in &originals {
            let body = &self.bodies[original];
            if let Err(error) = self
                .client
                .execute(unsafe { (self.client.api.remove)(self.client.handle, body.native_id) })
            {
                self.poisoned = true;
                return Err(error);
            }
            self.original_ids.remove(&body.native_id);
            self.map_bodies.remove(original);
            self.bodies.remove(original);
        }
        self.breakables.remove(key);
        Ok(())
    }

    fn cast(&self, rays: &[([f64; 3], [f64; 3])]) -> Result<Vec<bullet_api::RayHit>, String> {
        let mut hits = self.client.cast_rays(rays)?;
        for hit in &mut hits {
            hit.body = self.original_ids.get(&hit.body).copied().unwrap_or(-1);
        }
        Ok(hits)
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
    let world = &mut *handle.cast::<World>();
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
    if !world.ground.contains_key(&command.entity) || world.heightfield.is_none() {
        world.error = "Missing native vehicle or terrain configuration".into();
        return -3;
    }
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
            None,
        )?;
        world.ground.insert(command.entity, state);
        Ok::<_, String>(value)
    }));
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
pub unsafe extern "C" fn solbreaker_physics_create(
    fixture: *const c_char,
    library: *const c_char,
) -> *mut c_void {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let fixture = json_argument(fixture)?;
        if library.is_null() {
            return Err("Missing Bullet library path".to_string());
        }
        World::new(
            &fixture,
            CStr::from_ptr(library)
                .to_str()
                .map_err(|error| error.to_string())?,
        )
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
    let world = &mut *handle.cast::<World>();
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
        drop(Box::from_raw(handle.cast::<World>()));
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
        (&*handle.cast::<World>()).error.clone()
    };
    let length = error.len().min(capacity - 1);
    std::ptr::copy_nonoverlapping(error.as_ptr(), output, length);
    *output.add(length) = 0;
    length
}
