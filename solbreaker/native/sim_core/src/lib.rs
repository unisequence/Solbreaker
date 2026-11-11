//! Optional, transactional arithmetic for the existing tracked-ground solver.
//!
//! Bullet remains behind the caller's sampling and validation callbacks. A
//! rejected native attempt never publishes a partially integrated state.

mod drive;
mod math;
mod measured_support;
mod support_slice;

use std::ffi::c_void;
use std::mem::{align_of, size_of};
use std::panic::{catch_unwind, AssertUnwindSafe};

pub const ABI_VERSION: u32 = 1;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Pose {
    pub x: f64,
    pub y: f64,
    pub z: f64,
    pub yaw: f64,
    pub pitch: f64,
    pub roll: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Motion {
    pub velocity: [f64; 3],
    pub suspension: [f64; 3],
    pub angular: [f64; 2],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Terrain {
    pub heights: *const u16,
    pub size: u32,
    pub min_x: f64,
    pub min_z: f64,
    pub width: f64,
    pub depth: f64,
    pub height_min: f64,
    pub height_range: f64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct DriveInput {
    pub abi_version: u32,
    pub count: u32,
    pub pose: Pose,
    pub road: Pose,
    pub motion: Motion,
    pub lean: [f64; 4],
    pub effort: [f64; 3],
    pub distance: f64,
    pub yaw_delta: f64,
    pub dt: f64,
    pub displacement: [f64; 2],
    pub has_displacement: u32,
    pub was_blocked: u32,
    pub has_memory: u32,
    pub hint_count: u32,
    pub hints: [u32; 3],
    pub a: *const f64,
    pub b: *const f64,
    pub p: *const f64,
    pub weight: *const f64,
    pub memory_heights: *const f64,
    pub memory_speeds: *const f64,
    pub constants: [f64; 8],
    pub terrain: Terrain,
}

#[repr(C)]
pub struct DriveOutput {
    pub pose: Pose,
    pub road: Pose,
    pub motion: Motion,
    pub lean: [f64; 4],
    pub moved: u32,
    pub blocked: u32,
    pub carrying_supported: u32,
    pub hint_count: u32,
    pub hints: [u32; 3],
    pub spring_pose: Pose,
    pub bounds_pose: Pose,
    pub spring_velocity: [f64; 3],
    pub spring_lean: [f64; 4],
    pub spring_angular: [f64; 2],
    pub memory_heights: *mut f64,
    pub memory_speeds: *mut f64,
    pub lower_bounds: *mut f64,
    pub reason: u32,
    pub support_queries: u32,
    pub validation_queries: u32,
}

pub type SampleFn = unsafe extern "C" fn(
    *mut c_void,
    u32,
    *const f64,
    *const f64,
    *const Pose,
    *mut f64,
) -> i32;
pub type ValidateFn = unsafe extern "C" fn(*mut c_void, *const Pose, *const Pose) -> i32;
pub type SamplePlanFn = unsafe extern "C" fn(*mut c_void, u32, *const f64, *const f64,
    *const Pose, *const f64, *const f64, *const f64, *const f64, *const u32, u32, *mut f64) -> i32;

struct PlanContext {
    sample: SamplePlanFn,
    validate: ValidateFn,
    original: *mut c_void,
    terrain: Terrain,
    slope: f64,
    reach: f64,
    skin: f64,
}

unsafe extern "C" fn sample_planned(context: *mut c_void, count: u32,
    xs: *const f64, zs: *const f64, reference: *const Pose, output: *mut f64) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let ctx = &*(context as *const PlanContext);
        let terrain = ctx.terrain;
        let heights = std::slice::from_raw_parts(terrain.heights, (terrain.size as usize).pow(2));
        let x = std::slice::from_raw_parts(xs, count as usize);
        let z = std::slice::from_raw_parts(zs, count as usize);
        let plan = plan_ground_rays(terrain, heights, x, z, Some((2, *reference)),
            ctx.slope, ctx.reach, ctx.skin)?;
        Some((ctx.sample)(ctx.original, count, xs, zs, reference, plan.terrain.as_ptr(),
            plan.tops.as_ptr(), plan.starts.as_ptr(), plan.bounds.as_ptr(),
            plan.raised.as_ptr(), plan.raised_count, output))
    }));
    result.ok().flatten().unwrap_or(2)
}

unsafe extern "C" fn validate_planned(context: *mut c_void, candidate: *const Pose,
    previous: *const Pose) -> i32 {
    let ctx = &*(context as *const PlanContext);
    (ctx.validate)(ctx.original, candidate, previous)
}

/// The caller keeps all input buffers alive until the synchronous call returns.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_step_plan(input: *const DriveInput,
    sample: Option<SamplePlanFn>, validate: Option<ValidateFn>, context: *mut c_void,
    max_slope: f64, output: *mut DriveOutput) -> i32 {
    if !aligned(input) || !max_slope.is_finite()
        || !(0.0..=std::f64::consts::FRAC_PI_2).contains(&max_slope)
        || sample.is_none() || validate.is_none() {
        return drive::INVALID_INPUT as i32;
    }
    let value = input.read();
    let mut ctx = PlanContext { sample: sample.unwrap(), validate: validate.unwrap(),
        original: context, terrain: value.terrain, slope: max_slope,
        reach: value.constants[3], skin: value.constants[0] };
    tfs_ground_step(input, Some(sample_planned), Some(validate_planned),
        (&mut ctx as *mut PlanContext).cast(), output)
}

