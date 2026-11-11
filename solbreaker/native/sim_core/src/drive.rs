use crate::{aligned, math, DriveInput, Motion, Pose, SampleFn, Terrain, ValidateFn, ABI_VERSION};
use std::f64::consts::{PI, TAU};
use std::ffi::c_void;

pub(crate) const INVALID_INPUT: u32 = 1;
const STEP_LIMIT: u32 = 2;
const UNSUPPORTED: u32 = 3;
const SAMPLE_ERROR: u32 = 4;
const FIT_ERROR: u32 = 5;
const VALIDATION_REJECTED: u32 = 6;
const VALIDATION_ERROR: u32 = 7;
const DETACHED: u32 = 8;
const NONFINITE: u32 = 9;
pub(crate) const PANIC: u32 = 10;
const MAX_STATIONS: usize = 256;
const MAX_STEPS: usize = 512;

#[derive(Debug)]
pub(crate) struct Failure {
    pub prefix: Option<Box<Prefix>>,
    pub reason: u32,
    pub support_queries: u32,
    pub validation_queries: u32,
}

impl Failure {
    pub fn new(reason: u32) -> Self {
        Self { reason, support_queries: 0, validation_queries: 0, prefix: None }
    }
}

#[derive(Debug)]
pub(crate) struct Prefix {
    pub rotated: Pose,
    pub road: Pose,
    pub spring: Spring,
    pub hints: Vec<usize>,
    pub lower_bounds: Vec<f64>,
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct Spring {
    pub pose: Pose,
    pub velocity: [f64; 3],
    pub lean: [f64; 4],
    pub angular: [f64; 2],
}

pub(crate) struct ResultState {
    pub pose: Pose,
    pub road: Pose,
    pub motion: Motion,
    pub lean: [f64; 4],
    pub moved: bool,
    pub blocked: bool,
    pub hints: Vec<usize>,
    pub spring: Spring,
    pub bounds_pose: Pose,
    pub memory_heights: Vec<f64>,
    pub memory_speeds: Vec<f64>,
    pub lower_bounds: Vec<f64>,
    pub support_queries: u32,
    pub validation_queries: u32,
}

struct Runner<'a> {
    input: DriveInput,
    a: Vec<f64>,
    b: Vec<f64>,
    p: Vec<f64>,
    weight: Vec<f64>,
    phi: Vec<[f64; 3]>,
    heights: &'a [u16],
    sample: SampleFn,
    validate: ValidateFn,
    context: *mut c_void,
    hints: Vec<usize>,
    bounds_pose: Pose,
    lower_bounds: Vec<f64>,
    spring: Spring,
    support_queries: u32,
    validation_queries: u32,
}

fn finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

fn pose_finite(p: Pose) -> bool {
    finite(&[p.x, p.y, p.z, p.yaw, p.pitch, p.roll])
}

fn motion_finite(m: Motion) -> bool {
    finite(&m.velocity) && finite(&m.suspension) && finite(&m.angular)
}

/// The second column of orientation.matrix, in the same arithmetic order.
pub(crate) fn up(p: Pose) -> [f64; 3] {
    let cy = p.yaw.cos();
    let sy = p.yaw.sin();
    let cp = p.pitch.cos();
    let sp = p.pitch.sin();
    let cr = p.roll.cos();
    let sr = p.roll.sin();
    [-cy * sr + sy * sp * cr, cp * cr, sy * sr + cy * sp * cr]
}

fn unlean(pose: Pose, lean: [f64; 4]) -> Pose {
    Pose { pitch: pose.pitch - lean[0], roll: pose.roll - lean[1], ..pose }
}

fn road_velocity(road: Pose, vx: f64, vz: f64) -> [f64; 3] {
    let normal = up(road);
    let vy = -(normal[0] * vx + normal[2] * vz) / normal[1].max(0.05);
    [vx, vy, vz]
}

