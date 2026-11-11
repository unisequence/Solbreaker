//! Bullet's public C client API. These calls never enter the Python API.
//!
//! Structure layouts follow SharedMemoryPublic.h from Bullet 3.25 / PyBullet
//! 3.2.7. Bullet is covered by the project's existing third-party notices.
use libloading::Library;
use std::ffi::{c_char, c_void};

pub type Handle = *mut c_void;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Contact {
    pub flags: i32,
    pub body_a: i32,
    pub body_b: i32,
    pub link_a: i32,
    pub link_b: i32,
    pub point_a: [f64; 3],
    pub point_b: [f64; 3],
    pub normal: [f64; 3],
    pub distance: f64,
    pub force: f64,
    pub friction_force_1: f64,
    pub friction_force_2: f64,
    pub friction_direction_1: [f64; 3],
    pub friction_direction_2: [f64; 3],
}

#[repr(C)]
pub struct Contacts {
    pub count: i32,
    pub points: *const Contact,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Overlap {
    pub body: i32,
    pub link: i32,
}

#[repr(C)]
pub struct Overlaps {
    pub count: i32,
    pub objects: *const Overlap,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct RayHit {
    pub fraction: f64,
    pub body: i32,
    pub link: i32,
    pub point: [f64; 3],
    pub normal: [f64; 3],
}

#[repr(C)]
pub struct RayHits {
    pub count: i32,
    pub hits: *const RayHit,
}

macro_rules! bullet_api {
    ($( $field:ident: $symbol:literal ($($argument:ty),*) -> $result:ty; )*) => {
        pub struct Api {
            $(pub $field: unsafe extern "C" fn($($argument),*) -> $result,)*
            _library: Library,
        }

        impl Api {
            pub unsafe fn load(path: &str) -> Result<Self, String> {
                let library = Library::new(path).map_err(|error| error.to_string())?;
                Ok(Self {
                    $($field: *library.get::<unsafe extern "C" fn($($argument),*) -> $result>(
                        concat!($symbol, "\0").as_bytes()).map_err(|error| error.to_string())?,)*
                    _library: library,
                })
            }
        }
    }
}

bullet_api! {
    connect: "b3ConnectPhysicsDirect" () -> Handle;
    disconnect: "b3DisconnectSharedMemory" (Handle) -> ();
    submit: "b3SubmitClientCommandAndWaitStatus" (Handle, Handle) -> Handle;
    status_type: "b3GetStatusType" (Handle) -> i32;
    shape_init: "b3CreateCollisionShapeCommandInit" (Handle) -> Handle;
    mesh: "b3CreateCollisionShapeAddMesh" (Handle, *const c_char, *const f64) -> i32;
    box_shape: "b3CreateCollisionShapeAddBox" (Handle, *const f64) -> i32;
    cylinder: "b3CreateCollisionShapeAddCylinder" (Handle, f64, f64) -> i32;
    shape_flags: "b3CreateCollisionSetFlag" (Handle, i32, i32) -> ();
    shape_transform: "b3CreateCollisionShapeSetChildTransform" (Handle, i32, *const f64, *const f64) -> ();
    shape_id: "b3GetStatusCollisionShapeUniqueId" (Handle) -> i32;
    body_init: "b3CreateMultiBodyCommandInit" (Handle) -> Handle;
    body_base: "b3CreateMultiBodyBase" (Handle, f64, i32, i32, *const f64, *const f64, *const f64, *const f64) -> i32;
    body_link: "b3CreateMultiBodyLink" (Handle, f64, f64, f64, *const f64, *const f64, *const f64, *const f64, i32, i32, *const f64) -> i32;
    body_id: "b3GetStatusBodyIndex" (Handle) -> i32;
    pose_init: "b3CreatePoseCommandInit" (Handle, i32) -> Handle;
    position: "b3CreatePoseCommandSetBasePosition" (Handle, f64, f64, f64) -> i32;
    orientation: "b3CreatePoseCommandSetBaseOrientation" (Handle, f64, f64, f64, f64) -> i32;
    joint_position: "b3CreatePoseCommandSetJointPosition" (Handle, Handle, i32, f64) -> i32;
    filter_init: "b3CollisionFilterCommandInit" (Handle) -> Handle;
    filter_mask: "b3SetCollisionFilterGroupMask" (Handle, i32, i32, i32, i32) -> ();
    dynamics_init: "b3InitChangeDynamicsInfo" (Handle) -> Handle;
    collision_margin: "b3ChangeDynamicsInfoSetCollisionMargin" (Handle, i32, f64) -> i32;
    detect: "b3InitPerformCollisionDetectionCommand" (Handle) -> Handle;
    collision_info: "b3RequestCollisionInfoCommandInit" (Handle, i32) -> Handle;
    aabb: "b3GetStatusAABB" (Handle, i32, *mut f64, *mut f64) -> i32;
    overlap_init: "b3InitAABBOverlapQuery" (Handle, *const f64, *const f64) -> Handle;
    overlaps: "b3GetAABBOverlapResults" (Handle, *mut Overlaps) -> ();
    closest_init: "b3InitClosestDistanceQuery" (Handle) -> Handle;
    closest_a: "b3SetClosestDistanceFilterBodyA" (Handle, i32) -> ();
    closest_b: "b3SetClosestDistanceFilterBodyB" (Handle, i32) -> ();
    closest_distance: "b3SetClosestDistanceThreshold" (Handle, f64) -> ();
    contacts: "b3GetContactPointInformation" (Handle, *mut Contacts) -> ();
    ray_init: "b3CreateRaycastBatchCommandInit" (Handle) -> Handle;
    ray_add: "b3RaycastBatchAddRay" (Handle, *const f64, *const f64) -> ();
    ray_filter: "b3RaycastBatchSetCollisionFilterMask" (Handle, i32) -> ();
    rays: "b3GetRaycastInformation" (Handle, *mut RayHits) -> ();
    remove: "b3InitRemoveBodyCommand" (Handle, i32) -> Handle;
}

pub struct Client {
    pub api: Api,
    pub handle: Handle,
}

impl Client {
    pub fn open(path: &str) -> Result<Self, String> {
        let api = unsafe { Api::load(path)? };
        let handle = unsafe { (api.connect)() };
        if handle.is_null() {
            return Err("Bullet direct connection failed".into());
        }
        Ok(Self { api, handle })
    }