/// Optional checkpoint ABI. Status 11 publishes the first-translation
/// arithmetic in output: pose=validated rotation, road/bounds=second target,
/// spring fields=second candidate, hints and lower bounds. Memory-height and
/// memory-speed buffers remain untouched. It does NOT admit the translation.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_step_plan_prefix_v1(input: *const DriveInput,
    sample: Option<SamplePlanFn>, validate: Option<ValidateFn>, context: *mut c_void,
    max_slope: f64, output: *mut DriveOutput) -> i32 {
    if !aligned(input) || !max_slope.is_finite()
        || !(0.0..=std::f64::consts::FRAC_PI_2).contains(&max_slope)
        || sample.is_none() || validate.is_none() {
        return drive::INVALID_INPUT as i32;
    }
    let value = input.read();
    let mut ctx = PlanContext { sample: sample.unwrap(), validate: validate.unwrap(),
        original: context, terrain: value.terrain, slope: max_slope,
        reach: value.constants[3], skin: value.constants[0] };
    ground_step_impl(input, Some(sample_planned), Some(validate_planned),
        (&mut ctx as *mut PlanContext).cast(), output, 1)
}

/// Optional v2 also publishes a rejected first-rotation checkpoint.
/// Status 11 with support_queries=validation_queries=1 means pose is the
/// REJECTED rotation, road/bounds are its support and spring fields its pose.
/// The caller must restore its captured rejection contacts and run the normal
/// rotation-rejection branch. V1 and the original entry points are unchanged.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_step_plan_prefix_v2(input: *const DriveInput,
    sample: Option<SamplePlanFn>, validate: Option<ValidateFn>, context: *mut c_void,
    max_slope: f64, output: *mut DriveOutput) -> i32 {
    if !aligned(input) || !max_slope.is_finite()
        || !(0.0..=std::f64::consts::FRAC_PI_2).contains(&max_slope)
        || sample.is_none() || validate.is_none() {
        return drive::INVALID_INPUT as i32;
    }
    let value = input.read();
    let mut ctx = PlanContext { sample: sample.unwrap(), validate: validate.unwrap(),
        original: context, terrain: value.terrain, slope: max_slope,
        reach: value.constants[3], skin: value.constants[0] };
    ground_step_impl(input, Some(sample_planned), Some(validate_planned),
        (&mut ctx as *mut PlanContext).cast(), output, 2)
}


#[no_mangle]
pub extern "C" fn tfs_sim_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn tfs_sim_input_size() -> u32 {
    size_of::<DriveInput>() as u32
}

#[no_mangle]
pub extern "C" fn tfs_sim_output_size() -> u32 {
    size_of::<DriveOutput>() as u32
}

pub(crate) fn aligned<T>(ptr: *const T) -> bool {
    !ptr.is_null() && (ptr as usize) % align_of::<T>() == 0
}

struct GroundPlan {
    terrain: Vec<f64>,
    tops: Vec<f64>,
    starts: Vec<f64>,
    bounds: [f64; 6],
    raised: Vec<u32>,
    raised_count: u32,
}

fn plan_ground_rays(
    terrain: Terrain,
    heights: &[u16],
    xs: &[f64],
    zs: &[f64],
    reference: Option<(u32, Pose)>,
    max_slope: f64,
    support_reach: f64,
    skin: f64,
) -> Option<GroundPlan> {
    let mut sampled = Vec::with_capacity(xs.len());
    let mut tops = Vec::with_capacity(xs.len());
    let mut starts = Vec::with_capacity(xs.len());
    let mut raised = Vec::with_capacity(xs.len());
    let normal = reference.filter(|(kind, _)| *kind == 2).map(|(_, pose)| drive::up(pose));
    let tangent = max_slope.tan();
    let mut min_x = f64::INFINITY;
    let mut min_z = f64::INFINITY;
    let mut min_terrain = f64::INFINITY;
    let mut max_x = f64::NEG_INFINITY;
    let mut max_z = f64::NEG_INFINITY;
    let mut max_start = f64::NEG_INFINITY;
    for (index, (&x, &z)) in xs.iter().zip(zs).enumerate() {
        if !x.is_finite() || !z.is_finite() {
            return None;
        }
        let ground = drive::terrain_height(terrain, heights, x, z);
        let top = reference.map_or(ground, |(_, pose)| ground.max(pose.y)) + support_reach;
        let start = if let (Some((_, pose)), Some(up)) = (reference, normal) {
            let dx = x - pose.x;
            let dz = z - pose.z;
            let rise = -(up[0] * dx + up[2] * dz) / up[1].max(0.05);
            let limit = dx.hypot(dz) * tangent - support_reach;
            if ![dx, dz, rise, limit].iter().all(|value| value.is_finite()) {
                return None;
            }
            let plane = pose.y + rise.max(0.0).min(limit.max(0.0));
            if !plane.is_finite() {
                return None;
            }
            ground.max(plane) + support_reach
        } else {
            top
        };
        if ![ground, top, start].iter().all(|value| value.is_finite()) {
            return None;
        }
        if normal.is_some() && start > top {
            raised.push(index as u32);
        }
        sampled.push(ground);
        tops.push(top);
        starts.push(start);
        min_x = min_x.min(x);
        min_z = min_z.min(z);
        min_terrain = min_terrain.min(ground);
        max_x = max_x.max(x);
        max_z = max_z.max(z);
        max_start = max_start.max(top.max(start));
    }
    let bounds = [min_x - skin, min_terrain - 0.4 - skin, min_z - skin,
                  max_x + skin, max_start + skin, max_z + skin];
    if !bounds.iter().all(|value| value.is_finite()) {
        return None;
    }
    let raised_count = raised.len() as u32;
    raised.resize(xs.len(), 0);
    Some(GroundPlan { terrain: sampled, tops, starts, bounds, raised, raised_count })
}

