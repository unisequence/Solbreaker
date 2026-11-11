use std::mem::align_of;
use std::panic::{catch_unwind, AssertUnwindSafe};

#[repr(C)]
pub struct GroundVehicle {
    pub entity: u32,
    pub changed: u32,
    pub state: [f64; 3],
}

#[repr(C)]
pub struct GroundPair {
    pub a: u32,
    pub b: u32,
    pub point_a: [f64; 3],
    pub point_b: [f64; 3],
    pub normal: [f64; 3],
    pub separation: f64,
    pub was_touching: u32,
    pub mass_a: f64,
    pub mass_b: f64,
    pub inertia_a: f64,
    pub inertia_b: f64,
    pub center_a: [f64; 3],
    pub center_b: [f64; 3],
}

#[repr(C)]
pub struct GroundParams {
    pub restitution: f64,
    pub friction: f64,
    pub min_closing: f64,
    pub ram_min: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct GroundImpact {
    pub entity_a: u32,
    pub entity_b: u32,
    pub point_a: [f64; 3],
    pub point_b: [f64; 3],
    pub normal: [f64; 2],
    pub separation: f64,
    pub closing_speed: f64,
    pub normal_impulse: f64,
    pub tangent_impulse: f64,
    pub mass_a: f64,
    pub mass_b: f64,
    pub velocity_a: [f64; 2],
    pub velocity_b: [f64; 2],
    pub yaw_rate_a: f64,
    pub yaw_rate_b: f64,
    pub ram: u32,
    pub ram_energy: f64,
    pub ram_energy_a: f64,
    pub ram_energy_b: f64,
}

fn aligned<T>(ptr: *const T) -> bool {
    !ptr.is_null() && (ptr as usize) % align_of::<T>() == 0
}

const MAX_VEHICLES: usize = 128;
const MAX_PAIRS: usize = 512;

fn point_velocity(state: [f64; 3], arm: [f64; 3]) -> [f64; 2] {
    [state[0] + state[2] * arm[2], state[1] - state[2] * arm[0]]
}

fn cross(arm: [f64; 3], direction: [f64; 2]) -> f64 {
    arm[2] * direction[0] - arm[0] * direction[1]
}

fn valid_vehicle(vehicle: &GroundVehicle) -> bool {
    vehicle.state.iter().all(|value| value.is_finite())
}

fn valid_pair(pair: &GroundPair) -> bool {
    [pair.point_a, pair.point_b, pair.normal, pair.center_a, pair.center_b]
        .iter().flatten().all(|value| value.is_finite())
        && [pair.separation, pair.mass_a, pair.mass_b, pair.inertia_a, pair.inertia_b]
        .iter().all(|value| value.is_finite())
        && pair.mass_a > 0.0 && pair.mass_b > 0.0
        && pair.inertia_a > 1e-6 && pair.inertia_b > 1e-6
}

/// Apply the complete ordered planar impact stage to caller-owned vehicle states.
/// Output impacts are written only after every input has passed validation.
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_impacts(
    vehicle_count: u32,
    vehicles: *mut GroundVehicle,
    pair_count: u32,
    pairs: *const GroundPair,
    params: *const GroundParams,
    impacts: *mut GroundImpact,
    impact_count: *mut u32,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if vehicle_count as usize > MAX_VEHICLES || pair_count as usize > MAX_PAIRS
            || !aligned(vehicles) || !aligned(pairs) || !aligned(params)
            || !aligned(impacts) || !aligned(impact_count) {
            return 1;
        }
        let vehicles = std::slice::from_raw_parts_mut(vehicles, vehicle_count as usize);
        let pairs = std::slice::from_raw_parts(pairs, pair_count as usize);
        let params = &*params;
        if ![params.restitution, params.friction, params.min_closing, params.ram_min]
            .iter().all(|value| value.is_finite())
            || vehicles.iter().any(|vehicle| !valid_vehicle(vehicle))
            || pairs.iter().any(|pair| pair.a as usize >= vehicles.len()
                || pair.b as usize >= vehicles.len() || pair.a == pair.b || !valid_pair(pair)) {
            return 1;
        }
        let outputs = std::slice::from_raw_parts_mut(impacts, pair_count as usize);
        let mut written = 0usize;
        for pair in pairs {
            let normal_length = (pair.normal[0] * pair.normal[0]
                + pair.normal[2] * pair.normal[2]).sqrt();
            if !normal_length.is_finite() || normal_length < 0.25 {
                continue;
            }
            let normal = [pair.normal[0] / normal_length, pair.normal[2] / normal_length];
            let (mass_a, mass_b) = (pair.mass_a, pair.mass_b);
            let (inertia_a, inertia_b) = (pair.inertia_a, pair.inertia_b);
            let arm_a = [pair.point_a[0] - pair.center_a[0],
                         pair.point_a[1] - pair.center_a[1],
                         pair.point_a[2] - pair.center_a[2]];
            let arm_b = [pair.point_b[0] - pair.center_b[0],
                         pair.point_b[1] - pair.center_b[1],
                         pair.point_b[2] - pair.center_b[2]];
            let a = &vehicles[pair.a as usize];
            let b = &vehicles[pair.b as usize];
            let (entity_a, entity_b) = (a.entity, b.entity);
            let va = point_velocity(a.state, arm_a);
            let vb = point_velocity(b.state, arm_b);
            let closing = (va[0] - vb[0]) * normal[0] + (va[1] - vb[1]) * normal[1];
            if !closing.is_finite() || closing >= -params.min_closing {
                continue;
            }
            let q_a = cross(arm_a, normal);
            let q_b = cross(arm_b, normal);
            let effective = 1.0 / mass_a + 1.0 / mass_b
                + q_a * q_a / inertia_a + q_b * q_b / inertia_b;
            if !effective.is_finite() || effective <= 0.0 {
                return 2;
            }
            let normal_impulse = -(1.0 + params.restitution) * closing / effective;
            let ram_energy = 0.5 * closing * closing / effective;
            let share_a = (1.0 / mass_a + q_a * q_a / inertia_a) / effective;
            let ram = pair.was_touching == 0 && -closing >= params.ram_min;
            let impulse = [normal_impulse * normal[0], normal_impulse * normal[1]];
            let mut va = [a.state[0] + impulse[0] / mass_a,
                          a.state[1] + impulse[1] / mass_a];
            let mut wa = a.state[2] + q_a * normal_impulse / inertia_a;
            let mut vb = [b.state[0] - impulse[0] / mass_b,
                          b.state[1] - impulse[1] / mass_b];
            let mut wb = b.state[2] - q_b * normal_impulse / inertia_b;

            let tangent = [-normal[1], normal[0]];
            let pva = [va[0] + wa * arm_a[2], va[1] - wa * arm_a[0]];
            let pvb = [vb[0] + wb * arm_b[2], vb[1] - wb * arm_b[0]];
            let tangent_speed = (pva[0] - pvb[0]) * tangent[0]
                + (pva[1] - pvb[1]) * tangent[1];
            let t_a = cross(arm_a, tangent);
            let t_b = cross(arm_b, tangent);
            let tangent_effective = 1.0 / mass_a + 1.0 / mass_b
                + t_a * t_a / inertia_a + t_b * t_b / inertia_b;
            let mut tangent_impulse = 0.0;
            if tangent_effective > 0.0 && tangent_effective.is_finite() {
                tangent_impulse = (-tangent_speed / tangent_effective)
                    .clamp(-params.friction * normal_impulse,
                           params.friction * normal_impulse);
                let friction = [tangent_impulse * tangent[0], tangent_impulse * tangent[1]];
                va = [va[0] + friction[0] / mass_a, va[1] + friction[1] / mass_a];
                wa += t_a * tangent_impulse / inertia_a;
                vb = [vb[0] - friction[0] / mass_b, vb[1] - friction[1] / mass_b];
                wb -= t_b * tangent_impulse / inertia_b;
            }
            if ![va, vb].iter().flatten().chain([wa, wb, normal_impulse,
                    tangent_impulse, ram_energy, share_a].iter())
                .all(|value| value.is_finite()) {
                return 2;
            }
            vehicles[pair.a as usize].state = [va[0], va[1], wa];
            vehicles[pair.b as usize].state = [vb[0], vb[1], wb];
            vehicles[pair.a as usize].changed = 1;
            vehicles[pair.b as usize].changed = 1;
            outputs[written] = GroundImpact {
                entity_a,
                entity_b,
                point_a: pair.point_a,
                point_b: pair.point_b,
                normal,
                separation: pair.separation,
                closing_speed: closing,
                normal_impulse,
                tangent_impulse,
                mass_a,
                mass_b,
                velocity_a: va,
                velocity_b: vb,
                yaw_rate_a: wa,
                yaw_rate_b: wb,
                ram: ram as u32,
                ram_energy,
                ram_energy_a: ram_energy * share_a,
                ram_energy_b: ram_energy * (1.0 - share_a),
            };
            written += 1;
        }
        *impact_count = written as u32;
        0
    }));
    result.unwrap_or(3)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ordered_impulse_updates_both_vehicles_and_event() {
        let mut vehicles = [
            GroundVehicle { entity: 200, changed: 0, state: [0.0, 6.0, 0.0] },
            GroundVehicle { entity: 201, changed: 0, state: [0.0; 3] },
        ];
        let pairs = [GroundPair { a: 0, b: 1, point_a: [0.0; 3], point_b: [0.0; 3],
            normal: [0.0, 0.0, -1.0], separation: 0.0, was_touching: 0,
            mass_a: 1000.0, mass_b: 3000.0, inertia_a: 1000.0, inertia_b: 3000.0,
            center_a: [0.0; 3], center_b: [0.0; 3] }];
        let params = GroundParams { restitution: 0.08, friction: 0.5,
            min_closing: 0.05, ram_min: 1.5 };
        let mut impacts = [GroundImpact::default(); 1];
        let mut count = 0;
        let status = unsafe { tfs_ground_impacts(2, vehicles.as_mut_ptr(), 1, pairs.as_ptr(),
            &params, impacts.as_mut_ptr(), &mut count) };
        assert_eq!(status, 0);
        assert_eq!(count, 1);
        assert_eq!(impacts[0].entity_a, 200);
        assert_eq!(impacts[0].entity_b, 201);
        assert_eq!(impacts[0].ram, 1);
        assert!(vehicles[0].state[1] < 6.0);
        assert!(vehicles[1].state[1] > 0.0);
        assert_eq!(vehicles[0].changed, 1);
        assert_eq!(vehicles[1].changed, 1);
        assert!((impacts[0].ram_energy_a + impacts[0].ram_energy_b
            - impacts[0].ram_energy).abs() < 1e-12);
    }
}
