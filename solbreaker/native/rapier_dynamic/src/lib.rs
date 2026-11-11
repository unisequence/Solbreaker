use rapier3d_f64::dynamics::{
    CCDSolver, ImpulseJointSet, IntegrationParameters, IslandManager, MassProperties,
    MultibodyJointSet, RigidBodyBuilder, RigidBodyHandle, RigidBodySet,
};
use rapier3d_f64::geometry::{
    BroadPhaseMultiSap, ColliderBuilder, ColliderHandle, ColliderSet, NarrowPhase, SharedShape, Ray,
};
use rapier3d_f64::math::{Isometry, Point, Rotation, Translation, Vector};
use rapier3d_f64::pipeline::{PhysicsPipeline, QueryPipeline, QueryFilter};
use rapier3d_f64::prelude::{CoefficientCombineRule, TriMeshFlags};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::ffi::{c_char, CStr};
use std::fs;
use std::panic::{catch_unwind, AssertUnwindSafe};

fn vec3(value: &Value) -> Vector<f64> {
    let row = value.as_array().unwrap();
    assert_eq!(row.len(), 3, "expected vec3");
    let result = Vector::new(row[0].as_f64().unwrap(), row[1].as_f64().unwrap(), row[2].as_f64().unwrap());
    assert!(result.iter().all(|v| v.is_finite()), "nonfinite vector");
    result
}

fn quat(value: &Value) -> [f64; 4] {
    let row = value.as_array().unwrap();
    assert_eq!(row.len(), 4, "expected quaternion");
    let result = [row[0].as_f64().unwrap(), row[1].as_f64().unwrap(),
                  row[2].as_f64().unwrap(), row[3].as_f64().unwrap()];
    assert!(valid_quaternion(&result), "invalid quaternion");
    result
}

fn obj(path: &str) -> (Vec<Point<f64>>, Vec<[u32; 3]>) {
    let text = fs::read_to_string(path).unwrap();
    let mut vertices = Vec::new();
    let mut triangles = Vec::new();
    for line in text.lines() {
        let mut columns = line.split_whitespace();
        match columns.next() {
            Some("v") => {
                let values: Vec<f64> = columns.map(|v| v.parse().unwrap()).collect();
                vertices.push(Point::new(values[0], values[1], values[2]));
            }
            Some("f") => {
                let count = vertices.len() as i32;
                let face: Vec<u32> = columns.map(|v| {
                    let index: i32 = v.split('/').next().unwrap().parse().unwrap();
                    if index < 0 { (count+index) as u32 } else { (index-1) as u32 }
                }).collect();
                for i in 1..face.len()-1 {
                    triangles.push([face[0], face[i], face[i+1]]);
                }
            }
            _ => {}
        }
    }
    (vertices, triangles)
}

fn isometry(position: Vector<f64>, rotation: &[f64]) -> Isometry<f64> {
    let quaternion = rapier3d_f64::na::Quaternion::new(rotation[3], rotation[0], rotation[1], rotation[2]);
    Isometry::from_parts(Translation::from(position), Rotation::from_quaternion(quaternion))
}

fn valid_quaternion(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite()) && values.iter().map(|v| v*v).sum::<f64>().is_finite()
        && values.iter().map(|v| v*v).sum::<f64>() > 1e-24
}

fn entity_id(value: &Value) -> u32 {
    u32::try_from(value.as_u64().expect("expected entity id")).expect("entity id overflow")
}

fn breakable_key(value: &Value) -> String {
    value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string())
}

struct EntityDefinition {
    mass: MassProperties,
    parts: Value,
}

pub struct World {
    poisoned: bool,
    bodies: RigidBodySet,
    colliders: ColliderSet,
    definitions: BTreeMap<u32, EntityDefinition>,
    sensors: BTreeMap<u32, RigidBodyHandle>,
    dynamic: BTreeMap<u32, RigidBodyHandle>,
    collider_entities: HashMap<ColliderHandle, Value>,
    breakables: HashMap<String, Vec<ColliderHandle>>,
    pipeline: PhysicsPipeline,
    islands: IslandManager,
    broad_phase: BroadPhaseMultiSap,
    narrow_phase: NarrowPhase,
    impulse_joints: ImpulseJointSet,
    multibody_joints: MultibodyJointSet,
    ccd: CCDSolver,
    convex_cache: HashMap<String, SharedShape>,
    query: QueryPipeline,
    query_dirty: bool,
}