fn energy(velocity: [f64; 3]) -> f64 {
    velocity[0] * velocity[0] + velocity[1] * velocity[1] + velocity[2] * velocity[2]
}

fn spring_step(value: f64, goal: f64, velocity: f64, feed: f64, frequency: f64, dt: f64)
    -> (f64, f64)
{
    let decay = (-frequency * dt).exp();
    let old_goal = goal - feed * dt;
    let error = value - old_goal;
    let relative = velocity - feed;
    let c = relative + frequency * error;
    (
        goal + (error + c * dt) * decay,
        feed + (relative - frequency * c * dt) * decay,
    )
}

fn spring_pose(
    previous: Pose,
    target: Pose,
    old_road: Pose,
    dt: f64,
    motion: Motion,
    lean: [f64; 4],
    effort: [f64; 3],
    constants: [f64; 8],
) -> Spring {
    let vx = (target.x - previous.x) / dt;
    let vz = (target.z - previous.z) / dt;
    let normal = up(old_road);
    let grade_speed = if normal[1] > 0.5 {
        -(normal[0] * vx + normal[2] * vz) / normal[1]
    } else {
        0.0
    };
    let omega = constants[5];
    let angular_omega = constants[6];
    let pitch_lean = (-effort[0] * 0.004).clamp(-0.045, 0.045);
    let roll_lean = (-effort[1] * 0.004).clamp(-0.045, 0.045);
    let (lp, lvp) = spring_step(lean[0], pitch_lean, lean[2], 0.0, angular_omega, dt);
    let (lr, lvr) = spring_step(lean[1], roll_lean, lean[3], 0.0, angular_omega, dt);
    let (mut y, mut vy) = spring_step(
        previous.y, target.y, motion.suspension[0], grade_speed, omega, dt,
    );
    let free_y = previous.y + motion.suspension[0] * dt - 0.5 * constants[2] * dt * dt;
    let free_vy = motion.suspension[0] - constants[2] * dt;
    let unloaded = y < free_y;
    if unloaded {
        y = free_y;
        vy = vy.max(free_vy);
    }
    let (pitch, vpitch) = spring_step(
        previous.pitch - lean[0], target.pitch,
        motion.suspension[1] - lean[2], 0.0, omega, dt,
    );
    let (roll, vroll) = spring_step(
        previous.roll - lean[1], target.roll,
        motion.suspension[2] - lean[3], 0.0, omega, dt,
    );
    let pitch = pitch + lp;
    let roll = roll + lr;
    let pose = Pose { x: target.x, y, z: target.z, yaw: target.yaw, pitch, roll };
    let angular = if unloaded {
        motion.angular
    } else {
        [(pitch - previous.pitch) / dt, (roll - previous.roll) / dt]
    };
    Spring {
        pose,
        velocity: [vy, vpitch + lvp, vroll + lvr],
        lean: [lp, lr, lvp, lvr],
        angular,
    }
}

/// Scalar MapTerrain.height, including clamping and interpolation order.
pub(crate) fn terrain_height(terrain: Terrain, heights: &[u16], x: f64, z: f64) -> f64 {
    let size = terrain.size as usize;
    let last = (size - 1) as f64;
    let fx = ((x - terrain.min_x) * size as f64 / terrain.width).clamp(0.0, last);
    let fz = ((z - terrain.min_z) * size as f64 / terrain.depth).clamp(0.0, last);
    let ix = fx as usize;
    let iz = fz as usize;
    let dx = fx - ix as f64;
    let dz = fz - iz as f64;
    let h = |a: usize, b: usize| heights[b.min(size - 1) * size + a.min(size - 1)] as f64;
    let row0 = h(ix, iz) * (1.0 - dx) + h(ix + 1, iz) * dx;
    let row1 = h(ix, iz + 1) * (1.0 - dx) + h(ix + 1, iz + 1) * dx;
    terrain.height_min + (row0 * (1.0 - dz) + row1 * dz) * terrain.height_range / 65535.0
}