    pub fn execute(&self, command: Handle) -> Result<Handle, String> {
        if command.is_null() {
            return Err("Bullet returned an empty command".into());
        }
        let status = unsafe { (self.api.submit)(self.handle, command) };
        if status.is_null() {
            return Err("Bullet command returned no status".into());
        }
        let kind = unsafe { (self.api.status_type)(status) };
        if matches!(kind, 0 | 1 | 3 | 32 | 40 | 42 | 76 | 79 | 83 | 86) {
            return Err(format!(
                "Bullet query command failed with SDK status {kind}"
            ));
        }
        Ok(status)
    }

    pub fn overlapping(&self, low: [f64; 3], high: [f64; 3]) -> Result<Vec<Overlap>, String> {
        self.execute(unsafe { (self.api.overlap_init)(self.handle, low.as_ptr(), high.as_ptr()) })?;
        let mut output = Overlaps {
            count: 0,
            objects: std::ptr::null(),
        };
        unsafe { (self.api.overlaps)(self.handle, &mut output) };
        copy_results(output.objects, output.count)
    }

    pub fn closest(&self, body_a: i32, body_b: i32, distance: f64) -> Result<Vec<Contact>, String> {
        unsafe {
            let command = (self.api.closest_init)(self.handle);
            (self.api.closest_a)(command, body_a);
            (self.api.closest_b)(command, body_b);
            (self.api.closest_distance)(command, distance);
            self.execute(command)?;
            let mut output = Contacts {
                count: 0,
                points: std::ptr::null(),
            };
            (self.api.contacts)(self.handle, &mut output);
            copy_results(output.points, output.count)
        }
    }

    pub fn cast_rays(&self, rays: &[([f64; 3], [f64; 3])]) -> Result<Vec<RayHit>, String> {
        let mut hits = Vec::with_capacity(rays.len());
        for chunk in rays.chunks(256) {
            unsafe {
                let command = (self.api.ray_init)(self.handle);
                (self.api.ray_filter)(command, 1);
                for (start, end) in chunk {
                    (self.api.ray_add)(command, start.as_ptr(), end.as_ptr());
                }
                self.execute(command)?;
                let mut output = RayHits {
                    count: 0,
                    hits: std::ptr::null(),
                };
                (self.api.rays)(self.handle, &mut output);
                let values = copy_results(output.hits, output.count)?;
                if values.len() != chunk.len() {
                    return Err("Bullet returned an incomplete ray batch".into());
                }
                hits.extend(values);
            }
        }
        Ok(hits)
    }
}

fn copy_results<T: Copy>(pointer: *const T, count: i32) -> Result<Vec<T>, String> {
    if count < 0 || count > 1_000_000 || (count > 0 && pointer.is_null()) {
        return Err("Invalid Bullet query result".into());
    }
    if count == 0 {
        return Ok(Vec::new());
    }
    Ok(unsafe { std::slice::from_raw_parts(pointer, count as usize) }.to_vec())
}

impl Drop for Client {
    fn drop(&mut self) {
        unsafe { (self.api.disconnect)(self.handle) };
    }
}
