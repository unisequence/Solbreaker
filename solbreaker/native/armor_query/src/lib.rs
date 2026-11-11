//! Pure narrow-phase armor queries. No world state, integration or damage.
use std::mem::{align_of, size_of};
use std::panic::{catch_unwind, AssertUnwindSafe};

const MAX_PARTS: usize = 64;
const MAX_TRIANGLES: usize = 65_536;
const LIMIT: f64 = 1_000_000.;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct PartInput {
    triangles: *const f64,
    count: u32,
    origin: [f64; 3],
    direction: [f64; 3],
    distance: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Hit {
    part: u32,
    triangle: u32,
    distance: f64,
}

fn aligned<T>(pointer: *const T) -> bool {
    !pointer.is_null() && (pointer as usize) % align_of::<T>() == 0
}

fn bounded(value: f64) -> bool {
    value.is_finite() && value.abs() < LIMIT
}

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    let x0 = a[1] * b[2];
    let x1 = a[2] * b[1];
    let y0 = a[2] * b[0];
    let y1 = a[0] * b[2];
    let z0 = a[0] * b[1];
    let z1 = a[1] * b[0];
    [x0 - x1, y0 - y1, z0 - z1]
}

// Installed NumPy's contiguous three-term einsum reduces two SIMD lanes.
// Python must verify this numerical contract before enabling this kernel.
// Explicit zero additions preserve the reduction's signed-zero behavior.
fn einsum3(a: [f64; 3], b: [f64; 3]) -> f64 {
    let x = a[0] * b[0];
    let y = a[1] * b[1];
    let z = a[2] * b[2];
    let lane0 = z + (x + 0.);
    let lane1 = 0. + (y + 0.);
    0. + (lane0 + lane1)
}

fn matmul3(a: [f64; 3], b: [f64; 3]) -> f64 {
    let x = a[0] * b[0];
    let y = a[1] * b[1];
    let z = a[2] * b[2];
    (x + y) + z
}

fn intersect(vertices: &[f64], part: &PartInput) -> Option<f64> {
    let a = [vertices[0], vertices[1], vertices[2]];
    let e1 = sub([vertices[3], vertices[4], vertices[5]], a);
    let e2 = sub([vertices[6], vertices[7], vertices[8]], a);
    let p = cross(part.direction, e2);
    let det = einsum3(e1, p);
    if det <= 1e-9 {
        return None;
    }
    let inv = 1. / det;
    let delta = sub(part.origin, a);
    let u = einsum3(delta, p) * inv;
    let q = cross(delta, e1);
    let v = matmul3(q, part.direction) * inv;
    let distance = einsum3(e2, q) * inv;
    if u >= -1e-7 && v >= -1e-7 && u + v <= 1.0000001
        && distance > 1e-5 && distance <= part.distance
    {
        Some(distance)
    } else {
        None
    }
}

#[no_mangle]
pub extern "C" fn tfs_armor_query_abi() -> u32 { 1 }

#[no_mangle]
pub extern "C" fn tfs_armor_part_size() -> u32 { size_of::<PartInput>() as u32 }

#[no_mangle]
pub extern "C" fn tfs_armor_hit_size() -> u32 { size_of::<Hit>() as u32 }