fn station_heights(pose: Pose, a: &[f64], b: &[f64]) -> Vec<f64> {
    let slope_z = -pose.pitch.tan();
    let slope_x = pose.roll.tan() * (1.0 + slope_z * slope_z).sqrt();
    a.iter().zip(b).map(|(a, b)| pose.y + a * slope_x + b * slope_z).collect()
}

fn footprint_supported(a: &[f64], b: &[f64], weight: &[f64]) -> bool {
    let active: Vec<_> = (0..a.len()).filter(|&i| weight[i] > 0.0).collect();
    if active.len() < 3 {
        return false;
    }
    let min_a = active.iter().map(|&i| a[i]).fold(f64::INFINITY, f64::min);
    let max_a = active.iter().map(|&i| a[i]).fold(f64::NEG_INFINITY, f64::max);
    let min_b = active.iter().map(|&i| b[i]).fold(f64::INFINITY, f64::min);
    let max_b = active.iter().map(|&i| b[i]).fold(f64::NEG_INFINITY, f64::max);
    if min_a > 0.05 || max_a < -0.05 || min_b > 0.05 || max_b < -0.05 {
        return false;
    }
    let first = active[0];
    // Conservatively decline an ill-conditioned footprint rather than
    // extrapolating a carrying plane from a partial or collinear patch.
    for &second in active.iter().skip(1) {
        for &third in active.iter().skip(2) {
            let ax = a[second] - a[first];
            let az = b[second] - b[first];
            let bx = a[third] - a[first];
            let bz = b[third] - b[first];
            let determinant = ax * bz - az * bx;
            let scale = (ax.abs() + az.abs()) * (bx.abs() + bz.abs());
            if determinant.abs() > 64.0 * f64::EPSILON * scale.max(1.0) {
                return true;
            }
        }
    }
    false
}