impl World {
    fn new(fixture: &Value) -> Self {
        let mut world = World {
            poisoned: false, bodies: RigidBodySet::new(), colliders: ColliderSet::new(),
            definitions: BTreeMap::new(), sensors: BTreeMap::new(), dynamic: BTreeMap::new(),
            collider_entities: HashMap::new(), breakables: HashMap::new(),
            pipeline: PhysicsPipeline::new(), islands: IslandManager::new(),
            broad_phase: BroadPhaseMultiSap::new(), narrow_phase: NarrowPhase::new(),
            impulse_joints: ImpulseJointSet::new(), multibody_joints: MultibodyJointSet::new(),
            ccd: CCDSolver::new(), convex_cache: HashMap::new(),
            query: QueryPipeline::new(), query_dirty: true,
        };
        for row in fixture["static"].as_array().unwrap() {
            let path = row["path"].as_str().unwrap();
            let (vertices, triangles) = obj(path);
            let kind = row["kind"].as_str().unwrap_or("map");
            // Terrain diagonals are not physical ridges. Unconstrained edge
            // normals can turn a landing into a second angular kick (T100LT
            // at Fort); keep the adjacent triangle normal constraints.
            let flags = if kind == "terrain" { TriMeshFlags::FIX_INTERNAL_EDGES } else { TriMeshFlags::empty() };
            let handle = world.colliders.insert(ColliderBuilder::trimesh_with_flags(vertices, triangles, flags)
                .unwrap().build());
            world.collider_entities.insert(handle, json!({"kind": kind, "id": row["key"], "body": row["body"]}));
            if kind == "breakable" {
                world.breakables.entry(breakable_key(&row["key"])).or_default().push(handle);
            }
        }
        for row in fixture["entities"].as_array().unwrap() {
            let id = entity_id(&row["id"]);
            let position = isometry(vec3(&row["initial_position"]), &quat(&row["initial_orientation"]));
            assert!(!world.definitions.contains_key(&id), "duplicate entity id");
            let scalar_mass = row["mass"].as_f64().unwrap();
            assert!(scalar_mass.is_finite() && scalar_mass > 0., "invalid mass");
            assert!(vec3(&row["inertia"]).iter().all(|v| *v > 0.), "invalid inertia");
            let mass = MassProperties::new(vec3(&row["center_of_mass"]).into(),
                row["mass"].as_f64().unwrap(), vec3(&row["inertia"]));
            world.definitions.insert(id, EntityDefinition {
                mass, parts: row["parts"].clone(),
            });
            let sensor = world.bodies.insert(RigidBodyBuilder::kinematic_position_based()
                .position(position).build());
            world.sensors.insert(id, sensor);
            world.add_parts(id, sensor, &world.definitions[&id].parts.clone());
        }
        world
    }