/// Status 0 publishes every hit and count; all errors leave output untouched.
/// Pointers must refer to live, valid buffers for their specified lengths.
#[no_mangle]
pub unsafe extern "C" fn tfs_armor_query(
    parts: *const PartInput,
    part_count: u32,
    hits: *mut Hit,
    capacity: u32,
    output_count: *mut u32,
) -> i32 {
    if !aligned(parts) || !aligned(hits) || !aligned(output_count)
        || !(1..=MAX_PARTS).contains(&(part_count as usize))
        || capacity as usize > MAX_TRIANGLES
    {
        return 1;
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        let parts = std::slice::from_raw_parts(parts, part_count as usize);
        let mut total = 0usize;
        for part in parts {
            total = total.checked_add(part.count as usize).ok_or(1)?;
            if part.count == 0 || total > MAX_TRIANGLES || !aligned(part.triangles)
                || !part.origin.iter().chain(&part.direction).all(|&v| bounded(v))
                || !bounded(part.distance) || part.distance < 0.
            {
                return Err(3);
            }
            let values = std::slice::from_raw_parts(part.triangles, part.count as usize * 9);
            if !values.iter().all(|&v| bounded(v)) {
                return Err(3);
            }
        }
        if (capacity as usize) < total {
            return Err(1);
        }
        let mut result = Vec::new();
        for (index, part) in parts.iter().enumerate() {
            let values = std::slice::from_raw_parts(part.triangles, part.count as usize * 9);
            for (triangle, vertices) in values.chunks_exact(9).enumerate() {
                if let Some(distance) = intersect(vertices, part) {
                    result.push(Hit { part: index as u32, triangle: triangle as u32, distance });
                }
            }
        }
        Ok(result)
    }));
    match result {
        Ok(Ok(values)) => {
            std::ptr::copy_nonoverlapping(values.as_ptr(), hits, values.len());
            output_count.write(values.len() as u32);
            0
        }
        Ok(Err(code)) => code,
        Err(_) => 10,
    }
}

/// Coarse rejection only. Possible/borderline hits retain Python's predicate.
/// Rows are origin(3), direction(3), clipped distance; targets center(3),
/// Python-computed squared radius. No geometry or accepted hit is changed.
#[no_mangle]
pub extern "C" fn tfs_marker_spheres_abi() -> u32 { 1 }