/// Build the existing terrain/support broadphase and ray inputs, without
/// querying Bullet. All output arrays are caller-owned; a decline or caught
/// panic leaves them unchanged.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_plan(
    terrain: *const Terrain,
    count: u32,
    xs: *const f64,
    zs: *const f64,
    ref_kind: u32,
    reference: *const Pose,
    max_slope: f64,
    support_reach: f64,
    skin: f64,
    terrain_out: *mut f64,
    tops_out: *mut f64,
    starts_out: *mut f64,
    bounds_out: *mut f64,
    raised_out: *mut u32,
    raised_count: *mut u32,
) -> i32 {
    let result = catch_unwind(|| {
        if !(1..=256).contains(&count) || ref_kind > 2
            || !aligned(terrain) || !aligned(xs) || !aligned(zs)
            || !aligned(terrain_out) || !aligned(tops_out) || !aligned(starts_out)
            || !aligned(bounds_out) || !aligned(raised_out) || !aligned(raised_count)
            || (ref_kind != 0 && !aligned(reference))
            || !max_slope.is_finite() || !(0.0..=std::f64::consts::FRAC_PI_2).contains(&max_slope)
            || !support_reach.is_finite() || support_reach < 0.0
            || !skin.is_finite() || skin < 0.0
        {
            return None;
        }
        let terrain = terrain.read();
        if !(2..=16384).contains(&terrain.size) || !aligned(terrain.heights)
            || ![terrain.min_x, terrain.min_z, terrain.width, terrain.depth,
                 terrain.height_min, terrain.height_range].iter().all(|value| value.is_finite())
            || terrain.width <= 0.0 || terrain.depth <= 0.0
        {
            return None;
        }
        let reference = if ref_kind == 0 { None } else {
            let pose = reference.read();
            if ![pose.x, pose.y, pose.z, pose.yaw, pose.pitch, pose.roll]
                .iter().all(|value| value.is_finite())
            {
                return None;
            }
            Some((ref_kind, pose))
        };
        let heights = std::slice::from_raw_parts(terrain.heights, (terrain.size as usize).pow(2));
        let xs = std::slice::from_raw_parts(xs, count as usize);
        let zs = std::slice::from_raw_parts(zs, count as usize);
        plan_ground_rays(terrain, heights, xs, zs, reference, max_slope, support_reach, skin)
    });
    match result {
        Ok(Some(plan)) => {
            // All arithmetic and allocations finish before any publication.
            std::ptr::copy_nonoverlapping(plan.terrain.as_ptr(), terrain_out, count as usize);
            std::ptr::copy_nonoverlapping(plan.tops.as_ptr(), tops_out, count as usize);
            std::ptr::copy_nonoverlapping(plan.starts.as_ptr(), starts_out, count as usize);
            std::ptr::copy_nonoverlapping(plan.bounds.as_ptr(), bounds_out, 6);
            std::ptr::copy_nonoverlapping(plan.raised.as_ptr(), raised_out, count as usize);
            raised_count.write(plan.raised_count);
            0
        }
        Ok(None) => 1,
        Err(_) => 2,
    }
}

/// Pure support arithmetic for callers that retain Python's control flow.
/// All buffers belong to the caller and must contain the documented number
/// of elements. Declines and caught panics leave every output untouched.
#[no_mangle]
pub unsafe extern "C" fn tfs_support_plane(
    rows: u32,
    phi_flat: *const f64,
    target: *const f64,
    weight: *const f64,
    lower: *const f64,
    hint_len: u32,
    hint: *const u32,
    plane_out: *mut f64,
    active_out: *mut u32,
    active_len: *mut u32,
) -> i32 {
    let result = catch_unwind(|| {
        if !(1..=512).contains(&rows) || hint_len > 3
            || !aligned(phi_flat) || !aligned(target) || !aligned(weight)
            || !aligned(lower) || !aligned(plane_out) || !aligned(active_out)
            || !aligned(active_len) || (hint_len != 0 && !aligned(hint))
        {
            return None;
        }
        let count = rows as usize;
        let phi = std::slice::from_raw_parts(phi_flat.cast::<[f64; 3]>(), count);
        let target = std::slice::from_raw_parts(target, count);
        let weight = std::slice::from_raw_parts(weight, count);
        let lower = std::slice::from_raw_parts(lower, count);
        let mut hints = [0_usize; 3];
        if hint_len != 0 {
            let supplied = std::slice::from_raw_parts(hint, hint_len as usize);
            for (index, &value) in supplied.iter().enumerate() {
                if value >= rows {
                    return None;
                }
                hints[index] = value as usize;
            }
        }
        let fit = math::fit_plane(phi, target, weight, lower, &hints[..hint_len as usize])?;
        if !fit.plane.iter().all(|value| value.is_finite())
            || fit.active.len() > 3 || fit.active.iter().any(|&index| index >= count)
        {
            return None;
        }
        let mut active = [0_u32; 3];
        for (index, &value) in fit.active.iter().enumerate() {
            active[index] = value as u32;
        }
        Some((fit.plane, active, fit.active.len() as u32))
    });
    match result {
        Ok(Some((plane, active, length))) => {
            // There are no fallible operations after publishing starts.
            std::ptr::copy_nonoverlapping(plane.as_ptr(), plane_out, 3);
            std::ptr::copy_nonoverlapping(active.as_ptr(), active_out, 3);
            active_len.write(length);
            0
        }
        Ok(None) => 1,
        Err(_) => 2,
    }
}