    fn add_parts(&mut self, id: u32, handle: RigidBodyHandle, parts: &Value) {
        for part in parts.as_array().unwrap() {
            let offset = vec3(&part["offset"]);
            let position = Isometry::from_parts(Translation::from(offset), Rotation::identity());
            let builder = match part["kind"].as_str().unwrap() {
                "prism" => {
                    let path = part["path"].as_str().unwrap();
                    // These OBJ files carry vertex clouds, not boundary
                    // meshes: MeshFiles emits a fan to reference each vertex.
                    // Match Bullet's GEOM_MESH convex hull, including solid
                    // volume and all bottom/side faces. Terrain stays trimesh.
                    let shape = self.convex_cache.entry(path.to_string()).or_insert_with(|| {
                        let (vertices, _) = obj(path);
                        SharedShape::convex_hull(&vertices).expect("degenerate vehicle prism")
                    }).clone();
                    ColliderBuilder::new(shape).position(position)
                }
                "box" => {
                    let half = vec3(&part["half_extents"]);
                    ColliderBuilder::cuboid(half.x, half.y, half.z).position(position)
                }
                "cylinder" => ColliderBuilder::cylinder(part["length"].as_f64().unwrap()/2.0,
                    part["radius"].as_f64().unwrap()).position(position),
                _ => panic!("unknown part"),
            };
            let collider = self.colliders.insert_with_parent(builder.density(0.0)
                .friction(0.8).friction_combine_rule(CoefficientCombineRule::Average)
                .restitution(0.02).restitution_combine_rule(CoefficientCombineRule::Average)
                .build(), handle, &mut self.bodies);
            let entity = if self.dynamic.contains_key(&id) { "dynamic" } else { "sensor" };
            self.collider_entities.insert(collider, json!({"kind": entity, "id": id}));
        }
    }

    fn set_mass(&mut self, id: u32, values: &[f64]) -> i32 {
        if values.len() != 7 || !values.iter().all(|v| v.is_finite())
            || values[0] <= 0. || values[4..].iter().any(|v| *v <= 0.) { return -2; }
        if self.dynamic.contains_key(&id) { return 1; }
        let Some(definition) = self.definitions.get_mut(&id) else { return 2; };
        definition.mass = MassProperties::new(Point::new(values[1], values[2], values[3]),
            values[0], Vector::new(values[4], values[5], values[6]));
        0
    }

    fn activate(&mut self, id: u32, position: Vector<f64>, orientation: &[f64],
                velocity: Vector<f64>, angular: Vector<f64>) -> i32 {
        if self.dynamic.contains_key(&id) {
            return 1;
        }
        let Some(definition) = self.definitions.get(&id) else { return 2; };
        let body = RigidBodyBuilder::dynamic()
            .position(isometry(position, orientation))
            .additional_mass_properties(definition.mass)
            .ccd_enabled(true)
            .linear_damping(0.015).angular_damping(0.025)
            .linvel(velocity).angvel(angular).build();
        let handle = self.bodies.insert(body);
        self.dynamic.insert(id, handle);
        self.add_parts(id, handle, &definition.parts.clone());
        self.query_dirty = true;
        // Rapier otherwise delays rebuilding effective mass/COM until step.
        // Publish the launch properties before any caller observes the body.
        self.bodies[handle].recompute_mass_properties_from_colliders(&self.colliders);
        if let Some(sensor) = self.sensors.get(&id) {
            for collider in self.bodies[*sensor].colliders() {
                if let Some(collider) = self.colliders.get_mut(*collider) {
                    collider.set_enabled(false);
                }
            }
        }
        0
    }

    fn deactivate(&mut self, id: u32) -> i32 {
        let Some(handle) = self.dynamic.remove(&id) else { return 1; };
        for collider in self.bodies[handle].colliders() {
            self.collider_entities.remove(collider);
        }
        self.bodies.remove(handle, &mut self.islands, &mut self.colliders,
            &mut self.impulse_joints, &mut self.multibody_joints, true);
        self.query_dirty = true;
        if let Some(sensor) = self.sensors.get(&id) {
            for collider in self.bodies[*sensor].colliders() {
                if let Some(collider) = self.colliders.get_mut(*collider) {
                    collider.set_enabled(true);
                }
            }
        }
        0
    }

    fn sync(&mut self, rows: &Value) {
        // Validate the complete request before changing any sensor.
        let validated: Vec<_> = rows.as_array().unwrap().iter().map(|row| {
            (entity_id(&row["id"]), isometry(vec3(&row["position"]), &quat(&row["orientation"])))
        }).collect();
        for (id, position) in validated {
            if self.dynamic.contains_key(&id) {
                continue;
            }
            if let Some(sensor) = self.sensors.get(&id) {
                self.bodies[*sensor].set_position(position, true);
            }
        }
    }