#[no_mangle]
pub unsafe extern "C" fn tfs_marker_spheres(
    rays: *const f64, ray_count: u32,
    spheres: *const f64, sphere_count: u32, output: *mut u8,
) -> i32 {
    if !aligned(rays) || !aligned(spheres) || output.is_null()
        || !(1..=64).contains(&ray_count) || !(1..=256).contains(&sphere_count)
    { return 1; }
    let result = catch_unwind(AssertUnwindSafe(|| {
        let rays = std::slice::from_raw_parts(rays, ray_count as usize*7);
        let spheres = std::slice::from_raw_parts(spheres, sphere_count as usize*4);
        if !rays.iter().all(|&v| bounded(v))
            || !spheres.chunks_exact(4).all(|s|
                s[..3].iter().all(|&v| bounded(v))
                && s[3].is_finite() && s[3] >= 0. && s[3] < LIMIT*LIMIT)
            || rays.chunks_exact(7).any(|r| r[6] < 0.)
        { return Err(3); }
        let mut flags = Vec::with_capacity(ray_count as usize*sphere_count as usize);
        for r in rays.chunks_exact(7) {
            let d = [r[3], r[4], r[5]];
            let length2 = (d[0]*d[0]+d[1]*d[1])+d[2]*d[2];
            for s in spheres.chunks_exact(4) {
                if length2 < 1e-16*(1.-1e-8) { flags.push(0); continue; }
                if length2 <= 1e-16*(1.+1e-8) { flags.push(1); continue; }
                let delta = [s[0]-r[0], s[1]-r[1], s[2]-r[2]];
                let dot = (delta[0]*d[0]+delta[1]*d[1])+delta[2]*d[2];
                let t = (dot/length2).min(r[6]).max(0.);
                let mut distance2 = 0.;
                let mut scale = 1.+s[3];
                for i in 0..3 {
                    let travel = d[i]*t;
                    let gap = delta[i]-travel;
                    distance2 += gap*gap;
                    scale += delta[i]*delta[i]+travel*travel;
                }
                // Include cancellation/reduction differences, rather than
                // assuming Python/NumPy/LLVM use the same three-term sum.
                // Only well-separated misses are removed; the original
                // predicate decides every potentially touching sphere.
                flags.push(u8::from(distance2 <= s[3]+scale*1e-8));
            }
        }
        Ok(flags)
    }));
    match result {
        Ok(Ok(flags)) => { std::ptr::copy_nonoverlapping(flags.as_ptr(),output,flags.len()); 0 },
        Ok(Err(code)) => code,
        Err(_) => 10,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(vertices: &[f64]) -> PartInput {
        PartInput { triangles: vertices.as_ptr(), count: 1,
            origin: [0.25, 0.25, 1.], direction: [0., 0., -1.], distance: 2. }
    }

    #[test]
    fn marker_coarse_rejection_keeps_clipped_boundary_and_zero_direction() {
        let rays = [0.,0.,0., 0.,0.,1., 10., 0.,0.,0., 0.,0.,0.,10.];
        let spheres = [0.,0.,5.,1., 1.,0.,10.,1., 0.,0.,12.,1., 100.,0.,5.,1.];
        let mut out = [9u8;8];
        assert_eq!(unsafe{tfs_marker_spheres(rays.as_ptr(),2,spheres.as_ptr(),4,out.as_mut_ptr())},0);
        assert_eq!(out,[1,1,0,0, 0,0,0,0]);
    }

    #[test]
    fn marker_uncertain_reductions_remain_on_original_path() {
        let rays = [0.,0.,0., 1e-8,0.,0.,100.];
        let spheres = [100.,100.,100.,1.];
        let mut out=[9u8];
        assert_eq!(unsafe{tfs_marker_spheres(rays.as_ptr(),1,spheres.as_ptr(),1,out.as_mut_ptr())},0);
        assert_eq!(out,[1]);
    }

    #[test]
    fn marker_invalid_inputs_leave_output_untouched() {
        let rays=[0.,0.,0., 0.,0.,1.,-1.];
        let spheres=[0.,0.,5.,1.];
        let mut out=[9u8];
        assert_eq!(unsafe{tfs_marker_spheres(rays.as_ptr(),1,spheres.as_ptr(),1,out.as_mut_ptr())},3);
        assert_eq!(out,[9]);
        assert_eq!(unsafe{tfs_marker_spheres(std::ptr::null(),1,spheres.as_ptr(),1,out.as_mut_ptr())},1);
    }

    #[test]
    fn entry_backface_clip_and_edge_tolerance() {
        let vertices = [0., 0., 0., 1., 0., 0., 0., 1., 0.];
        let mut part = fixture(&vertices);
        assert_eq!(intersect(&vertices, &part), Some(1.));
        part.distance = 0.99;
        assert_eq!(intersect(&vertices, &part), None);
        part.distance = 2.;
        part.origin = [-0.5e-7, 0.25, 1.];
        assert_eq!(intersect(&vertices, &part), Some(1.));
        part.origin[0] = -2e-7;
        assert_eq!(intersect(&vertices, &part), None);
        part.origin = [0.25, 0.25, -1.];
        part.direction = [0., 0., 1.];
        assert_eq!(intersect(&vertices, &part), None);
    }

    #[test]
    fn errors_do_not_publish_and_hits_keep_part_then_triangle_order() {
        let vertices = [0., 0., 0., 1., 0., 0., 0., 1., 0.];
        let part = fixture(&vertices);
        let mut output = [Hit { part: 42, triangle: 43, distance: 44. }; 2];
        let mut count = 45;
        assert_eq!(unsafe { tfs_armor_query(&part, 1, output.as_mut_ptr(), 0, &mut count) }, 1);
        assert_eq!(count, 45);
        assert_eq!(output[0].part, 42);
        let parts = [part, part];
        assert_eq!(unsafe { tfs_armor_query(parts.as_ptr(), 2, output.as_mut_ptr(), 2, &mut count) }, 0);
        assert_eq!(count, 2);
        assert_eq!(output[0], Hit { part: 0, triangle: 0, distance: 1. });
        assert_eq!(output[1], Hit { part: 1, triangle: 0, distance: 1. });
    }

    #[test]
    fn invalid_values_and_nulls_reject_before_publication() {
        let vertices = [0., 0., 0., 1., 0., 0., 0., 1., f64::NAN];
        let mut part = fixture(&vertices);
        let mut output = Hit { part: 42, triangle: 43, distance: 44. };
        let mut count = 45;
        assert_eq!(unsafe { tfs_armor_query(&part, 1, &mut output, 1, &mut count) }, 3);
        part.triangles = std::ptr::null();
        assert_eq!(unsafe { tfs_armor_query(&part, 1, &mut output, 1, &mut count) }, 3);
        assert_eq!(count, 45);
        assert_eq!(output.distance, 44.);
        assert_eq!(unsafe { tfs_armor_query(std::ptr::null(), 1, &mut output, 1, &mut count) }, 1);
    }
}