impl Runner<'_> {
    fn fail(&self, reason: u32) -> Failure {
        Failure { reason, support_queries: self.support_queries, validation_queries: self.validation_queries, prefix: None }
    }

    fn terrain(&self, x: f64, z: f64) -> Result<f64, Failure> {
        if !finite(&[x, z]) {
            return Err(self.fail(NONFINITE));
        }
        let result = terrain_height(self.input.terrain, self.heights, x, z);
        if !result.is_finite() {
            return Err(self.fail(NONFINITE));
        }
        Ok(result)
    }

    fn support(
        &mut self,
        x: f64,
        z: f64,
        yaw: f64,
        reference: Pose,
        start: Pose,
        old_road: Pose,
        motion: Motion,
        lean: [f64; 4],
        h: f64,
    ) -> Result<(Pose, Pose), Failure> {
        self.support_with_centre(x,z,yaw,reference,start,old_road,motion,lean,h,None)
    }

    fn support_with_centre(
        &mut self, x:f64,z:f64,yaw:f64,reference:Pose,start:Pose,old_road:Pose,
        motion:Motion,lean:[f64;4],h:f64,centre_override:Option<f64>,
    ) -> Result<(Pose,Pose),Failure> {
        if !finite(&[x, z, yaw]) || !pose_finite(reference) {
            return Err(self.fail(NONFINITE));
        }
        let c = yaw.cos();
        let s = yaw.sin();
        let xs: Vec<_> = self.a.iter().zip(&self.b).map(|(a, b)| x + c * a + s * b).collect();
        let zs: Vec<_> = self.a.iter().zip(&self.b).map(|(a, b)| z - s * a + c * b).collect();
        if !finite(&xs) || !finite(&zs) {
            return Err(self.fail(NONFINITE));
        }
        let mut ground = vec![f64::NAN; self.a.len()];
        self.support_queries += 1;
        let status = unsafe {
            (self.sample)(self.context, ground.len() as u32, xs.as_ptr(), zs.as_ptr(),
                          &reference, ground.as_mut_ptr())
        };
        if status != 0 {
            return Err(self.fail(SAMPLE_ERROR));
        }
        if !finite(&ground) {
            return Err(self.fail(NONFINITE));
        }
        let carrying = unlean(start, lean);
        let normal = up(carrying);
        let reach = self.input.constants[3];
        let skin = self.input.constants[0];
        let relief: Vec<_> = ground.iter().zip(&self.p).map(|(g, p)| g - p).collect();
        if !finite(&relief) {
            return Err(self.fail(NONFINITE));
        }
        let relief_min = relief.iter().copied().fold(f64::INFINITY, f64::min);
        let relief_max = relief.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        if relief_max - relief_min > reach + skin {
            for i in 0..ground.len() {
                let prediction = carrying.y - (normal[0] * (xs[i] - carrying.x)
                    + normal[2] * (zs[i] - carrying.z)) / normal[1].max(0.05);
                if !prediction.is_finite() {
                    return Err(self.fail(NONFINITE));
                }
                // Unreachable ramp-end stations already have zero fit weight.
                // Python's weight * reachable leaves those weights unchanged;
                // their hard lower bounds still participate in the fit.
                if self.weight[i] > 0.0 && relief[i] < prediction - reach {
                    return Err(self.fail(UNSUPPORTED));
                }
            }
        }
        let centre = if let Some(value)=centre_override {
            if !value.is_finite() {return Err(self.fail(NONFINITE));}
            value
        } else {self.terrain(x,z)?};
        let compression = self.input.constants[4];
        let mut lower: Vec<_> = relief.iter().map(|r| r - compression).collect();
        let centre_floor = centre - self.input.constants[7] + skin;
        lower.push(centre_floor);
        let mut target = ground;
        target.push(0.0);
        if !finite(&lower) {
            return Err(self.fail(NONFINITE));
        }
        let fit = math::fit_plane(&self.phi, &target, &self.weight, &lower, &self.hints)
            .ok_or_else(|| self.fail(FIT_ERROR))?;
        if !finite(&fit.plane) || fit.active.len() > 3
            || fit.active.iter().any(|&i| i >= self.phi.len())
        {
            return Err(self.fail(FIT_ERROR));
        }
        let [y, slope_x, slope_z] = fit.plane;
        let pitch = -slope_z.atan();
        let roll = (slope_x / (1.0 + slope_z * slope_z).sqrt()).atan();
        let road = Pose { x, y, z, yaw, pitch, roll };
        if !pose_finite(road) {
            return Err(self.fail(NONFINITE));
        }
        self.hints = fit.active.into_iter().filter(|&i| i < self.a.len()).collect();
        self.bounds_pose = road;
        self.lower_bounds = lower;
        self.spring = spring_pose(start, road, old_road, h, motion, lean,
                                  self.input.effort, self.input.constants);
        if !pose_finite(self.spring.pose) || !finite(&self.spring.velocity)
            || !finite(&self.spring.lean) || !finite(&self.spring.angular)
        {
            return Err(self.fail(NONFINITE));
        }
        Ok((self.spring.pose, road))
    }

    fn validate(&mut self, candidate: Pose, previous: Pose) -> Result<(), Failure> {
        self.validation_queries += 1;
        let status = unsafe { (self.validate)(self.context, &candidate, &previous) };
        match status {
            0 => Ok(()),
            1 => Err(self.fail(VALIDATION_REJECTED)),
            _ => Err(self.fail(VALIDATION_ERROR)),
        }
    }

    fn detached(&self, start: Pose, pose: Pose, road: Pose, h: f64, vy: f64)
        -> Result<bool, Failure>
    {
        let [skin, _, gravity, reach, travel, ..] = self.input.constants;
        let ballistic = start.y + vy * h - 0.5 * gravity * h * h;
        let gap = ballistic.max(pose.y) - road.y;
        if !finite(&[ballistic, gap]) {
            return Err(self.fail(NONFINITE));
        }
        let terrain = self.terrain(pose.x, pose.z)?;
        // Every accepted native fit has a complete, centre-spanning footprint,
        // so _carrying_supported is true here.
        if gap <= travel + skin {
            return Ok(false);
        }
        let old_terrain = self.terrain(start.x, start.z)?;
        if terrain > old_terrain + 1e-4 {
            return Ok(false);
        }
        let horizontal = (pose.x - start.x).hypot(pose.z - start.z);
        Ok(start.y - terrain > reach || old_terrain - terrain > 0.15 * horizontal + skin)
    }
}