    fn step(&mut self, dt: f64) -> Value {
        let mut params = IntegrationParameters::default();
        params.dt = dt;
        self.pipeline.step(&Vector::new(0.0, -9.81, 0.0), &params,
            &mut self.islands, &mut self.broad_phase, &mut self.narrow_phase,
            &mut self.bodies, &mut self.colliders, &mut self.impulse_joints,
            &mut self.multibody_joints, &mut self.ccd, None, &(), &());
        let mut states = BTreeMap::new();
        for (id, handle) in &self.dynamic {
            let body = &self.bodies[*handle];
            let position = body.position();
            let translation = position.translation.vector;
            let rotation = position.rotation.quaternion().coords;
            states.insert(id.to_string(), json!({
                "position": [translation.x, translation.y, translation.z],
                "orientation": [rotation.x, rotation.y, rotation.z, rotation.w],
                "velocity": body.linvel().as_slice(),
                "angular": body.angvel().as_slice(),
            }));
        }
        let mut contacts = Vec::new();
        for (id, handle) in &self.dynamic {
            for collider in self.bodies[*handle].colliders() {
                for pair in self.narrow_phase.contact_pairs_with(*collider) {
                    let other_handle = if pair.collider1 == *collider { pair.collider2 } else { pair.collider1 };
                    let other = self.collider_entities.get(&other_handle).cloned().unwrap_or(Value::Null);
                    for manifold in &pair.manifolds {
                        let normal = if pair.collider1 == *collider { -manifold.data.normal } else { manifold.data.normal };
                        // Solver contacts can omit manifold points. Zipping the two
                        // arrays associates impulses with unrelated geometry.
                        let c1 = &self.colliders[pair.collider1];
                        let c2 = &self.colliders[pair.collider2];
                        let p1 = manifold.subshape_pos1.map(|p| c1.position() * p).unwrap_or(*c1.position());
                        let p2 = manifold.subshape_pos2.map(|p| c2.position() * p).unwrap_or(*c2.position());
                        for point in &manifold.points {
                            let first = p1 * point.local_p1;
                            let second = p2 * point.local_p2;
                            // Canonical body A is the reported dynamic entity.
                            let (a, b) = if pair.collider1 == *collider {
                                (first, second)
                            } else { (second, first) };
                            let midpoint = rapier3d_f64::na::center(&a, &b);
                            contacts.push(json!({
                                "entity": id,
                                "other": other,
                                "point": midpoint.coords.as_slice(),
                                "point_a": a.coords.as_slice(),
                                "point_b": b.coords.as_slice(),
                                "normal": normal.as_slice(),
                                "distance": point.dist,
                                "impulse": point.data.impulse,
                                // This is Rapier's reported solver impulse/dt,
                                // including its warm-start accounting. It is not
                                // guaranteed to equal net momentum change/dt.
                                "normal_force": point.data.impulse / dt,
                            }));
                        }
                    }
                }
            }
        }
        json!({"states": states, "contacts": contacts})
    }

    fn destroy(&mut self, key: &str) {
        if let Some(handles) = self.breakables.remove(key) {
            for handle in handles {
                self.colliders.remove(handle, &mut self.islands, &mut self.bodies, true);
                self.collider_entities.remove(&handle);
            }
            self.query_dirty = true;
        }
    }