/// The caller owns every input/output allocation and must keep it valid for
/// the duration of this call, including callbacks. Like any pointer-based C
/// ABI, allocation validity cannot be discovered from a raw address.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_step(
    input: *const DriveInput,
    sample: Option<SampleFn>,
    validate: Option<ValidateFn>,
    context: *mut c_void,
    output: *mut DriveOutput,
) -> i32 {
    ground_step_impl(input, sample, validate, context, output, 0)
}

unsafe fn ground_step_impl(input: *const DriveInput, sample: Option<SampleFn>,
    validate: Option<ValidateFn>, context: *mut c_void, output: *mut DriveOutput,
    capture_prefix: u32) -> i32 {
    if !aligned(output) {
        return drive::INVALID_INPUT as i32;
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        if !aligned(input) {
            return Err(drive::Failure::new(drive::INVALID_INPUT));
        }
        let input = input.read();
        let memory_heights = std::ptr::addr_of!((*output).memory_heights).read();
        let memory_speeds = std::ptr::addr_of!((*output).memory_speeds).read();
        let lower_bounds = std::ptr::addr_of!((*output).lower_bounds).read();
        if !aligned(memory_heights)
            || !aligned(memory_speeds)
            || !aligned(lower_bounds)
        {
            return Err(drive::Failure::new(drive::INVALID_INPUT));
        }
        let Some(sample) = sample else {
            return Err(drive::Failure::new(drive::INVALID_INPUT));
        };
        let Some(validate) = validate else {
            return Err(drive::Failure::new(drive::INVALID_INPUT));
        };
        let result = drive::run(input, sample, validate, context, capture_prefix)?;
        // Integration uses local copies throughout. Publish only after all
        // samples, validations, fit checks and release checks have succeeded.
        std::ptr::copy_nonoverlapping(
            result.memory_heights.as_ptr(),
            memory_heights,
            result.memory_heights.len(),
        );
        std::ptr::copy_nonoverlapping(
            result.memory_speeds.as_ptr(),
            memory_speeds,
            result.memory_speeds.len(),
        );
        std::ptr::copy_nonoverlapping(
            result.lower_bounds.as_ptr(),
            lower_bounds,
            result.lower_bounds.len(),
        );
        output.write(DriveOutput {
            pose: result.pose,
            road: result.road,
            motion: result.motion,
            lean: result.lean,
            moved: result.moved as u32,
            blocked: result.blocked as u32,
            carrying_supported: 1,
            hint_count: result.hints.len() as u32,
            hints: std::array::from_fn(|i| result.hints.get(i).copied().unwrap_or(0) as u32),
            spring_pose: result.spring.pose,
            bounds_pose: result.bounds_pose,
            spring_velocity: result.spring.velocity,
            spring_lean: result.spring.lean,
            spring_angular: result.spring.angular,
            memory_heights,
            memory_speeds,
            lower_bounds,
            reason: 0,
            support_queries: result.support_queries,
            validation_queries: result.validation_queries,
        });
        Ok(())
    }));
    let failure = match result {
        Ok(Ok(())) => return 0,
        Ok(Err(failure)) => failure,
        Err(_) => drive::Failure::new(drive::PANIC),
    };
    if let Some(prefix) = failure.prefix {
        // This path is reachable only through the opt-in checkpoint ABI.
        let lower = std::ptr::addr_of!((*output).lower_bounds).read();
        std::ptr::copy_nonoverlapping(prefix.lower_bounds.as_ptr(), lower, prefix.lower_bounds.len());
        std::ptr::addr_of_mut!((*output).pose).write(prefix.rotated);
        std::ptr::addr_of_mut!((*output).road).write(prefix.road);
        std::ptr::addr_of_mut!((*output).bounds_pose).write(prefix.road);
        std::ptr::addr_of_mut!((*output).spring_pose).write(prefix.spring.pose);
        std::ptr::addr_of_mut!((*output).spring_velocity).write(prefix.spring.velocity);
        std::ptr::addr_of_mut!((*output).spring_lean).write(prefix.spring.lean);
        std::ptr::addr_of_mut!((*output).spring_angular).write(prefix.spring.angular);
        std::ptr::addr_of_mut!((*output).hint_count).write(prefix.hints.len() as u32);
        std::ptr::addr_of_mut!((*output).hints).write(std::array::from_fn(
            |i| prefix.hints.get(i).copied().unwrap_or(0) as u32));
        std::ptr::addr_of_mut!((*output).reason).write(failure.reason);
        std::ptr::addr_of_mut!((*output).support_queries).write(failure.support_queries);
        std::ptr::addr_of_mut!((*output).validation_queries).write(failure.validation_queries);
        return 11;
    }
    std::ptr::addr_of_mut!((*output).reason).write(failure.reason);
    std::ptr::addr_of_mut!((*output).support_queries).write(failure.support_queries);
    std::ptr::addr_of_mut!((*output).validation_queries).write(failure.validation_queries);
    failure.reason as i32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, PartialEq)]
    struct PlanOutput<const N: usize> {
        status: i32,
        terrain: [f64; N],
        tops: [f64; N],
        starts: [f64; N],
        bounds: [f64; 6],
        raised: [u32; N],
        raised_count: u32,
    }

    fn ground_plan<const N: usize>(terrain: &Terrain, count: u32, xs: &[f64; N],
        zs: &[f64; N], kind: u32, reference: *const Pose, slope: f64, reach: f64, skin: f64)
        -> PlanOutput<N>
    {
        let mut result = PlanOutput {
            status: -1, terrain: [42.0; N], tops: [43.0; N], starts: [44.0; N],
            bounds: [45.0; 6], raised: [46; N], raised_count: 47,
        };
        result.status = unsafe {
            tfs_ground_plan(terrain, count, xs.as_ptr(), zs.as_ptr(), kind, reference,
                slope, reach, skin, result.terrain.as_mut_ptr(), result.tops.as_mut_ptr(),
                result.starts.as_mut_ptr(), result.bounds.as_mut_ptr(), result.raised.as_mut_ptr(),
                &mut result.raised_count)
        };
        result
    }

    fn test_terrain(heights: &[u16; 4]) -> Terrain {
        Terrain {
            heights: heights.as_ptr(), size: 2, min_x: 0.0, min_z: 0.0,
            width: 2.0, depth: 2.0, height_min: -2.0, height_range: 8.0,
        }
    }

    #[test]
    fn ground_plan_abi_bilinear_samples_and_unreferenced_bounds() {
        let heights = [0, 65535, 32768, 16384];
        let terrain = test_terrain(&heights);
        let result = ground_plan(&terrain, 3, &[0.0, 0.5, 1.0], &[0.0, 0.5, 1.0],
                                 0, std::ptr::null(), 0.5, 0.28, 0.008);
        assert_eq!(result.status, 0);
        let middle = -2.0 + ((0.0 * 0.5 + 65535.0 * 0.5) * 0.5
            + (32768.0 * 0.5 + 16384.0 * 0.5) * 0.5) * 8.0 / 65535.0;
        assert_eq!(result.terrain, [-2.0, middle, -2.0 + 16384.0 * 8.0 / 65535.0]);
        assert_eq!(result.tops, result.terrain.map(|height| height + 0.28));
        assert_eq!(result.starts, result.tops);
        assert_eq!(result.bounds, [-0.008, -2.0 - 0.4 - 0.008, -0.008,
                                  1.008, middle + 0.28 + 0.008, 1.008]);
        assert_eq!(result.raised_count, 0);
        assert_eq!(result.raised, [0; 3]);
    }

    #[test]
    fn ground_plan_abi_scalar_reference_only_raises_origin_rays() {
        let heights = [0; 4];
        let terrain = Terrain { height_min: 0.0, ..test_terrain(&heights) };
        let reference = Pose { y: 10.0, pitch: -0.8, ..Pose::default() };
        let result = ground_plan(&terrain, 2, &[0.0, 1.0], &[0.0, 1.0],
                                 1, &reference, 0.5, 0.28, 0.008);
        assert_eq!(result.status, 0);
        assert_eq!(result.terrain, [0.0; 2]);
        assert_eq!(result.tops, [10.28; 2]);
        assert_eq!(result.starts, result.tops);
        assert_eq!(result.raised_count, 0);
        assert_eq!(result.bounds[4], 10.28 + 0.008);
    }

    #[test]
    fn ground_plan_abi_caps_carrying_plane_and_lists_only_raised_samples() {
        let heights = [0; 4];
        let terrain = Terrain { height_min: 0.0, ..test_terrain(&heights) };
        let reference = Pose { y: 1.0, pitch: -2.0_f64.atan(), ..Pose::default() };
        let result = ground_plan(&terrain, 4, &[0.0; 4], &[-2.0, 0.0, 0.2, 2.0],
                                 2, &reference, 0.5_f64.atan(), 0.28, 0.008);
        assert_eq!(result.status, 0);
        assert_eq!(result.tops, [1.28; 4]);
        assert_eq!(&result.starts[..3], &[1.28; 3]);
        // The true plane rises by 4m at the last station; reach is capped
        // to 2m * tan(max_slope), including the existing ray headroom.
        assert!((result.starts[3] - 2.0).abs() < 1e-14);
        assert_eq!(result.raised_count, 1);
        assert_eq!(result.raised, [3, 0, 0, 0]);
        assert_eq!(result.bounds[0], -0.008);
        assert_eq!(result.bounds[1], -0.4 - 0.008);
        assert_eq!(result.bounds[2], -2.0 - 0.008);
        assert!((result.bounds[4] - (2.0 + 0.008)).abs() < 1e-14);
        assert_eq!(result.bounds[5], 2.0 + 0.008);

        let covered = Terrain { height_min: 5.0, ..terrain };
        let result = ground_plan(&covered, 4, &[0.0; 4], &[-2.0, 0.0, 0.2, 2.0],
                                 2, &reference, 0.5_f64.atan(), 0.28, 0.008);
        assert_eq!(result.status, 0);
        assert_eq!(result.starts, [5.28; 4]);
        assert_eq!(result.raised_count, 0);
    }

    #[test]
    fn ground_plan_abi_declines_invalid_inputs_without_publishing_outputs() {
        let heights = [0; 4];
        let terrain = test_terrain(&heights);
        let pose = Pose::default();
        let invalid = PlanOutput {
            status: 1, terrain: [42.0], tops: [43.0], starts: [44.0],
            bounds: [45.0; 6], raised: [46], raised_count: 47,
        };
        for (count, x, kind, reference, slope, reach, skin) in [
            (0, 0.0, 0, std::ptr::null(), 0.5, 0.28, 0.008),
            (257, 0.0, 0, std::ptr::null(), 0.5, 0.28, 0.008),
            (1, f64::NAN, 0, std::ptr::null(), 0.5, 0.28, 0.008),
            (1, 0.0, 3, &pose as *const Pose, 0.5, 0.28, 0.008),
            (1, 0.0, 1, std::ptr::null(), 0.5, 0.28, 0.008),
            (1, 0.0, 2, std::ptr::null(), 0.5, 0.28, 0.008),
            (1, 0.0, 0, std::ptr::null(), f64::INFINITY, 0.28, 0.008),
            (1, 0.0, 0, std::ptr::null(), -0.1, 0.28, 0.008),
            (1, 0.0, 0, std::ptr::null(), 2.0, 0.28, 0.008),
            (1, 0.0, 0, std::ptr::null(), 0.5, -0.1, 0.008),
            (1, 0.0, 0, std::ptr::null(), 0.5, 0.28, f64::NAN),
        ] {
            assert_eq!(ground_plan(&terrain, count, &[x], &[0.0], kind, reference, slope, reach, skin), invalid);
        }
        for bad_terrain in [
            Terrain { size: 1, ..terrain }, Terrain { size: 16385, ..terrain },
            Terrain { width: 0.0, ..terrain }, Terrain { depth: -1.0, ..terrain },
            Terrain { height_range: f64::INFINITY, ..terrain },
            Terrain { heights: std::ptr::null(), ..terrain },
        ] {
            assert_eq!(ground_plan(&bad_terrain, 1, &[0.0], &[0.0], 0, std::ptr::null(), 0.5, 0.28, 0.008), invalid);
        }
        let bad_pose = Pose { y: f64::NAN, ..pose };
        assert_eq!(ground_plan(&terrain, 1, &[0.0], &[0.0], 2, &bad_pose, 0.5, 0.28, 0.008), invalid);
    }

    fn support_fit(phi: &[[f64; 3]], target: &[f64], weight: &[f64], lower: &[f64],
                   hint: &[u32]) -> (i32, [f64; 3], [u32; 3], u32) {
        let mut plane = [42.0; 3];
        let mut active = [43; 3];
        let mut active_len = 44;
        let status = unsafe {
            tfs_support_plane(phi.len() as u32, phi.as_ptr().cast(), target.as_ptr(),
                weight.as_ptr(), lower.as_ptr(), hint.len() as u32,
                if hint.is_empty() { std::ptr::null() } else { hint.as_ptr() },
                plane.as_mut_ptr(), active.as_mut_ptr(), &mut active_len)
        };
        (status, plane, active, active_len)
    }

    #[test]
    fn support_plane_abi_solves_analytical_regularized_slope() {
        let phi = [[1.0, -1.0, -2.0], [1.0, -1.0, 2.0],
                   [1.0, 1.0, -2.0], [1.0, 1.0, 2.0]];
        let target = phi.map(|row| 1.25 + 0.125 * row[1] - 0.375 * row[2]);
        let (status, plane, _, active_len) = support_fit(&phi, &target, &[1.0; 4], &[-100.0; 4], &[]);
        assert_eq!(status, 0);
        let expected = [1.25 * 4.0 / (4.0 + 1e-9), 0.125 * 4.0 / (4.0 + 1e-9),
                        -0.375 * 16.0 / (16.0 + 1e-9)];
        for i in 0..3 {
            assert!((plane[i] - expected[i]).abs() < 1e-12);
        }
        assert_eq!(active_len, 0);
    }

    #[test]
    fn support_plane_abi_honours_a_zero_weight_centre_floor() {
        let phi = [[1.0, -1.0, -2.0], [1.0, -1.0, 2.0],
                   [1.0, 1.0, -2.0], [1.0, 1.0, 2.0], [1.0, 0.0, 0.0]];
        let (status, plane, active, active_len) = support_fit(
            &phi, &[1.0, 1.0, 1.0, 1.0, 0.0], &[1.0, 1.0, 1.0, 1.0, 0.0],
            &[-100.0, -100.0, -100.0, -100.0, 3.0], &[4],
        );
        assert_eq!(status, 0);
        assert!((plane[0] - 3.0).abs() < 1e-12);
        assert!(plane[1].abs() < 1e-12 && plane[2].abs() < 1e-12);
        assert_eq!(active_len, 1);
        assert_eq!(active[0], 4);
    }

    #[test]
    fn support_plane_abi_accepts_512_bounded_rows() {
        let corners = [[1.0, -1.0, -2.0], [1.0, -1.0, 2.0],
                       [1.0, 1.0, -2.0], [1.0, 1.0, 2.0]];
        let phi: Vec<_> = (0..512).map(|i| corners[i % 4]).collect();
        let (status, plane, _, _) = support_fit(&phi, &[2.0; 512], &[1.0; 512], &[-100.0; 512], &[]);
        assert_eq!(status, 0);
        assert!((plane[0] - 2.0 * 512.0 / (512.0 + 1e-9)).abs() < 1e-12);
        assert!(plane[1].abs() < 1e-12 && plane[2].abs() < 1e-12);
    }

    #[test]
    fn support_plane_abi_declines_nonfinite_and_bad_fit_without_output_mutation() {
        let phi = [[1.0, -1.0, -2.0], [1.0, -1.0, 2.0],
                   [1.0, 1.0, -2.0], [1.0, 1.0, 2.0]];
        for target in [[f64::NAN, 0.0, 0.0, 0.0], [0.0, f64::INFINITY, 0.0, 0.0]] {
            assert_eq!(support_fit(&phi, &target, &[1.0; 4], &[-100.0; 4], &[]),
                       (1, [42.0; 3], [43; 3], 44));
        }
        assert_eq!(support_fit(&phi, &[0.0; 4], &[-1.0; 4], &[-100.0; 4], &[]),
                   (1, [42.0; 3], [43; 3], 44));
        assert_eq!(support_fit(&phi, &[0.0; 4], &[1.0; 4], &[-100.0; 4], &[4]),
                   (1, [42.0; 3], [43; 3], 44));
    }

    #[test]
    fn support_plane_abi_checks_counts_and_pointer_alignment_before_reading() {
        let phi = [1.0_f64; 3];
        let scalar = [0.0_f64];
        let hint = [0_u32; 4];
        let mut plane = [42.0; 3];
        let mut active = [43; 3];
        let mut length = 44;
        let misaligned = unsafe { phi.as_ptr().cast::<u8>().add(1).cast::<f64>() };
        for (rows, phi_ptr, hint_len, hint_ptr) in [
            (0, phi.as_ptr(), 0, std::ptr::null()),
            (513, phi.as_ptr(), 0, std::ptr::null()),
            (1, std::ptr::null(), 0, std::ptr::null()),
            (1, misaligned, 0, std::ptr::null()),
            (1, phi.as_ptr(), 4, hint.as_ptr()),
            (1, phi.as_ptr(), 1, std::ptr::null()),
        ] {
            let status = unsafe {
                tfs_support_plane(rows, phi_ptr, scalar.as_ptr(), scalar.as_ptr(),
                    scalar.as_ptr(), hint_len, hint_ptr, plane.as_mut_ptr(),
                    active.as_mut_ptr(), &mut length)
            };
            assert_eq!(status, 1);
            assert_eq!(plane, [42.0; 3]);
            assert_eq!(active, [43; 3]);
            assert_eq!(length, 44);
        }
        let status = unsafe {
            tfs_support_plane(1, phi.as_ptr(), scalar.as_ptr(), scalar.as_ptr(),
                scalar.as_ptr(), 0, std::ptr::null(), std::ptr::null_mut(),
                active.as_mut_ptr(), &mut length)
        };
        assert_eq!(status, 1);
        assert_eq!(active, [43; 3]);
        assert_eq!(length, 44);
    }

    struct Callbacks {
        validations: usize,
        reject_at: usize,
    }

    unsafe extern "C" fn flat_sample(
        _context: *mut c_void,
        count: u32,
        _xs: *const f64,
        _zs: *const f64,
        _reference: *const Pose,
        heights: *mut f64,
    ) -> i32 {
        std::slice::from_raw_parts_mut(heights, count as usize).fill(0.0);
        0
    }

    unsafe extern "C" fn flat_plan_sample(_context: *mut c_void, count: u32,
        _xs: *const f64, _zs: *const f64, _reference: *const Pose,
        terrain: *const f64, _tops: *const f64, _starts: *const f64,
        _bounds: *const f64, _raised: *const u32, _raised_count: u32,
        heights: *mut f64) -> i32 {
        std::ptr::copy_nonoverlapping(terrain, heights, count as usize);
        0
    }

    unsafe extern "C" fn validate(
        context: *mut c_void,
        _candidate: *const Pose,
        _previous: *const Pose,
    ) -> i32 {
        let callbacks = &mut *(context as *mut Callbacks);
        callbacks.validations += 1;
        (callbacks.validations == callbacks.reject_at) as i32
    }

    #[test]
    fn a_later_validation_rejection_publishes_no_partial_state() {
        let a = [-1.0, -1.0, 1.0, 1.0];
        let b = [-2.0, 2.0, -2.0, 2.0];
        let p = [0.0; 4];
        let weight = [1.0; 4];
        let heights = [0; 4];
        let input = DriveInput {
            abi_version: ABI_VERSION, count: 4,
            pose: Pose::default(), road: Pose::default(), motion: Motion::default(),
            lean: [0.0; 4], effort: [0.0; 3], distance: 0.09, yaw_delta: 0.0,
            dt: 0.02, displacement: [0.0; 2], has_displacement: 0,
            was_blocked: 0, has_memory: 0, hint_count: 0, hints: [0; 3],
            a: a.as_ptr(), b: b.as_ptr(), p: p.as_ptr(), weight: weight.as_ptr(),
            memory_heights: std::ptr::null(), memory_speeds: std::ptr::null(),
            constants: [0.008, 0.06, 9.81, 0.28, 0.1, 32.0, 12.0, 0.0],
            terrain: Terrain {
                heights: heights.as_ptr(), size: 2, min_x: -100.0, min_z: -100.0,
                width: 200.0, depth: 200.0, height_min: 0.0, height_range: 0.0,
            },
        };
        let mut memory_heights = [42.0; 4];
        let mut memory_speeds = [43.0; 4];
        let mut lower_bounds = [44.0; 5];
        let sentinel = Pose { x: 123.0, ..Pose::default() };
        let mut output = DriveOutput {
            pose: sentinel, road: sentinel, motion: Motion::default(), lean: [0.0; 4],
            moved: 0, blocked: 0, carrying_supported: 0, hint_count: 0, hints: [0; 3],
            spring_pose: sentinel, bounds_pose: sentinel, spring_velocity: [0.0; 3],
            spring_lean: [0.0; 4], spring_angular: [0.0; 2],
            memory_heights: memory_heights.as_mut_ptr(),
            memory_speeds: memory_speeds.as_mut_ptr(), lower_bounds: lower_bounds.as_mut_ptr(),
            reason: 0, support_queries: 0, validation_queries: 0,
        };
        let mut callbacks = Callbacks { validations: 0, reject_at: 4 };
        let status = unsafe {
            tfs_ground_step(&input, Some(flat_sample), Some(validate),
                            &mut callbacks as *mut Callbacks as *mut c_void, &mut output)
        };
        assert_eq!(status, 6);
        assert_eq!(output.pose, sentinel);
        assert_eq!(output.road, sentinel);
        assert_eq!(output.spring_pose, sentinel);
        assert_eq!(memory_heights, [42.0; 4]);
        assert_eq!(memory_speeds, [43.0; 4]);
        assert_eq!(lower_bounds, [44.0; 5]);
        assert_eq!(output.support_queries, 4);
        assert_eq!(output.validation_queries, 4);

        callbacks = Callbacks { validations: 0, reject_at: 4 };
        let status = unsafe {
            tfs_ground_step_plan(&input, Some(flat_plan_sample), Some(validate),
                &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
        };
        assert_eq!(status, 6);
        assert_eq!(output.pose, sentinel);
        assert_eq!(memory_heights, [42.0; 4]);
        assert_eq!(memory_speeds, [43.0; 4]);
        assert_eq!(lower_bounds, [44.0; 5]);
        assert_eq!(output.support_queries, 4);
        assert_eq!(output.validation_queries, 4);

        // The old ABI still declines at the same first translation without
        // publishing arithmetic or touching the caller's buffers.
        callbacks = Callbacks { validations: 0, reject_at: 2 };
        let status = unsafe {
            tfs_ground_step_plan(&input, Some(flat_plan_sample), Some(validate),
                &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
        };
        assert_eq!(status, 6);
        assert_eq!(output.pose, sentinel);
        assert_eq!(lower_bounds, [44.0; 5]);

        // Only the optional ABI publishes a checkpoint, not an accepted move.
        callbacks = Callbacks { validations: 0, reject_at: 2 };
        let status = unsafe {
            tfs_ground_step_plan_prefix_v1(&input, Some(flat_plan_sample), Some(validate),
                &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
        };
        assert_eq!(status, 11);
        assert_eq!(output.reason, 6);
        assert_eq!(output.support_queries, 2);
        assert_eq!(output.validation_queries, 2);
        assert_eq!(output.pose.z, 0.0);
        assert_eq!(output.road.z, 0.045);
        assert_eq!(output.spring_pose.z, 0.045);
        assert_eq!(output.bounds_pose, output.road);
        assert_eq!(memory_heights, [42.0; 4]);
        assert_eq!(memory_speeds, [43.0; 4]);
        assert!(lower_bounds.iter().all(|v| v.is_finite()));
        for reject_at in [1, 4] {
            output.pose=sentinel;
            lower_bounds.fill(44.0);
            callbacks = Callbacks { validations: 0, reject_at };
            let status = unsafe {
                tfs_ground_step_plan_prefix_v1(&input, Some(flat_plan_sample), Some(validate),
                    &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
            };
            assert_eq!(status, 6);
            assert_eq!(output.pose, sentinel);
            assert_eq!(lower_bounds, [44.0; 5]);
        }

        for reject_at in [1, 2] {
            output.pose=sentinel;
            lower_bounds.fill(44.0);
            callbacks = Callbacks { validations: 0, reject_at };
            let status = unsafe {
                tfs_ground_step_plan_prefix_v2(&input, Some(flat_plan_sample), Some(validate),
                    &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
            };
            assert_eq!(status, 11);
            assert_eq!(output.reason, 6);
            assert_eq!(output.support_queries, reject_at as u32);
            assert_eq!(output.validation_queries, reject_at as u32);
            assert_eq!(output.road.z, if reject_at==1 { 0.0 } else { 0.045 });
            assert_eq!(output.spring_pose.z, output.road.z);
            assert_eq!(output.bounds_pose, output.road);
            assert_eq!(memory_heights, [42.0; 4]);
            assert_eq!(memory_speeds, [43.0; 4]);
        }
        output.pose=sentinel;
        lower_bounds.fill(44.0);
        callbacks = Callbacks { validations: 0, reject_at: 4 };
        let status = unsafe {
            tfs_ground_step_plan_prefix_v2(&input, Some(flat_plan_sample), Some(validate),
                &mut callbacks as *mut Callbacks as *mut c_void, 0.5, &mut output)
        };
        assert_eq!(status,6);
        assert_eq!(output.pose,sentinel);
        assert_eq!(lower_bounds,[44.0;5]);

        callbacks = Callbacks { validations: 0, reject_at: usize::MAX };
        let status = unsafe {
            tfs_ground_step(&input, Some(flat_sample), Some(validate),
                            &mut callbacks as *mut Callbacks as *mut c_void, &mut output)
        };
        assert_eq!(status, 0);
        assert!((output.pose.z - input.distance).abs() < 1e-14);
        assert_eq!(output.moved, 1);
        assert_eq!(output.support_queries, 6);
        assert_eq!(output.validation_queries, 6);
        assert!(memory_heights.iter().all(|v| (*v - output.pose.y).abs() < 1e-14));
    }
}