fn input_valid(input: &DriveInput) -> bool {
    input.abi_version == ABI_VERSION
        && (3..=MAX_STATIONS).contains(&(input.count as usize))
        && input.has_displacement <= 1 && input.was_blocked <= 1 && input.has_memory <= 1
        && input.hint_count <= 3
        && input.hints[..input.hint_count.min(3) as usize].iter().all(|&i| i < input.count)
        && pose_finite(input.pose) && pose_finite(input.road) && motion_finite(input.motion)
        && finite(&input.lean) && finite(&input.effort)
        && finite(&[input.distance, input.yaw_delta, input.dt]) && input.dt > 0.0
        && finite(&input.displacement) && finite(&input.constants)
        && input.constants[0] >= 0.0 && input.constants[1] > 0.0
        && input.constants[2] > 0.0 && input.constants[3] >= 0.0
        && input.constants[4] > 0.0 && input.constants[5] > 0.0
        && input.constants[6] > 0.0 && input.constants[7] >= 0.0
        && (2..=16384).contains(&input.terrain.size)
        && finite(&[input.terrain.min_x, input.terrain.min_z, input.terrain.width,
                     input.terrain.depth, input.terrain.height_min, input.terrain.height_range])
        && input.terrain.width > 0.0 && input.terrain.depth > 0.0
        && input.terrain.height_range >= 0.0
        && aligned(input.terrain.heights)
        && [input.a, input.b, input.p, input.weight].iter().all(|&p| aligned(p))
        && (input.has_memory == 0 || (aligned(input.memory_heights) && aligned(input.memory_speeds)))
}