    fn static_rays(&mut self, rays: &[[f64; 6]], output: &mut [StaticRayHit]) {
        if self.query_dirty {
            self.query.update(&self.colliders);
            self.query_dirty = false;
        }
        // Bullet's mask=1 includes terrain, map and breakables, but excludes
        // every tank. Kinematic and dynamic shapes must never occlude armor.
        let predicate = |_: ColliderHandle, collider: &rapier3d_f64::geometry::Collider|
            collider.parent().is_none();
        for (input, out) in rays.iter().zip(output) {
            let start = Point::new(input[0], input[1], input[2]);
            let end = Point::new(input[3], input[4], input[5]);
            let delta = end-start;
            *out = StaticRayHit { body: -1, fraction: 1., point: [end.x,end.y,end.z], normal: [0.;3] };
            if delta.norm_squared() == 0. { continue; }
            if let Some((handle, hit)) = self.query.cast_ray_and_get_normal(
                &self.bodies, &self.colliders, &Ray::new(start, delta), 1., true,
                QueryFilter::default().predicate(&predicate)) {
                let point = start+delta*hit.time_of_impact;
                *out = StaticRayHit {
                    body: self.collider_entities[&handle]["body"].as_i64().unwrap(),
                    fraction: hit.time_of_impact,
                    point: [point.x,point.y,point.z],
                    normal: [hit.normal.x,hit.normal.y,hit.normal.z],
                };
            }
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct StaticRayHit {
    body: i64,
    fraction: f64,
    point: [f64; 3],
    normal: [f64; 3],
}

#[no_mangle]
pub extern "C" fn tfs_rapier_ray_hit_size() -> usize { std::mem::size_of::<StaticRayHit>() }

/// Batch of [start.x,y,z,end.x,y,z]; no JSON in the query hot path.
#[no_mangle]
pub extern "C" fn tfs_rapier_static_rays(world: *mut World, rays: *const [f64; 6],
        count: usize, output: *mut StaticRayHit) -> i32 {
    if count > 65536 || rays.is_null() || output.is_null() { return -2; }
    let input = unsafe { std::slice::from_raw_parts(rays, count) };
    if !input.iter().flatten().all(|v| v.is_finite()) || input.iter().any(|row| {
        let delta = Vector::new(row[3]-row[0],row[4]-row[1],row[5]-row[2]);
        !delta.norm_squared().is_finite()
    }) { return -2; }
    let output = unsafe { std::slice::from_raw_parts_mut(output, count) };
    guarded(world, |world| { world.static_rays(input, output); 0 })
}

// Callers must provide valid live pointers. Nulls and malformed values are
// rejected; arbitrary invalid/double-freed addresses cannot be checked by C ABI.
fn input<'a>(pointer: *const c_char) -> &'a str {
    assert!(!pointer.is_null(), "null string");
    unsafe { CStr::from_ptr(pointer).to_str().unwrap() }
}

fn guarded(world: *mut World, operation: impl FnOnce(&mut World) -> i32) -> i32 {
    if world.is_null() { return -2; }
    let world = unsafe { &mut *world };
    if world.poisoned { return -3; }
    match catch_unwind(AssertUnwindSafe(|| operation(world))) {
        Ok(code) => code,
        Err(_) => { world.poisoned = true; -3 }
    }
}

#[no_mangle]
pub extern "C" fn tfs_rapier_create(path: *const c_char) -> *mut World {
    catch_unwind(AssertUnwindSafe(|| {
        let fixture: Value = serde_json::from_str(&fs::read_to_string(input(path)).unwrap()).unwrap();
        Box::into_raw(Box::new(World::new(&fixture)))
    })).unwrap_or(std::ptr::null_mut())
}

/// Update the actual launch-time mass, center of mass and diagonal inertia.
/// Values are [mass, com.x, com.y, com.z, inertia.x, inertia.y, inertia.z].
#[no_mangle]
pub extern "C" fn tfs_rapier_set_mass(world: *mut World, id: u32, values: *const f64) -> i32 {
    if values.is_null() { return -2; }
    let values = unsafe { std::slice::from_raw_parts(values, 7) };
    guarded(world, |world| world.set_mass(id, values))
}

#[no_mangle]
pub extern "C" fn tfs_rapier_activate(world: *mut World, id: u32, position: *const f64,
        orientation: *const f64, velocity: *const f64, angular: *const f64) -> i32 {
    if position.is_null() || orientation.is_null() || velocity.is_null() || angular.is_null() { return -2; }
    let read = |pointer: *const f64| unsafe { std::slice::from_raw_parts(pointer, 3) };
    let position = read(position);
    let orientation = unsafe { std::slice::from_raw_parts(orientation, 4) };
    let velocity = read(velocity);
    let angular = read(angular);
    if !position.iter().chain(velocity).chain(angular).all(|v| v.is_finite())
        || !valid_quaternion(orientation) { return -2; }
    guarded(world, |world| world.activate(id, Vector::from_column_slice(position), orientation,
        Vector::from_column_slice(velocity), Vector::from_column_slice(angular)))
}

#[no_mangle]
pub extern "C" fn tfs_rapier_sync(world: *mut World, rows: *const c_char) -> i32 {
    // Parse/validate before touching World; malformed input does not poison it.
    let parsed = catch_unwind(AssertUnwindSafe(|| {
        let rows: Value = serde_json::from_str(input(rows)).unwrap();
        for row in rows.as_array().unwrap() {
            entity_id(&row["id"]); vec3(&row["position"]); quat(&row["orientation"]);
        }
        rows
    }));
    let Ok(rows) = parsed else { return -2; };
    guarded(world, |world| { world.sync(&rows); 0 })
}

#[no_mangle]
pub extern "C" fn tfs_rapier_step(world: *mut World, dt: f64, output: *mut c_char, capacity: usize) -> i32 {
    if !dt.is_finite() || dt <= 0. || output.is_null() || capacity == 0 { return -2; }
    guarded(world, |world| {
        let result = world.step(dt).to_string();
        if result.len()+1 > capacity || result.len() > i32::MAX as usize {
            // Physics already advanced. Never allow the caller to retry it.
            world.poisoned = true;
            return -1;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(result.as_ptr() as *const c_char, output, result.len());
            *output.add(result.len()) = 0;
        }
        result.len() as i32
    })
}

#[no_mangle]
pub extern "C" fn tfs_rapier_deactivate(world: *mut World, id: u32) -> i32 {
    guarded(world, |world| world.deactivate(id))
}

#[no_mangle]
pub extern "C" fn tfs_rapier_destroy(world: *mut World, key: *const c_char) {
    guarded(world, |world| { world.destroy(input(key)); 0 });
}

#[no_mangle]
pub extern "C" fn tfs_rapier_close(world: *mut World) {
    if !world.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| drop(unsafe { Box::from_raw(world) })));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    #[test]
    fn static_rays_skip_vehicles_and_follow_destroyed_geometry() {
        let mut w=world();
        let floor=w.colliders.insert(ColliderBuilder::cuboid(5.,0.1,5.)
            .translation(Vector::new(0.,-0.1,0.)).build());
        w.collider_entities.insert(floor,json!({"kind":"breakable","body":17}));
        w.breakables.insert("bridge".into(),vec![floor]);
        let mut output=[StaticRayHit { body:-1,fraction:1.,point:[0.;3],normal:[0.;3] };2];
        let rays=[[0.,10.,0.,0.,-10.,0.],[10.,1.,10.,10.,-1.,10.]];
        assert_eq!(tfs_rapier_static_rays(&mut w,rays.as_ptr(),2,output.as_mut_ptr()),0);
        assert_eq!(output[0].body,17);
        assert!((output[0].fraction-0.5).abs()<1e-12);
        assert!(output[0].normal[1]>0.99);
        assert_eq!(output[1].body,-1);
        w.activate(200,Vector::new(0.,5.,0.),&[0.,0.,0.,1.],Vector::zeros(),Vector::zeros());
        w.step(1./240.);
        w.static_rays(&rays,&mut output);
        assert_eq!(output[0].body,17);
        w.destroy("bridge");
        w.static_rays(&rays,&mut output);
        assert_eq!(output[0].body,-1);
        let bad=[[0.,0.,f64::NAN,0.,0.,0.]];
        assert_eq!(tfs_rapier_static_rays(&mut w,bad.as_ptr(),1,output.as_mut_ptr()),-2);
        assert!(!w.poisoned);
        assert_eq!(tfs_rapier_ray_hit_size(),64);
    }

    #[test]
    fn vertex_cloud_prism_has_a_solid_bottom_on_a_small_support_patch() {
        // MeshFiles.mesh(vertices) emits a fan only to reference every vertex.
        // In this fan the top-left half of the bottom face is absent. A
        // trimesh therefore sinks onto an interior diagonal, unlike the hull.
        let path = std::env::temp_dir().join(format!("tfs-prism-{}.obj", std::process::id()));
        fs::write(&path, "v -1 0 -1\nv -1 0 1\nv -1 1 1\nv -1 1 -1\nv 1 0 -1\nv 1 0 1\nv 1 1 1\nv 1 1 -1\nf 1 2 3\nf 1 3 4\nf 1 4 5\nf 1 5 6\nf 1 6 7\nf 1 7 8\n").unwrap();
        let mut w = World::new(&json!({"static": [], "entities": [{
            "id":200,"initial_position":[0.,2.,0.],"initial_orientation":[0.,0.,0.,1.],
            "mass":1000.,"center_of_mass":[0.,0.5,0.],"inertia":[1000.,1000.,1000.],
            "parts":[{"kind":"prism","path":path,"offset":[0.,0.,0.]}]
        }]}));
        fs::remove_file(path).unwrap();
        let support = w.colliders.insert(ColliderBuilder::trimesh(
            vec![Point::new(-0.8,0.,0.4),Point::new(-0.4,0.,0.8),Point::new(-0.4,0.,0.4)],
            vec![[0,1,2]]).unwrap().build());
        w.collider_entities.insert(support,json!({"kind":"terrain","body":17}));
        w.activate(200,Vector::new(0.,2.,0.),&[0.,0.,0.,1.],Vector::zeros(),Vector::zeros());
        w.bodies[w.dynamic[&200]].lock_rotations(true,true);
        for _ in 0..480 { w.step(1./240.); }
        let body=&w.bodies[w.dynamic[&200]];
        assert!(body.translation().y > -0.02,"prism sank to {}",body.translation().y);
        assert!(body.linvel().norm() < 0.05);
    }

    #[test]
    fn fast_body_cannot_cross_thin_terrain_in_one_substep() {
        let mut w=world();
        let floor=w.colliders.insert(ColliderBuilder::trimesh(
            vec![Point::new(-20.,0.,-20.),Point::new(20.,0.,20.),Point::new(20.,0.,-20.)],
            vec![[0,1,2]]).unwrap().build());
        w.collider_entities.insert(floor,json!({"kind":"terrain","body":17}));
        w.activate(200,Vector::new(0.,2.,-2.),&[0.,0.,0.,1.],Vector::new(0.,-1000.,0.),Vector::zeros());
        w.step(1./240.);
        let body=&w.bodies[w.dynamic[&200]];
        assert!(body.translation().y >= 0.95,"tunneled to {}",body.translation().y);
    }

    fn world() -> World {
        World::new(&json!({"static": [], "entities": [{
            "id": 200, "initial_position": [0.,5.,0.],
            "initial_orientation": [0.,0.,0.,1.], "mass": 10.,
            "center_of_mass": [0.,0.,0.], "inertia": [1.,1.,1.],
            "parts": [{"kind": "box", "offset": [0.,0.,0.], "half_extents": [1.,1.,1.]}]
        }]}))
    }

    #[test]
    fn ffi_invalid_input_and_panic_do_not_escape() {
        assert!(tfs_rapier_create(std::ptr::null()).is_null());
        let mut w = world();
        let bad = CString::new("[]oops").unwrap();
        assert_eq!(tfs_rapier_sync(&mut w, bad.as_ptr()), -2);
        assert!(!w.poisoned);
        assert_eq!(guarded(&mut w, |_| panic!("test failure")), -3);
        assert!(w.poisoned);
        assert_eq!(tfs_rapier_deactivate(&mut w, 200), -3);
    }

    #[test]
    fn deactivation_removes_metadata_and_reactivation_does_not_leak() {
        let mut w = world();
        let baseline = w.collider_entities.len();
        for _ in 0..8 {
            assert_eq!(w.activate(200, Vector::new(0.,5.,0.), &[0.,0.,0.,1.], Vector::zeros(), Vector::zeros()), 0);
            assert_eq!(w.collider_entities.len(), baseline+1);
            assert_eq!(w.deactivate(200), 0);
            assert_eq!(w.collider_entities.len(), baseline);
        }
    }

    #[test]
    fn invalid_sync_is_atomic_and_output_overflow_prevents_retry() {
        let mut w = world();
        let handle = w.sensors[&200];
        let before = *w.bodies[handle].position();
        let bad = CString::new(r#"[{"id":200,"position":[9,0,0],"orientation":[0,0,0,1]},{"id":200,"position":[0,0],"orientation":[0,0,0,1]}]"#).unwrap();
        assert_eq!(tfs_rapier_sync(&mut w, bad.as_ptr()), -2);
        assert_eq!(*w.bodies[handle].position(), before);
        let mut output = [0i8;1];
        assert_eq!(tfs_rapier_step(&mut w, 1./60., output.as_mut_ptr(), 1), -1);
        assert_eq!(tfs_rapier_step(&mut w, 1./60., output.as_mut_ptr(), 1), -3);
    }
    #[test]
    fn contact_metadata_points_and_units_follow_the_same_manifold_point() {
        let mut w = world();
        let floor = w.colliders.insert(ColliderBuilder::cuboid(20., 1., 20.)
            .translation(Vector::new(0.,-1.,0.)).build());
        w.collider_entities.insert(floor, json!({"kind":"map","body":17,"id":null}));
        // A falling body's instantaneous reaction need not equal its weight.
        // Start supported and let contact regularization reach equilibrium.
        w.activate(200, Vector::new(0.,1.,0.), &[0.,0.,0.,1.], Vector::zeros(), Vector::zeros());
        let dt = 1./60.;
        let mut result = Value::Null;
        for _ in 0..60 { result = w.step(dt); }
        let contacts = result["contacts"].as_array().unwrap();
        assert!(!contacts.is_empty());
        let mut impulse = 0.;
        for row in contacts {
            assert_eq!(row["other"]["body"],17);
            assert_eq!(row["entity"],200);
            assert!(vec3(&row["normal"]).y > 0.9);
            let a = vec3(&row["point_a"]); let b = vec3(&row["point_b"]);
            assert!((vec3(&row["point"])-(a+b)*0.5).norm() < 1e-12);
            let value = row["impulse"].as_f64().unwrap();
            assert!((row["normal_force"].as_f64().unwrap()*dt-value).abs() < 1e-12);
            impulse += value;
        }
        // Rapier 0.26.1 total_impulse includes the initial warm-start
        // accumulator as well as substep solves. A supported body's reported
        // impulse/dt therefore need not equal mg (observed 1.25*mg here).
        // Validate units, sign and resting behavior instead of inventing a
        // correction factor or asserting physical momentum equivalence.
        assert!(impulse.is_finite() && impulse > 0.);
        let body = &w.bodies[w.dynamic[&200]];
        assert!(body.linvel().norm() < 1e-3);
        assert!((body.translation().y-1.).abs() < 0.01);
    }

    #[test]
    fn launch_mass_properties_update_is_validated_and_preserved() {
        let mut w = world();
        let properties = [20., 1., 2., 3., 4., 5., 6.];
        assert_eq!(tfs_rapier_set_mass(&mut w, 200, properties.as_ptr()), 0);
        let before = w.definitions[&200].mass;
        let invalid = [20., 1., 2., 3., -1., 5., 6.];
        assert_eq!(tfs_rapier_set_mass(&mut w, 200, invalid.as_ptr()), -2);
        assert_eq!(w.definitions[&200].mass, before);
        assert_eq!(w.activate(200, Vector::zeros(), &[0.,0.,0.,1.], Vector::zeros(), Vector::zeros()), 0);
        let body = &w.bodies[w.dynamic[&200]];
        assert!((body.mass()-20.).abs() < 1e-12);
        assert!((body.center_of_mass().coords-Vector::new(1.,2.,3.)).norm() < 1e-12);
        assert_eq!(tfs_rapier_set_mass(&mut w, 200, properties.as_ptr()), 1);
        assert_eq!(w.deactivate(200), 0);
        assert_eq!(tfs_rapier_set_mass(&mut w, 200, properties.as_ptr()), 0);
    }

}