pub(crate) unsafe fn run(input: DriveInput, sample: SampleFn, validate: ValidateFn,
                         context: *mut c_void, capture_prefix: u32) -> Result<ResultState, Failure>
{
    if !input_valid(&input) {
        return Err(Failure::new(INVALID_INPUT));
    }
    let count = input.count as usize;
    let a = std::slice::from_raw_parts(input.a, count).to_vec();
    let b = std::slice::from_raw_parts(input.b, count).to_vec();
    let p = std::slice::from_raw_parts(input.p, count).to_vec();
    let mut weight = std::slice::from_raw_parts(input.weight, count).to_vec();
    if !finite(&a) || !finite(&b) || !finite(&p) || !finite(&weight)
        || weight.iter().any(|&w| w < 0.0)
    {
        return Err(Failure::new(INVALID_INPUT));
    }
    if !footprint_supported(&a, &b, &weight) {
        return Err(Failure::new(UNSUPPORTED));
    }
    let mut memory_heights = if input.has_memory != 0 {
        std::slice::from_raw_parts(input.memory_heights, count).to_vec()
    } else { vec![0.0; count] };
    let mut memory_speeds = if input.has_memory != 0 {
        std::slice::from_raw_parts(input.memory_speeds, count).to_vec()
    } else { vec![0.0; count] };
    if !finite(&memory_heights) || !finite(&memory_speeds)
        || memory_speeds.iter().any(|&v| v < 0.0)
    {
        return Err(Failure::new(INVALID_INPUT));
    }
    let mut has_memory = input.has_memory != 0;
    let travel = if input.has_displacement != 0 {
        input.displacement[0].hypot(input.displacement[1])
    } else { input.distance.abs() };
    let steps_float = (travel / input.constants[1]).ceil()
        .max((input.yaw_delta.abs() / (0.75_f64.to_radians())).ceil()).max(1.0);
    if !steps_float.is_finite() || steps_float > MAX_STEPS as f64 {
        return Err(Failure::new(STEP_LIMIT));
    }
    let steps = steps_float as usize;
    let h = input.dt / steps as f64;
    if !h.is_finite() || h <= 0.0 {
        return Err(Failure::new(INVALID_INPUT));
    }
    let mut phi: Vec<_> = a.iter().zip(&b).map(|(&a, &b)| [1.0, a, b]).collect();
    phi.push([1.0, 0.0, 0.0]);
    weight.push(0.0);
    let heights = std::slice::from_raw_parts(input.terrain.heights,
                                            (input.terrain.size as usize).pow(2));
    let mut runner = Runner {
        input, a, b, p, weight, phi, heights, sample, validate, context,
        hints: input.hints[..input.hint_count as usize].iter().map(|&i| i as usize).collect(),
        bounds_pose: input.road, lower_bounds: Vec::new(), spring: Spring::default(),
        support_queries: 0, validation_queries: 0,
    };
    let mut pose = input.pose;
    let mut road = input.road;
    let mut motion = input.motion;
    let mut lean = input.lean;
    let mut moved = false;
    let mut was_blocked = input.was_blocked != 0;
    let mut curvature_scale = 1.0;
    for _ in 0..steps {
        let start = pose;
        let old_road = road;
        let yaw = (start.yaw + input.yaw_delta / steps as f64 + PI).rem_euclid(TAU) - PI;
        let (rotated, _) = runner.support(start.x, start.z, yaw, unlean(start, lean),
                                         start, old_road, motion, lean, h)?;
        if let Err(mut failure) = runner.validate(rotated, start) {
            if capture_prefix >= 2 && failure.reason == VALIDATION_REJECTED
                && runner.support_queries == 1 && runner.validation_queries == 1
            {
                failure.prefix = Some(Box::new(Prefix {
                    rotated, road: runner.bounds_pose, spring: runner.spring,
                    hints: runner.hints.clone(), lower_bounds: runner.lower_bounds.clone(),
                }));
            }
            return Err(failure);
        }
        let previous = rotated;
        let (mut dx, mut dz) = if input.has_displacement == 0 {
            (yaw.sin() * input.distance / steps as f64 * curvature_scale,
             yaw.cos() * input.distance / steps as f64 * curvature_scale)
        } else {
            (input.displacement[0] / steps as f64 * curvature_scale,
             input.displacement[1] / steps as f64 * curvature_scale)
        };
        let incoming = road_velocity(old_road, dx / h, dz / h);
        if !finite(&incoming) || !energy(incoming).is_finite() {
            return Err(runner.fail(NONFINITE));
        }
        let reference = unlean(previous, lean);
        let (mut candidate, mut road_pose) = runner.support(
            start.x + dx, start.z + dz, yaw, reference, start, old_road, motion, lean, h,
        )?;
        if let Err(mut failure) = runner.validate(candidate, previous) {
            // Only the first translation, before any state is committed.
            // The caller must still validate it with its impact/destruction
            // policy; this checkpoint carries arithmetic, not admission.
            if capture_prefix >= 1 && failure.reason == VALIDATION_REJECTED
                && runner.support_queries == 2 && runner.validation_queries == 2
            {
                failure.prefix = Some(Box::new(Prefix {
                    rotated: previous, road: road_pose, spring: runner.spring,
                    hints: runner.hints.clone(), lower_bounds: runner.lower_bounds.clone(),
                }));
            }
            return Err(failure);
        }
        if dx.hypot(dz) > 1e-8 {
            if runner.detached(start, candidate, road_pose, h, incoming[1])? {
                return Err(runner.fail(DETACHED));
            }
            let tangent = road_velocity(road_pose, dx / h, dz / h);
            let factor = (energy(incoming) / energy(tangent).max(1e-12)).sqrt();
            if !finite(&tangent) || !energy(tangent).is_finite() || !factor.is_finite() {
                return Err(runner.fail(NONFINITE));
            }
            curvature_scale *= factor;
            dx *= factor;
            dz *= factor;
            if !finite(&[curvature_scale, dx, dz]) {
                return Err(runner.fail(NONFINITE));
            }
            (candidate, road_pose) = runner.support(
                start.x + dx, start.z + dz, yaw, reference, start, old_road, motion, lean, h,
            )?;
            runner.validate(candidate, previous)?;
        }
        pose = candidate;
        moved |= dx.hypot(dz) > 0.00001_f64.max(travel / steps as f64 * 0.02);
        // All accepted candidates are the most recent spring query. Keep the
        // same equality guard as Python in case a callback changes its inputs.
        if runner.spring.pose == pose {
            motion.suspension = runner.spring.velocity;
            lean = runner.spring.lean;
            motion.angular = runner.spring.angular;
        } else {
            motion.suspension = [0.0; 3];
            motion.angular = [0.0; 2];
        }
        let blocked = travel > 0.0 && dx.hypot(dz) < 1e-9;
        if was_blocked && !blocked {
            motion.suspension[0] = 0.0;
        }
        was_blocked = blocked;
        let actual_dx = pose.x - start.x;
        let actual_dz = pose.z - start.z;
        if actual_dx.hypot(actual_dz) < 1e-9 {
            motion.velocity = [0.0; 3];
        } else {
            if runner.detached(start, pose, road_pose, h, incoming[1])? {
                return Err(runner.fail(DETACHED));
            }
            let tangent = road_velocity(road_pose, actual_dx / h, actual_dz / h);
            let speed = energy(tangent).sqrt();
            let budget = energy(incoming).sqrt();
            if !finite(&tangent) || !finite(&[speed, budget]) {
                return Err(runner.fail(NONFINITE));
            }
            let scale = if speed != 0.0 { (budget / speed).min(1.0) } else { 1.0 };
            motion.velocity = tangent.map(|v| v * scale);
        }
        let new_heights = station_heights(pose, &runner.a, &runner.b);
        memory_speeds = if has_memory {
            memory_heights.iter().zip(&new_heights)
                .map(|(old, new)| ((old - new) / h).max(0.0)).collect()
        } else { vec![0.0; count] };
        memory_heights = new_heights;
        has_memory = true;
        if !motion_finite(motion) || !finite(&memory_heights) || !finite(&memory_speeds) {
            return Err(runner.fail(NONFINITE));
        }
        road = road_pose;
    }
    Ok(ResultState {
        pose, road, motion, lean, moved, blocked: was_blocked,
        hints: runner.hints, spring: runner.spring, bounds_pose: runner.bounds_pose,
        memory_heights, memory_speeds, lower_bounds: runner.lower_bounds,
        support_queries: runner.support_queries, validation_queries: runner.validation_queries,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terrain_matches_scalar_corners_and_bilinear_order() {
        let heights = [0, 65535, 32768, 16384];
        let terrain = Terrain {
            heights: heights.as_ptr(), size: 2, min_x: -1.0, min_z: -2.0,
            width: 4.0, depth: 8.0, height_min: -3.0, height_range: 12.0,
        };
        assert_eq!(terrain_height(terrain, &heights, -100.0, -100.0), -3.0);
        assert_eq!(terrain_height(terrain, &heights, 100.0, -100.0), 9.0);
        let expected = -3.0 + ((0.0 * 0.5 + 65535.0 * 0.5) * 0.5
            + (32768.0 * 0.5 + 16384.0 * 0.5) * 0.5) * 12.0 / 65535.0;
        assert_eq!(terrain_height(terrain, &heights, 0.0, 0.0), expected);
    }

    #[test]
    fn spring_cannot_pull_heave_down_faster_than_gravity() {
        let previous = Pose { y: 1.0, ..Pose::default() };
        let target = Pose::default();
        let constants = [0.008, 0.06, 9.81, 0.28, 0.1, 32.0, 12.0, 0.0];
        let result = spring_pose(previous, target, previous, 0.01, Motion::default(),
                                 [0.0; 4], [0.0; 3], constants);
        assert_eq!(result.pose.y, 1.0 - 0.5 * 9.81 * 0.01 * 0.01);
        assert!(result.velocity[0] >= -9.81 * 0.01);
    }

    #[test]
    fn footprint_requires_both_tracks_and_a_longitudinal_span() {
        assert!(footprint_supported(&[-1.0, -1.0, 1.0, 1.0],
                                    &[-2.0, 2.0, -2.0, 2.0], &[1.0; 4]));
        assert!(!footprint_supported(&[-1.0, -1.0, -1.0],
                                     &[-2.0, 0.0, 2.0], &[1.0; 3]));
        assert!(!footprint_supported(&[-1.0, 0.0, 1.0],
                                     &[-2.0, 0.0, 2.0], &[1.0; 3]));
    }

    #[test]
    fn road_tangent_preserves_flat_horizontal_components() {
        assert_eq!(road_velocity(Pose::default(), 3.0, -4.0), [3.0, -0.0, -4.0]);
        let slope = Pose { pitch: -0.2_f64.atan(), ..Pose::default() };
        let velocity = road_velocity(slope, 0.0, 5.0);
        assert!((velocity[1] - 1.0).abs() < 1e-14);
    }
}


/// Pure support/spring arithmetic for the existing Python fallback controller.
/// No move admission, destruction, integration or world publication occurs here.
pub(crate) unsafe fn support_slice(
    input:DriveInput, query:crate::support_slice::SupportQuery,
    sample:SampleFn,context:*mut c_void,
) -> Result<Prefix,Failure> {
    if !input_valid(&input) || !query.valid(input.count as usize) {
        return Err(Failure::new(INVALID_INPUT));
    }
    let count=input.count as usize;
    let a=std::slice::from_raw_parts(input.a,count).to_vec();
    let b=std::slice::from_raw_parts(input.b,count).to_vec();
    let p=std::slice::from_raw_parts(input.p,count).to_vec();
    let mut weight=std::slice::from_raw_parts(input.weight,count).to_vec();
    if !finite(&a) || !finite(&b) || !finite(&p) || !finite(&weight)
        || weight.iter().any(|&v|v<0.) {return Err(Failure::new(INVALID_INPUT));}
    if !footprint_supported(&a,&b,&weight) {return Err(Failure::new(UNSUPPORTED));}
    let mut phi:Vec<_>=a.iter().zip(&b).map(|(&a,&b)|[1.,a,b]).collect();
    phi.push([1.,0.,0.]);weight.push(0.);
    let heights=std::slice::from_raw_parts(input.terrain.heights,
                                         (input.terrain.size as usize).pow(2));
    unsafe extern "C" fn unused_validation(_: *mut c_void,_:*const Pose,_:*const Pose)->i32 {2}
    let mut runner=Runner{input,a,b,p,weight,phi,heights,sample,
        validate:unused_validation,context,
        hints:query.hints[..query.hint_count as usize].iter().map(|&i|i as usize).collect(),
        bounds_pose:query.road,lower_bounds:Vec::new(),spring:Spring::default(),
        support_queries:0,validation_queries:0};
    let (candidate,road)=runner.support_with_centre(query.x,query.z,query.yaw,
        query.reference,query.start,query.road,query.motion,query.lean,query.h,
        Some(query.centre))?;
    Ok(Prefix{rotated:candidate,road,spring:runner.spring,
        hints:runner.hints,lower_bounds:runner.lower_bounds})
}
