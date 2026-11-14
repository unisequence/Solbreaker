//! Authoritative tracked-ground state and contact response.
use super::{vector, World};
use crate::bullet_api::{Contact, RayHit};
use crate::support::fit as fit_plane;
use serde_json::Value;
use std::collections::BTreeMap;
use std::rc::Rc;
use tfs_sim_core::drive::{spring_pose, terrain_height, Spring};
use tfs_sim_core::{DriveInput, Motion, Pose, Terrain};

pub struct Heightfield {
    pub heights: Vec<u16>,
    size: u32,
    origin: [f64; 2],
    dimensions: [f64; 2],
    vertical: [f64; 2],
}

impl Heightfield {
    pub fn new(value: &Value) -> Result<Self, String> {
        let size = value["size"].as_u64().ok_or("Missing terrain size")?;
        if size == 0 || size > 8192 {
            return Err("Invalid terrain size".into());
        }
        let heights = value["heights"]
            .as_array()
            .ok_or("Missing terrain heights")?
            .iter()
            .map(|v| {
                v.as_u64()
                    .and_then(|v| u16::try_from(v).ok())
                    .ok_or("Invalid height".to_string())
            })
            .collect::<Result<Vec<_>, _>>()?;
        if heights.len() != (size * size) as usize {
            return Err("Invalid terrain grid".into());
        }
        let dimensions = vector::<2>(&value["dimensions"])?;
        if dimensions.iter().any(|v| *v <= 0.) {
            return Err("Invalid terrain dimensions".into());
        }
        Ok(Self {
            heights,
            size: size as u32,
            origin: vector::<2>(&value["origin"])?,
            dimensions,
            vertical: vector::<2>(&value["vertical"])?,
        })
    }

    pub fn parameters(&self) -> Terrain {
        Terrain {
            heights: self.heights.as_ptr(),
            size: self.size,
            min_x: self.origin[0],
            min_z: self.origin[1],
            width: self.dimensions[0],
            depth: self.dimensions[1],
            height_min: self.vertical[0],
            height_range: self.vertical[1],
        }
    }

    pub fn height(&self, x: f64, z: f64) -> f64 {
        terrain_height(self.parameters(), &self.heights, x, z)
    }
}

#[derive(Clone)]
pub struct Track {
    low: f64,
    high: f64,
    z: Vec<f64>,
    y: Vec<f64>,
}

pub struct Configuration {
    a: Vec<f64>,
    b: Vec<f64>,
    p: Vec<f64>,
    weights: Vec<f64>,
    tracks: Vec<Track>,
    patch: f64,
    clearance: f64,
    slope: f64,
    support_span: f64,
    bounds: [f64; 4],
    components: Vec<([f64; 3], [f64; 3])>,
    track_links: Vec<i32>,
}

fn numbers(value: &Value) -> Result<Vec<f64>, String> {
    value
        .as_array()
        .ok_or("Expected numeric array")?
        .iter()
        .map(|v| {
            v.as_f64()
                .filter(|v| v.is_finite())
                .ok_or("Invalid ground parameter".into())
        })
        .collect()
}

impl Configuration {
    pub fn new(value: &Value) -> Result<Self, String> {
        let a = numbers(&value["a"])?;
        let b = numbers(&value["b"])?;
        let p = numbers(&value["p"])?;
        let weights = numbers(&value["weights"])?;
        if a.len() < 3
            || a.len() > 256
            || [b.len(), p.len(), weights.len()]
                .iter()
                .any(|n| *n != a.len())
            || weights.iter().any(|v| *v < 0.)
        {
            return Err("Invalid support layout".into());
        }
        let tracks = value["tracks"]
            .as_array()
            .ok_or("Missing tracks")?
            .iter()
            .map(|row| {
                let span = vector::<2>(&row["span"])?;
                let z = numbers(&row["z"])?;
                let y = numbers(&row["y"])?;
                if z.is_empty() || z.len() != y.len() || z.windows(2).any(|p| p[0] >= p[1]) {
                    return Err("Invalid track profile".into());
                }
                Ok(Track {
                    low: span[0],
                    high: span[1],
                    z,
                    y,
                })
            })
            .collect::<Result<Vec<_>, String>>()?;
        let components = value["components"]
            .as_array()
            .ok_or("Missing component bounds")?
            .iter()
            .map(|row| Ok((vector::<3>(&row[0])?, vector::<3>(&row[1])?)))
            .collect::<Result<Vec<_>, String>>()?;
        let track_links = value["track_links"]
            .as_array()
            .ok_or("Missing track links")?
            .iter()
            .map(|v| {
                v.as_i64()
                    .and_then(|v| i32::try_from(v).ok())
                    .ok_or("Invalid track link".into())
            })
            .collect::<Result<Vec<_>, String>>()?;
        let configuration = Self {
            a,
            b,
            p,
            weights,
            tracks,
            components,
            track_links,
            patch: value["patch"].as_f64().ok_or("Missing patch")?,
            clearance: value["clearance"].as_f64().ok_or("Missing clearance")?,
            slope: value["slope"].as_f64().ok_or("Missing climb angle")?,
            support_span: value["support_span"]
                .as_f64()
                .ok_or("Missing support span")?,
            bounds: vector::<4>(&value["bounds"])?,
        };
        if configuration.patch <= 0.
            || configuration.clearance < 0.
            || configuration.support_span <= 0.
            || configuration.slope <= 0.
            || configuration.slope >= std::f64::consts::FRAC_PI_2
            || configuration.bounds[0] >= configuration.bounds[2]
            || configuration.bounds[1] >= configuration.bounds[3]
            || configuration.tracks.is_empty()
            || configuration
                .tracks
                .iter()
                .any(|track| track.low > track.high)
            || configuration.components.is_empty()
            || configuration
                .components
                .iter()
                .any(|(low, high)| low.iter().zip(high).any(|(a, b)| a > b))
            || configuration.weights.iter().all(|v| *v == 0.)
        {
            return Err("Invalid native vehicle geometry".into());
        }
        Ok(configuration)
    }

    fn profile(&self, a: f64, b: f64, margin: f64) -> Option<f64> {
        for track in &self.tracks {
            if a < track.low - margin || a > track.high + margin {
                continue;
            }
            if b < track.z[0] - margin || b > track.z[track.z.len() - 1] + margin {
                return None;
            }
            if b.abs() <= self.patch {
                return Some(0.);
            }
            let i = track.z.partition_point(|z| *z < b);
            let value = if i == 0 {
                track.y[0]
            } else if i == track.z.len() {
                track.y[i - 1]
            } else {
                let fraction = (b - track.z[i - 1]) / (track.z[i] - track.z[i - 1]);
                track.y[i - 1] + fraction * (track.y[i] - track.y[i - 1])
            };
            return Some(value.max(0.));
        }
        None
    }
}

#[derive(Clone, Copy)]
struct Support {
    a: f64,
    b: f64,
    height: f64,
    compliant: bool,
}

#[derive(Clone)]
pub struct State {
    configuration: Rc<Configuration>,
    pose: Pose,
    road: Pose,
    motion: Motion,
    lean: [f64; 4],
    hints: Vec<usize>,
    supported: bool,
    spring: Spring,
    bounds_pose: Option<Pose>,
    rows: Vec<[f64; 3]>,
    lower: Vec<f64>,
    supports: Vec<(i32, f64, f64, f64)>,
    latch: Option<[f64; 4]>,
    blocked: bool,
}

impl State {
    pub fn new(configuration: Configuration) -> Self {
        Self {
            configuration: Rc::new(configuration),
            pose: Pose::default(),
            road: Pose::default(),
            motion: Motion::default(),
            lean: [0.; 4],
            hints: Vec::new(),
            supported: true,
            spring: Spring::default(),
            bounds_pose: None,
            rows: Vec::new(),
            lower: Vec::new(),
            supports: Vec::new(),
            latch: None,
            blocked: false,
        }
    }
}

pub fn rotation(pose: Pose) -> [[f64; 3]; 3] {
    let (cy, sy) = (pose.yaw.cos(), pose.yaw.sin());
    let (cp, sp) = (pose.pitch.cos(), pose.pitch.sin());
    let (cr, sr) = (pose.roll.cos(), pose.roll.sin());
    [
        [cy * cr + sy * sp * sr, -cy * sr + sy * sp * cr, sy * cp],
        [cp * sr, cp * cr, -sp],
        [-sy * cr + cy * sp * sr, sy * sr + cy * sp * cr, cy * cp],
    ]
}

fn transform(matrix: [[f64; 3]; 3], vector: [f64; 3]) -> [f64; 3] {
    matrix.map(|row| row[0] * vector[0] + row[1] * vector[1] + row[2] * vector[2])
}

fn local(matrix: [[f64; 3]; 3], vector: [f64; 3]) -> [f64; 3] {
    [0, 1, 2]
        .map(|i| matrix[0][i] * vector[0] + matrix[1][i] * vector[1] + matrix[2][i] * vector[2])
}

pub fn quaternion(pose: Pose) -> [f64; 4] {
    let (cy, sy) = ((pose.yaw / 2.).cos(), (pose.yaw / 2.).sin());
    let (cp, sp) = ((pose.pitch / 2.).cos(), (pose.pitch / 2.).sin());
    let (cr, sr) = ((pose.roll / 2.).cos(), (pose.roll / 2.).sin());
    [
        cy * sp * cr + sy * cp * sr,
        sy * cp * cr - cy * sp * sr,
        cy * cp * sr - sy * sp * cr,
        cy * cp * cr + sy * sp * sr,
    ]
}

fn position(pose: Pose) -> [f64; 3] {
    [pose.x, pose.y, pose.z]
}
fn difference(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn add(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

struct Movement<'a> {
    world: &'a mut World,
    entity: u32,
    state: State,
    constants: [f64; 8],
    effort: [f64; 3],
    contact_cache: BTreeMap<[u64; 6], Vec<Contact>>,
}

impl Movement<'_> {
    fn skin(&self) -> f64 {
        self.constants[0]
    }
    fn terrain(&self, x: f64, z: f64) -> f64 {
        self.world.heightfield.as_ref().unwrap().height(x, z)
    }

    fn ray(&self, start: [f64; 3], end: [f64; 3]) -> Result<RayHit, String> {
        Ok(self.world.cast(&[(start, end)])?[0])
    }

    fn support_reference(&self, reference: Pose, x: f64, z: f64) -> f64 {
        let matrix = rotation(reference);
        let (dx, dz) = (x - reference.x, z - reference.z);
        let rise = -(matrix[0][1] * dx + matrix[2][1] * dz) / matrix[1][1].max(0.05);
        let limit =
            (dx.hypot(dz) * self.state.configuration.slope.tan() - self.constants[3]).max(0.);
        reference.y + rise.clamp(0., limit)
    }

    fn sample(&self, xs: &[f64], zs: &[f64], reference: Pose) -> Result<Vec<f64>, String> {
        let reach = self.constants[3];
        let mut ground = Vec::with_capacity(xs.len());
        let mut rays = Vec::with_capacity(xs.len() * 2);
        let mut raised = Vec::new();
        let mut tops = Vec::new();
        let mut low = [f64::INFINITY; 3];
        let mut high = [f64::NEG_INFINITY; 3];
        for (&x, &z) in xs.iter().zip(zs) {
            let terrain = self.terrain(x, z);
            let top = terrain.max(reference.y) + reach;
            let start = terrain.max(self.support_reference(reference, x, z)) + reach;
            ground.push(terrain);
            tops.push(top);
            rays.push(([x, top, z], [x, terrain - 0.4, z]));
            if start > top {
                raised.push((ground.len() - 1, ([x, start, z], [x, terrain - 0.4, z])));
            }
            for (i, a, b) in [(0, x, x), (1, terrain - 0.4, top.max(start)), (2, z, z)] {
                low[i] = low[i].min(a - self.skin());
                high[i] = high[i].max(b + self.skin());
            }
        }
        let near = self.world.client.overlapping(low, high)?;
        if !near.iter().any(|row| {
            self.world
                .original_ids
                .get(&row.body)
                .is_some_and(|id| self.world.solid_map(*id))
        }) {
            return Ok(ground);
        }
        rays.extend(raised.iter().map(|row| row.1));
        let hits = self.world.cast(&rays)?;
        for (i, hit) in hits[..xs.len()].iter().enumerate() {
            if self.world.solid_map(hit.body) && hit.normal[1] > 0.65 {
                ground[i] = ground[i].max(hit.point[1]);
            }
        }
        for ((i, _), hit) in raised.iter().zip(&hits[xs.len()..]) {
            if self.world.solid_map(hit.body) && hit.normal[1] > 0.65 && hit.point[1] > tops[*i] {
                ground[*i] = ground[*i].max(hit.point[1]);
            }
        }
        Ok(ground)
    }

    fn incline(
        &self,
        pose: Pose,
        point: Contact,
        crest: bool,
    ) -> Result<Option<([f64; 3], [f64; 3], f64)>, String> {
        let config = &self.state.configuration;
        if !config.track_links.contains(&point.link_a) || !self.world.solid_map(point.body_b) {
            return Ok(None);
        }
        let (px, py, pz) = (point.point_b[0], point.point_b[1], point.point_b[2]);
        let (hx, hz) = (pose.yaw.sin() * self.skin(), pose.yaw.cos() * self.skin());
        let mut found = None;
        for (ox, oz) in [(0., 0.), (-hx, -hz), (hx, hz)] {
            let hit = self.ray(
                [px + ox, py + 2. * self.constants[1], pz + oz],
                [px + ox, py - 2. * self.constants[1], pz + oz],
            )?;
            if self.world.solid_map(hit.body)
                && hit.fraction > 0.
                && hit.normal[1] >= config.slope.cos()
                && (hit.normal[1] < 0.9999 || crest)
                && dot(difference(point.point_b, hit.point), hit.normal).abs() <= self.skin()
            {
                found = Some(hit);
                break;
            }
        }
        let Some(hit) = found else {
            return Ok(None);
        };
        let matrix = rotation(pose);
        let offset = local(matrix, difference(point.point_a, position(pose)));
        let Some(profile) = config.profile(offset[0], offset[2], self.skin()) else {
            return Ok(None);
        };
        let floor = add(
            transform(matrix, [offset[0], profile, offset[2]]),
            position(pose),
        );
        Ok(Some((
            hit.normal,
            floor,
            dot(difference(hit.point, floor), hit.normal),
        )))
    }

    fn contacts(&mut self, pose: Pose) -> Result<Vec<Contact>, String> {
        let original = self.world.entities[&self.entity];
        self.world
            .set_pose(original, position(pose), quaternion(pose), None)?;
        let key = [pose.x, pose.y, pose.z, pose.yaw, pose.pitch, pose.roll].map(f64::to_bits);
        let raw = if let Some(points) = self.contact_cache.get(&key) {
            points.clone()
        } else {
            let points = self.world.contact_points(self.entity, self.skin())?;
            if self.contact_cache.len() < 128 {
                self.contact_cache.insert(key, points.clone());
            }
            points
        };
        let mut result = Vec::new();
        for point in raw {
            let map = self.world.map_bodies.contains(&point.body_b);
            if point.distance >= self.skin()
                || (map && point.normal[1] > 0.65 && point.distance > -0.025)
            {
                continue;
            }
            if self.state.configuration.track_links.contains(&point.link_a)
                && self.world.solid_map(point.body_b)
            {
                if point.normal[1] > 0.65 && point.distance > -self.constants[4] - self.skin() {
                    continue;
                }
                let matrix = rotation(pose);
                let offset = local(matrix, difference(point.point_a, position(pose)));
                if let Some(profile) =
                    self.state
                        .configuration
                        .profile(offset[0], offset[2], self.skin())
                {
                    let floor = transform(matrix, [offset[0], profile, offset[2]])[1] + pose.y;
                    if self
                        .incline(pose, point, false)?
                        .is_some_and(|(_, _, depth)| depth <= self.constants[4] + self.skin())
                    {
                        continue;
                    }
                    if self.state.supports.iter().any(|&(body, x, z, height)| {
                        body == point.body_b
                            && (x - point.point_a[0]).hypot(z - point.point_a[2])
                                <= self.constants[1] * 2. + self.skin()
                            && height - floor >= -self.skin()
                            && height - floor <= self.constants[4] + self.skin()
                            && point.point_a[1] <= height + self.skin()
                    }) {
                        continue;
                    }
                }
            }
            result.push(point);
        }
        Ok(result)
    }

    fn inside(&self, pose: Pose) -> bool {
        let matrix = rotation(pose);
        let [lx, lz, hx, hz] = self.state.configuration.bounds;
        self.state
            .configuration
            .components
            .iter()
            .all(|(low, high)| {
                [low[0], high[0]].into_iter().all(|x| {
                    [low[2], high[2]].into_iter().all(|z| {
                        let value = add(transform(matrix, [x, 0., z]), position(pose));
                        value[0] >= lx + self.skin()
                            && value[0] <= hx - self.skin()
                            && value[2] >= lz + self.skin()
                            && value[2] <= hz - self.skin()
                    })
                })
            })
    }

    fn valid(
        &mut self,
        candidate: Pose,
        previous: Pose,
        climb: bool,
    ) -> Result<(bool, Vec<Contact>), String> {
        if !self.inside(candidate) {
            return Ok((false, Vec::new()));
        }
        let rise = candidate.y - previous.y;
        if climb && rise > 0.0001 {
            let horizontal = (candidate.x - previous.x).hypot(candidate.z - previous.z);
            let layout = &self.state.configuration;
            let span = layout.support_span.min(
                self.world.heightfield.as_ref().unwrap().dimensions[0]
                    / self.world.heightfield.as_ref().unwrap().size as f64,
            );
            if rise > layout.slope.tan() * horizontal.max(span * 0.5) {
                return Ok((false, Vec::new()));
            }
        }
        let (dx, dz) = (candidate.x - previous.x, candidate.z - previous.z);
        let distance = dx.hypot(dz);
        let points = self
            .contacts(candidate)?
            .into_iter()
            .filter(|point| {
                let approach = dx * point.normal[0] + dz * point.normal[2];
                !(point.distance >= -self.skin() * 3.
                    && approach >= -(distance * 0.1).max(1e-6)
                    && point.normal[0].hypot(point.normal[2]) > 0.1)
                    && !(self.state.configuration.track_links.contains(&point.link_a)
                        && self.world.solid_map(point.body_b)
                        && point.distance >= -self.constants[4] - self.skin()
                        && approach > 1e-8)
            })
            .collect::<Vec<_>>();
        Ok((points.is_empty(), points))
    }

    fn cached_supports(&mut self, x: f64, z: f64, yaw: f64) -> Vec<Support> {
        let config = &self.state.configuration;
        let inner = config
            .tracks
            .iter()
            .map(|track| track.low.abs().min(track.high.abs()))
            .fold(f64::INFINITY, f64::min);
        let (c, s) = (yaw.cos(), yaw.sin());
        let mut rows = Vec::new();
        self.state.supports.retain(|&(body, wx, wz, height)| {
            if !self.world.solid_map(body) {
                return false;
            }
            let (a, b) = (c * (wx - x) - s * (wz - z), s * (wx - x) + c * (wz - z));
            let profile = config.profile(a, b, self.constants[0]);
            if profile.is_none() && (a.abs() >= inner || b.abs() > config.patch + self.constants[0])
            {
                return false;
            }
            rows.push(Support {
                a,
                b,
                height: height - profile.unwrap_or(config.clearance),
                compliant: profile.is_some(),
            });
            true
        });
        rows
    }

    fn support(
        &mut self,
        x: f64,
        z: f64,
        yaw: f64,
        reference: Pose,
        start: Pose,
        h: f64,
        extra: &[Support],
    ) -> Result<(Pose, Pose), String> {
        let mut supports = self.cached_supports(x, z, yaw);
        supports.extend_from_slice(extra);
        let config = Rc::clone(&self.state.configuration);
        let (c, s) = (yaw.cos(), yaw.sin());
        let xs: Vec<_> = config
            .a
            .iter()
            .zip(&config.b)
            .map(|(a, b)| x + c * a + s * b)
            .collect();
        let zs: Vec<_> = config
            .a
            .iter()
            .zip(&config.b)
            .map(|(a, b)| z - s * a + c * b)
            .collect();
        let ground = self.sample(&xs, &zs, reference)?;
        let relief: Vec<_> = ground.iter().zip(&config.p).map(|(g, p)| g - p).collect();
        let mut carrying = start;
        carrying.pitch -= self.state.lean[0];
        carrying.roll -= self.state.lean[1];
        let up = rotation(carrying);
        let mut weights = config.weights.clone();
        let low = relief
            .iter()
            .copied()
            .chain(supports.iter().map(|row| row.height))
            .fold(f64::INFINITY, f64::min);
        let high = relief
            .iter()
            .copied()
            .chain(supports.iter().map(|row| row.height))
            .fold(f64::NEG_INFINITY, f64::max);
        if high - low > self.constants[3] + self.skin() {
            for i in 0..weights.len() {
                let prediction = carrying.y
                    - (up[0][1] * (xs[i] - carrying.x) + up[2][1] * (zs[i] - carrying.z))
                        / up[1][1].max(0.05);
                if relief[i] < prediction - self.constants[3] {
                    weights[i] = 0.;
                }
            }
        }
        let active: Vec<_> = weights
            .iter()
            .enumerate()
            .filter(|(_, w)| **w > 0.)
            .map(|(i, _)| i)
            .collect();
        self.state.supported = active.iter().any(|&i| config.a[i] <= 0.05)
            && active.iter().any(|&i| config.a[i] >= -0.05)
            && active.iter().any(|&i| config.b[i] <= 0.05)
            && active.iter().any(|&i| config.b[i] >= -0.05);
        let rank_three = active.first().is_some_and(|&i| {
            active
                .iter()
                .find(|&&j| (config.a[j] - config.a[i]).hypot(config.b[j] - config.b[i]) > 1e-12)
                .is_some_and(|&j| {
                    active.iter().any(|&k| {
                        ((config.a[j] - config.a[i]) * (config.b[k] - config.b[i])
                            - (config.a[k] - config.a[i]) * (config.b[j] - config.b[i]))
                            .abs()
                            > 1e-12
                    })
                })
        });
        let target = if !rank_three {
            self.state.bounds_pose = None;
            Pose {
                x,
                y: carrying.y
                    - (up[0][1] * (x - carrying.x) + up[2][1] * (z - carrying.z))
                        / up[1][1].max(0.05),
                z,
                yaw,
                pitch: carrying.pitch,
                roll: carrying.roll,
            }
        } else {
            let mut rows: Vec<_> = config
                .a
                .iter()
                .zip(&config.b)
                .map(|(&a, &b)| [1., a, b])
                .collect();
            rows.push([1., 0., 0.]);
            let mut lower: Vec<_> = relief.iter().map(|g| g - self.constants[4]).collect();
            lower.push(self.terrain(x, z) - config.clearance + self.skin());
            let mut values = ground;
            values.push(0.);
            weights.push(0.);
            for support in &supports {
                let bound = support.height
                    - if support.compliant { self.constants[4] } else { 0. };
                // The cached and fresh contact can describe the same patch.
                // Near-identical constraint normals make the active set singular.
                if let Some(index) = (config.a.len() + 1..rows.len()).find(|&i| {
                    (rows[i][1] - support.a).hypot(rows[i][2] - support.b) <= 1e-5
                }) {
                    lower[index] = lower[index].max(bound);
                    continue;
                }
                rows.push([1., support.a, support.b]);
                lower.push(bound);
                values.push(0.);
                weights.push(0.);
            }
            let fit = fit_plane(&rows, &values, &weights, &lower, &self.state.hints)
                .ok_or("Native support fit failed")?;
            self.state.hints = fit
                .active
                .into_iter()
                .filter(|&i| i < config.a.len())
                .collect();
            let target = Pose {
                x,
                y: fit.plane[0],
                z,
                yaw,
                pitch: -fit.plane[2].atan(),
                roll: (fit.plane[1] / (1. + fit.plane[2] * fit.plane[2]).sqrt()).atan(),
            };
            self.state.rows = rows;
            self.state.lower = lower;
            self.state.bounds_pose = Some(target);
            target
        };
        let mut spring = spring_pose(
            start,
            target,
            self.state.road,
            h,
            self.state.motion,
            self.state.lean,
            self.effort,
            self.constants,
        );
        let actual = rotation(spring.pose);
        let base = rotation(Pose {
            pitch: spring.pose.pitch - spring.lean[0],
            roll: spring.pose.roll - spring.lean[1],
            ..spring.pose
        });
        for support in supports {
            let profile = if support.compliant {
                config.profile(support.a, support.b, self.skin())
            } else {
                Some(config.clearance)
            };
            if let Some(profile) = profile {
                let offset = transform(actual, [support.a, profile, support.b]);
                let base_offset = transform(base, [support.a, profile, support.b]);
                let compression = if support.compliant {
                    self.constants[4]
                } else {
                    0.
                };
                let needed = support.height + profile - offset[1] - compression - spring.pose.y;
                spring.pose.y += needed.max(0.).min((base_offset[1] - offset[1]).max(0.));
            }
        }
        self.state.spring = spring;
        Ok((spring.pose, target))
    }

    fn contact_supports(
        &mut self,
        candidate: Pose,
        points: &[Contact],
        reference: Pose,
    ) -> Result<Vec<Support>, String> {
        let config = Rc::clone(&self.state.configuration);
        let inner = config
            .tracks
            .iter()
            .map(|track| track.low.abs().min(track.high.abs()))
            .fold(f64::INFINITY, f64::min);
        let (c, s) = (candidate.yaw.cos(), candidate.yaw.sin());
        let mut sorted = points.to_vec();
        sorted.sort_by(|a, b| a.distance.total_cmp(&b.distance));
        let mut found: Vec<Support> = Vec::new();
        for point in sorted.into_iter().take(8) {
            if !self.world.solid_map(point.body_b) {
                continue;
            }
            let (a, b) = (
                c * (point.point_a[0] - candidate.x) - s * (point.point_a[2] - candidate.z),
                s * (point.point_a[0] - candidate.x) + c * (point.point_a[2] - candidate.z),
            );
            let profile = config.profile(a, b, self.skin());
            if profile.is_none() && (a.abs() >= inner || b.abs() > config.patch + self.skin()) {
                continue;
            }
            let mut x = point.point_b[0];
            let mut z = point.point_b[2];
            let length = point.normal[0].hypot(point.normal[2]);
            if length > 1e-6 {
                x -= point.normal[0] / length * self.constants[1];
                z -= point.normal[2] / length * self.constants[1];
            }
            let terrain = self.terrain(x, z);
            let top = terrain.max(reference.y) + self.constants[3];
            let mut hit = self.ray([x, top, z], [x, terrain - 0.4, z])?;
            let start = terrain.max(self.support_reference(reference, x, z)) + self.constants[3];
            if start > top {
                let higher = self.ray([x, start, z], [x, terrain - 0.4, z])?;
                if self.world.solid_map(higher.body)
                    && higher.normal[1] > 0.65
                    && higher.fraction > 0.
                    && higher.point[1] > top
                {
                    hit = higher;
                }
            }
            if !self.world.solid_map(hit.body) || hit.normal[1] <= 0.65 || hit.fraction <= 0. {
                continue;
            }
            let row = Support {
                a,
                b,
                height: hit.point[1] - profile.unwrap_or(config.clearance),
                compliant: profile.is_some(),
            };
            if found
                .iter()
                .any(|other| (a - other.a).hypot(b - other.b) <= 1e-3)
            {
                continue;
            }
            found.push(row);
            let (wx, wz) = (candidate.x + c * a + s * b, candidate.z - s * a + c * b);
            self.state
                .supports
                .retain(|&(body, x, z, _)| body != point.body_b || (x - wx).hypot(z - wz) > 0.10);
            self.state
                .supports
                .push((point.body_b, wx, wz, hit.point[1]));
            if self.state.supports.len() > 16 {
                self.state.supports.remove(0);
            }
        }
        Ok(found)
    }

    fn bottomed(
        &mut self,
        candidate: Pose,
        points: &[Contact],
        dx: f64,
        dz: f64,
        road: Pose,
    ) -> Result<Option<Pose>, String> {
        if self.state.bounds_pose != Some(road)
            || self.state.spring.pose != candidate
            || points.is_empty()
        {
            return Ok(None);
        }
        let (c, s) = (candidate.yaw.cos(), candidate.yaw.sin());
        let slope_z = -candidate.pitch.tan();
        let slope_x = candidate.roll.tan() * (1. + slope_z * slope_z).sqrt();
        let u = [candidate.y, slope_x, slope_z];
        let up = rotation(candidate);
        let mut extra = Vec::new();
        for &point in points {
            let Some((normal, floor, depth)) = self.incline(candidate, point, true)? else {
                return Ok(None);
            };
            let excess = depth - self.constants[4];
            if excess <= 0. {
                continue;
            }
            let approach = if normal[1] < 0.9999 {
                dx * normal[0] + dz * normal[2]
            } else {
                dx * up[0][1] + dz * up[2][1]
            };
            if excess > self.constants[4] || approach >= -1e-8 {
                return Ok(None);
            }
            let row = [
                1.,
                c * (floor[0] - candidate.x) - s * (floor[2] - candidate.z),
                s * (floor[0] - candidate.x) + c * (floor[2] - candidate.z),
            ];
            extra.push((row, dot(row, u) + excess / normal[1]));
        }
        if extra.is_empty() {
            return Ok(None);
        }
        let mut rows = self.state.rows.clone();
        let heights: Vec<_> = rows.iter().map(|&row| dot(row, u)).collect();
        let mut lower: Vec<_> = self
            .state
            .lower
            .iter()
            .zip(&heights)
            .map(|(a, b)| a.min(*b))
            .collect();
        let mut weight = vec![0.; rows.len() + extra.len()];
        weight[..self.state.configuration.weights.len()]
            .copy_from_slice(&self.state.configuration.weights);
        let mut target = heights;
        for (row, limit) in extra {
            rows.push(row);
            lower.push(limit);
            target.push(0.);
        }
        let fit =
            fit_plane(&rows, &target, &weight, &lower, &[]).ok_or("Native bump stop fit failed")?;
        let pose = Pose {
            y: fit.plane[0],
            pitch: -fit.plane[2].atan(),
            roll: (fit.plane[1] / (1. + fit.plane[2] * fit.plane[2]).sqrt()).atan(),
            ..candidate
        };
        self.state.spring.pose = pose;
        Ok(Some(pose))
    }

    fn bottom(
        &mut self,
        candidate: Pose,
        points: Vec<Contact>,
        dx: f64,
        dz: f64,
        road: Pose,
        previous: Pose,
    ) -> Result<(Pose, bool, Vec<Contact>), String> {
        let mut pose = candidate;
        let mut more = points.clone();
        for _ in 0..2 {
            let Some(held) = self.bottomed(pose, &more, dx, dz, road)? else {
                break;
            };
            pose = held;
            let (clear, contacts) = self.valid(pose, previous, true)?;
            if clear {
                return Ok((pose, true, Vec::new()));
            }
            more = contacts;
        }
        Ok((candidate, false, points))
    }

    /// Try the measured top when the spring lags a low, sharp riser.
    /// The normal contact, climb and border checks still own the result.
    fn low_step(
        &mut self,
        candidate: Pose,
        previous: Pose,
        points: &[Contact],
        supports: &[Support],
    ) -> Result<Option<Pose>, String> {
        if points.is_empty()
            || (candidate.x - previous.x).hypot(candidate.z - previous.z) <= 1e-8
            || points.iter().any(|point| !self.world.solid_map(point.body_b))
        {
            return Ok(None);
        }
        // A convex contact at the seam can land just outside the track
        // profile, leaving no support row. Re-probe the actual riser from
        // the direction of travel, then require a measured low top.
        let mut measured = 0_f64;
        let dx = candidate.x - previous.x;
        let dz = candidate.z - previous.z;
        let distance = dx.hypot(dz);
        let advance = self.constants[1];
        for point in points.iter().take(8) {
            // The leading track can reach the top before its support samples.
            // Use the measured compression when the contact already lies on
            // a low, upward-facing surface.
            let top = point.point_b[1] - self.terrain(point.point_b[0], point.point_b[2]);
            if self.state.configuration.track_links.contains(&point.link_a)
                && point.normal[1] >= self.state.configuration.slope.cos()
                && top > self.skin()
                && top <= self.constants[3]
            {
                let excess = (-point.distance - self.constants[4]).max(0.);
                measured = measured.max(excess / point.normal[1] + self.skin());
                continue;
            }
            if dx * point.normal[0] + dz * point.normal[2] >= -1e-8 {
                continue;
            }
            let x = point.point_b[0] + advance * dx / distance;
            let z = point.point_b[2] + advance * dz / distance;
            let ground = self.terrain(x, z);
            let ceiling = ground + self.constants[3];
            let hit = self.ray([x, ceiling, z], [x, ground - 0.1, z])?;
            if self.world.solid_map(hit.body)
                && hit.normal[1] >= self.state.configuration.slope.cos()
                && hit.fraction > 0.
                && hit.point[1] > ground + self.skin()
                && hit.point[1] >= point.point_b[1] - self.skin()
            {
                measured = measured.max(hit.point[1] - candidate.y + self.skin());
            }
        }
        let matrix = rotation(candidate);
        let config = &self.state.configuration;
        let mut rise: f64 = 0.;
        for support in supports {
            let profile = if support.compliant {
                config.profile(support.a, support.b, self.skin())
            } else {
                Some(config.clearance)
            };
            if let Some(profile) = profile {
                let offset = transform(matrix, [support.a, profile, support.b]);
                rise = rise.max(support.height + profile - candidate.y - offset[1] + self.skin());
            }
        }
        let limit = self.constants[4] + 3. * self.skin();
        if measured <= 0. && (rise <= 0. || rise > limit) {
            return Ok(None);
        }
        let rise = rise.max(measured).min(limit);
        for clearance in [rise, (rise + self.skin()).min(limit), limit] {
            let lifted = Pose {
                y: candidate.y + clearance,
                ..candidate
            };
            if self.valid(lifted, previous, true)?.0 {
                self.state.spring.pose = lifted;
                self.state.spring.velocity[0] = 0.;
                return Ok(Some(lifted));
            }
        }
        Ok(None)
    }

    fn retreat(
        &mut self,
        previous: Pose,
        points: &[Contact],
        dx: f64,
        dz: f64,
    ) -> Result<Option<Pose>, String> {
        let config = Rc::clone(&self.state.configuration);
        if points.is_empty()
            || points.iter().any(|point| {
                !config.track_links.contains(&point.link_a)
                    || !self.world.solid_map(point.body_b)
                    || dx * point.normal[0] + dz * point.normal[2] <= 1e-8
            })
        {
            return Ok(None);
        }
        let held = Pose {
            x: previous.x + dx,
            z: previous.z + dz,
            ..previous
        };
        if !self.valid(held, previous, false)?.0 {
            return Ok(None);
        }
        let matrix = rotation(held);
        let stations: Vec<_> = config
            .a
            .iter()
            .zip(&config.p)
            .zip(&config.b)
            .map(|((&a, &p), &b)| transform(matrix, [a, p, b]))
            .collect();
        let xs: Vec<_> = stations.iter().map(|v| held.x + v[0]).collect();
        let zs: Vec<_> = stations.iter().map(|v| held.z + v[2]).collect();
        let ground = self.sample(&xs, &zs, held)?;
        let active: Vec<_> = config
            .weights
            .iter()
            .enumerate()
            .filter(|(i, w)| **w > 0. && ground[*i] >= held.y + stations[*i][1] - self.constants[3])
            .map(|(i, _)| i)
            .collect();
        let supported = active.iter().any(|&i| config.a[i] <= 0.05)
            && active.iter().any(|&i| config.a[i] >= -0.05)
            && active.iter().any(|&i| config.b[i] <= 0.05)
            && active.iter().any(|&i| config.b[i] >= -0.05);
        Ok(supported.then_some(held))
    }

    fn held(
        &self,
        x: f64,
        z: f64,
        yaw: f64,
        pitch: f64,
        roll: f64,
        reference: f64,
    ) -> Result<Pose, String> {
        let config = &self.state.configuration;
        let matrix = rotation(Pose {
            x,
            y: reference,
            z,
            yaw,
            pitch,
            roll,
        });
        let stations: Vec<_> = config
            .a
            .iter()
            .zip(&config.p)
            .zip(&config.b)
            .map(|((&a, &p), &b)| transform(matrix, [a, p, b]))
            .collect();
        let xs: Vec<_> = stations.iter().map(|v| x + v[0]).collect();
        let zs: Vec<_> = stations.iter().map(|v| z + v[2]).collect();
        let samples = self.sample(
            &xs,
            &zs,
            Pose {
                x,
                y: reference,
                z,
                yaw,
                pitch: 0.,
                roll: 0.,
            },
        )?;
        let y = samples
            .iter()
            .zip(stations)
            .map(|(g, p)| g - p[1])
            .fold(self.terrain(x, z), f64::max);
        Ok(Pose {
            x,
            y,
            z,
            yaw,
            pitch,
            roll,
        })
    }

    fn detached(&self, start: Pose, pose: Pose, road: Pose, h: f64, vy: f64) -> bool {
        let terrain = self.terrain(pose.x, pose.z);
        if !self.state.supported && start.y - terrain > self.constants[3] {
            return true;
        }
        let ballistic = start.y + vy * h - 0.5 * self.constants[2] * h * h;
        if ballistic.max(pose.y) - road.y <= self.constants[4] + self.skin() {
            return false;
        }
        let old = self.terrain(start.x, start.z);
        if terrain > old + 1e-4 {
            return false;
        }
        start.y - terrain > self.constants[3]
            || old - terrain > 0.15 * (pose.x - start.x).hypot(pose.z - start.z) + self.skin()
    }

    fn slide(dx: f64, dz: f64, points: &[Contact]) -> (f64, f64) {
        let (mut x, mut z) = (dx, dz);
        let mut ordered = points.to_vec();
        ordered.sort_by(|a, b| a.distance.total_cmp(&b.distance));
        for point in ordered.iter().take(4) {
            let (nx, nz) = (point.normal[0], point.normal[2]);
            let squared = nx * nx + nz * nz;
            let toward = x * nx + z * nz;
            if squared > 0.01 && toward < 0. {
                let into = toward / squared;
                x -= into * nx;
                z -= into * nz;
            }
        }
        (x, z)
    }

    fn wall_impact(
        &self,
        points: &[Contact],
        vx: f64,
        vz: f64,
        reference: f64,
    ) -> Result<Option<[f64; 4]>, String> {
        if points
            .iter()
            .any(|point| !self.world.solid_map(point.body_b))
        {
            return Ok(None);
        }
        let speed = vx.hypot(vz);
        if speed < 1.5 {
            return Ok(None);
        }
        let (mut nx, mut nz) = (0., 0.);
        for point in points {
            if point.normal[0].hypot(point.normal[2]) > 0.7
                && vx * point.normal[0] + vz * point.normal[2] < 0.
            {
                nx += point.normal[0];
                nz += point.normal[2];
            }
        }
        let length = nx.hypot(nz);
        if length < 1e-6 {
            return Ok(None);
        }
        nx /= length;
        nz /= length;
        let closing = -(vx * nx + vz * nz);
        if closing < 1.5 || closing < 0.5 * speed {
            return Ok(None);
        }
        let mut nearest = points.to_vec();
        nearest.sort_by(|a, b| a.distance.total_cmp(&b.distance));
        for point in nearest.iter().take(4) {
            let floor = self
                .terrain(point.point_b[0], point.point_b[2])
                .max(reference)
                + self.constants[3];
            if point.point_b[1] > floor + self.skin() {
                return Ok(Some([nx, nz, closing, speed]));
            }
            let y = floor + self.skin();
            let hit = self.ray(
                [point.point_b[0] + nx * 0.3, y, point.point_b[2] + nz * 0.3],
                [point.point_b[0] - nx * 0.1, y, point.point_b[2] - nz * 0.1],
            )?;
            if self.world.solid_map(hit.body) {
                return Ok(Some([nx, nz, closing, speed]));
            }
        }
        Ok(None)
    }

    fn road_velocity(road: Pose, vx: f64, vz: f64) -> [f64; 3] {
        let up = rotation(road);
        [
            vx,
            -(up[0][1] * vx + up[2][1] * vz) / up[1][1].max(0.05),
            vz,
        ]
    }

    fn record(&mut self, pose: Pose, points: &[Contact]) {
        let points = points
            .iter()
            .filter(|point| {
                self.world
                    .entities
                    .iter()
                    .any(|(&entity, &body)| entity != self.entity && body == point.body_b)
            })
            .copied()
            .collect::<Vec<_>>();
        if !points.is_empty() {
            self.world.contact_events.push((self.entity, pose, points));
        }
    }

    fn valid_impact(
        &mut self,
        candidate: Pose,
        previous: Pose,
        impact: bool,
        dx: f64,
        dz: f64,
    ) -> Result<(bool, Vec<Contact>), String> {
        let (clear, points) = self.valid(candidate, previous, true)?;
        if clear || !impact {
            return Ok((clear, points));
        }
        let keys: Vec<_> = self
            .world
            .breakables
            .iter()
            .filter(|(_, bodies)| points.iter().any(|point| bodies.contains(&point.body_b)))
            .map(|(key, _)| key.clone())
            .collect();
        if keys.is_empty() {
            return Ok((false, points));
        }
        for key in keys {
            self.world.destroy_object(&key)?;
            self.world.destroyed_events.push((key, dx, dz));
        }
        self.contact_cache.clear();
        let points = self.contacts(candidate)?;
        Ok((points.is_empty(), points))
    }

    fn angular(previous: Pose, current: Pose, dt: f64) -> [f64; 3] {
        let a = rotation(previous);
        let b = rotation(current);
        let mut relative = [[0.; 3]; 3];
        for i in 0..3 {
            for j in 0..3 {
                relative[i][j] = dot(b[i], a[j]);
            }
        }
        let vector = [
            relative[2][1] - relative[1][2],
            relative[0][2] - relative[2][0],
            relative[1][0] - relative[0][1],
        ];
        let sine = 0.5 * dot(vector, vector).sqrt();
        let cosine = ((relative[0][0] + relative[1][1] + relative[2][2] - 1.) * 0.5).clamp(-1., 1.);
        let angle = sine.atan2(cosine);
        if angle <= 1e-10 || sine <= 1e-10 {
            return [0.; 3];
        }
        let mut velocity = vector.map(|value| value * (angle / (2. * sine * dt)));
        let magnitude = dot(velocity, velocity).sqrt();
        if magnitude > 2. {
            velocity = velocity.map(|value| value * 2. / magnitude);
        }
        velocity
    }

    fn run(&mut self, input: DriveInput, seed: &super::StepInput) -> Result<Outcome, String> {
        if self.state.pose != input.pose {
            self.state.hints.clear();
            self.state.bounds_pose = None;
        }
        // Python flight and landing can replace these mirrored caches between calls.
        self.state.supports = seed.supports[..seed.support_count as usize]
            .iter()
            .filter(|row| self.world.solid_map(row.body))
            .map(|row| (row.body, row.x, row.z, row.height))
            .collect();
        self.state.latch = (seed.has_latch != 0).then_some(seed.latch);
        self.state.supported = seed.carrying_supported != 0;
        self.state.pose = input.pose;
        self.state.road = input.road;
        self.state.motion = input.motion;
        self.state.lean = input.lean;
        self.state.blocked = input.was_blocked != 0;
        let travel = if input.has_displacement != 0 {
            input.displacement[0].hypot(input.displacement[1])
        } else {
            input.distance.abs()
        };
        let slices = (travel / self.constants[1])
            .ceil()
            .max((input.yaw_delta.abs() / 0.75_f64.to_radians()).ceil())
            .max(1.);
        if !slices.is_finite() || slices > 512. {
            return Err("Invalid native movement subdivision".into());
        }
        let steps = slices as usize;
        let h = input.dt / slices;
        let mut outcome = Outcome::default();
        let mut pose = input.pose;
        if self
            .state
            .latch
            .is_some_and(|latch| (pose.x - latch[0]).hypot(pose.z - latch[1]) > 0.25)
        {
            self.state.latch = None;
        }
        if travel == 0.
            && input.yaw_delta == 0.
            && self.state.motion.suspension.iter().all(|v| v.abs() < 0.001)
            && self.effort.iter().all(|v| v.abs() <= 0.01)
        {
            self.state.motion.velocity = [0.; 3];
            self.state.motion.angular = [0.; 2];
            return Ok(outcome);
        }
        let mut curvature_scale = 1.;
        let mut wall_hit = None;
        for step in 0..steps {
            let start = pose;
            let old_road = self.state.road;
            let mut previous = pose;
            let mut yaw = (previous.yaw + input.yaw_delta / slices + std::f64::consts::PI)
                .rem_euclid(std::f64::consts::TAU)
                - std::f64::consts::PI;
            let mut carrying = previous;
            carrying.pitch -= self.state.lean[0];
            carrying.roll -= self.state.lean[1];
            let (rotated, _) = self.support(start.x, start.z, yaw, carrying, start, h, &[])?;
            let (valid, points) = self.valid(rotated, previous, true)?;
            if valid {
                previous = rotated;
            } else {
                self.record(rotated, &points);
                yaw = previous.yaw;
            }
            let (mut dx, mut dz) = if input.has_displacement != 0 {
                (
                    input.displacement[0] / slices * curvature_scale,
                    input.displacement[1] / slices * curvature_scale,
                )
            } else {
                (
                    yaw.sin() * input.distance / slices * curvature_scale,
                    yaw.cos() * input.distance / slices * curvature_scale,
                )
            };
            let mut incoming = Self::road_velocity(old_road, dx / h, dz / h);
            let impact = dx.hypot(dz) > 1e-8;
            let mut reference = previous;
            reference.pitch -= self.state.lean[0];
            reference.roll -= self.state.lean[1];
            let (mut candidate, mut road) =
                self.support(start.x + dx, start.z + dz, yaw, reference, start, h, &[])?;
            let (mut valid, mut points) = self.valid_impact(candidate, previous, impact, dx, dz)?;
            let mut extra = Vec::new();
            for _ in 0..2 {
                if valid || points.is_empty() {
                    break;
                }
                let found = self.contact_supports(candidate, &points, reference)?;
                if found.is_empty() {
                    break;
                }
                extra.extend(found);
                let (stepped, target) =
                    self.support(start.x + dx, start.z + dz, yaw, reference, start, h, &extra)?;
                road = target;
                let (clear, more) = self.valid_impact(stepped, previous, impact, dx, dz)?;
                if clear {
                    candidate = stepped;
                    valid = true;
                    points.clear();
                } else if !more.is_empty() {
                    candidate = stepped;
                    points = more;
                }
            }
            if !valid && !points.is_empty() {
                (candidate, valid, points) =
                    self.bottom(candidate, points, dx, dz, road, previous)?;
                if !valid {
                    if let Some(held) = self.low_step(candidate, previous, &points, &extra)? {
                        candidate = held;
                        valid = true;
                        points.clear();
                    }
                }
            }
            let mut retreating = false;
            if !valid && !points.is_empty() {
                if let Some(held) = self.retreat(previous, &points, dx, dz)? {
                    candidate = held;
                    valid = true;
                    points.clear();
                    road = Pose {
                        pitch: old_road.pitch,
                        roll: old_road.roll,
                        ..held
                    };
                    retreating = true;
                }
            }
            if !valid && !points.is_empty() {
                self.record(candidate, &points);
                outcome.contact = if points.iter().any(|point| {
                    self.world
                        .entities
                        .values()
                        .any(|&body| body == point.body_b)
                }) {
                    2
                } else {
                    1
                };
                if wall_hit.is_none() {
                    wall_hit = self.wall_impact(&points, dx / h, dz / h, previous.y)?;
                }
                (dx, dz) = Self::slide(dx, dz, &points);
                if dx.hypot(dz) < travel / slices * 0.02 {
                    dx = 0.;
                    dz = 0.;
                }
                (candidate, road) =
                    self.support(start.x + dx, start.z + dz, yaw, reference, start, h, &extra)?;
                (valid, points) = self.valid(candidate, previous, true)?;
            }
            if !valid && points.is_empty() {
                let held = self.held(
                    start.x + dx,
                    start.z + dz,
                    yaw,
                    previous.pitch,
                    previous.roll,
                    previous.y,
                )?;
                if held.y <= previous.y + self.skin() && self.valid(held, previous, false)?.0 {
                    valid = true;
                    candidate = held;
                    road = held;
                }
            }
            if valid {
                incoming = Self::road_velocity(old_road, dx / h, dz / h);
                if dx.hypot(dz) > 1e-8 && !self.detached(start, candidate, road, h, incoming[1]) {
                    let tangent = Self::road_velocity(road, dx / h, dz / h);
                    let factor =
                        (dot(incoming, incoming) / dot(tangent, tangent).max(1e-12)).sqrt();
                    curvature_scale *= factor;
                    dx *= factor;
                    dz *= factor;
                    let adjusted = if retreating {
                        Pose {
                            x: start.x + dx,
                            z: start.z + dz,
                            ..previous
                        }
                    } else {
                        let (pose, target) = self.support(
                            start.x + dx,
                            start.z + dz,
                            yaw,
                            reference,
                            start,
                            h,
                            &extra,
                        )?;
                        road = target;
                        pose
                    };
                    let (mut clear, more) =
                        self.valid_impact(adjusted, previous, impact, dx, dz)?;
                    let mut adjusted = adjusted;
                    if !clear && !more.is_empty() {
                        let contacts;
                        (adjusted, clear, contacts) =
                            self.bottom(adjusted, more, dx, dz, road, previous)?;
                        if !clear {
                            if let Some(held) = self.low_step(adjusted, previous, &contacts, &extra)? {
                                adjusted = held;
                                clear = true;
                            }
                        }
                    }
                    if clear {
                        candidate = adjusted;
                    } else {
                        dx = 0.;
                        dz = 0.;
                        candidate = previous;
                        road = old_road;
                    }
                }
                pose = candidate;
                outcome.moved |= dx.hypot(dz) > (travel / slices * 0.02).max(0.00001);
            } else {
                if outcome.contact == 0 {
                    outcome.contact = 3;
                }
                pose = previous;
            }
            if self.state.spring.pose == pose {
                self.state.motion.suspension = self.state.spring.velocity;
                self.state.lean = self.state.spring.lean;
                self.state.motion.angular = self.state.spring.angular;
            } else {
                self.state.motion.suspension = [0.; 3];
                self.state.motion.angular = [0.; 2];
            }
            let blocked = travel > 0. && dx.hypot(dz) < 1e-9;
            if self.state.blocked && !blocked {
                self.state.motion.suspension[0] = 0.;
            }
            self.state.blocked = blocked;
            if valid && (pose.x - start.x).hypot(pose.z - start.z) >= 1e-9 {
                if self.detached(start, pose, road, h, incoming[1]) {
                    let mut velocity = incoming;
                    velocity[1] = self.state.motion.suspension[0];
                    let budget = dot(incoming, incoming).sqrt();
                    let speed = dot(velocity, velocity).sqrt();
                    if speed > 0. {
                        velocity = velocity.map(|v| v * (budget / speed).min(1.));
                    }
                    let air = Pose {
                        x: start.x + velocity[0] * h,
                        z: start.z + velocity[2] * h,
                        pitch: previous.pitch,
                        roll: previous.roll,
                        ..pose
                    };
                    if self.valid(air, pose, false)?.0 {
                        let loaded_angular = self.state.motion.angular;
                        self.state.pose = air;
                        self.state.motion.velocity = velocity;
                        self.state.motion.suspension = [0.; 3];
                        self.state.motion.angular = [0.; 2];
                        self.state.latch = None;
                        outcome.grounded = false;
                        outcome.rest = input.dt - h * (step + 1) as f64;
                        let droop_speed = (2. * self.constants[2] * self.constants[4]).sqrt();
                        let release_rate = |actual: f64, loaded: f64, arm: f64| {
                            let allowance = droop_speed / (2. * arm).max(0.1);
                            let carried = if actual * loaded > 0. {
                                loaded.abs()
                            } else {
                                0.
                            };
                            actual.abs().min(carried.max(allowance)).copysign(actual)
                        };
                        let pitch = release_rate(
                            (previous.pitch - start.pitch) / h,
                            loaded_angular[0],
                            self.state.configuration.patch,
                        );
                        let arm = self
                            .state
                            .configuration
                            .a
                            .iter()
                            .map(|v| v.abs())
                            .fold(0., f64::max);
                        let roll =
                            release_rate((previous.roll - start.roll) / h, loaded_angular[1], arm);
                        outcome.spin = Self::angular(
                            start,
                            Pose {
                                pitch: start.pitch + pitch * h,
                                roll: start.roll + roll * h,
                                ..previous
                            },
                            h,
                        );
                        return Ok(outcome);
                    }
                }
                let tangent =
                    Self::road_velocity(road, (pose.x - start.x) / h, (pose.z - start.z) / h);
                let speed = dot(tangent, tangent).sqrt();
                let budget = dot(incoming, incoming).sqrt();
                let scale = if speed > 0. {
                    (budget / speed).min(1.)
                } else {
                    1.
                };
                self.state.motion.velocity = tangent.map(|v| v * scale);
            } else {
                self.state.motion.velocity = [0.; 3];
            }
            if valid {
                self.state.road = road;
            }
            self.world.set_pose(
                self.world.entities[&self.entity],
                position(pose),
                quaternion(pose),
                None,
            )?;
        }
        if let Some([nx, nz, closing, speed]) = wall_hit {
            if !self
                .state
                .latch
                .is_some_and(|latch| latch[2] * nx + latch[3] * nz > 0.7)
            {
                self.state.latch = Some([pose.x, pose.z, nx, nz]);
                let [vx, vy, vz] = self.state.motion.velocity;
                let along = vx * nx + vz * nz;
                let rebound = (0.15 * closing).min(1.);
                let (mut vx, mut vz) = (
                    vx - along * nx + rebound * nx,
                    vz - along * nz + rebound * nz,
                );
                let planar = vx.hypot(vz);
                if planar > speed {
                    vx *= speed / planar;
                    vz *= speed / planar;
                }
                self.state.motion.velocity = [vx, vy, vz];
                outcome.recoil = Some([nx, nz, closing, rebound, vx, vy, vz]);
            }
        }
        self.state.pose = pose;
        Ok(outcome)
    }
}

pub struct Outcome {
    pub moved: bool,
    pub grounded: bool,
    pub rest: f64,
    pub spin: [f64; 3],
    pub contact: i32,
    pub recoil: Option<[f64; 7]>,
}

impl Default for Outcome {
    fn default() -> Self {
        Self {
            moved: false,
            grounded: true,
            rest: 0.,
            spin: [0.; 3],
            contact: 0,
            recoil: None,
        }
    }
}

pub fn step(
    world: &mut World,
    entity: u32,
    input: DriveInput,
    seed: &super::StepInput,
) -> Result<(State, Outcome), String> {
    if input.dt <= 0.
        || input.dt > 1.
        || ![input.distance, input.yaw_delta, input.dt]
            .iter()
            .all(|v| v.is_finite())
    {
        return Err("Invalid native physics timestep".into());
    }
    let state = world
        .ground
        .remove(&entity)
        .ok_or("Missing native vehicle configuration")?;
    let mut movement = Movement {
        world,
        entity,
        state,
        constants: input.constants,
        effort: input.effort,
        contact_cache: BTreeMap::new(),
    };
    let outcome = movement.run(input, seed)?;
    Ok((movement.state, outcome))
}

impl State {
    pub fn snapshot(&self) -> Value {
        let pose = |p: Pose| [p.x, p.y, p.z, p.yaw, p.pitch, p.roll];
        serde_json::json!({"pose":pose(self.pose),"road":pose(self.road),
            "hints":self.hints,"supports":self.supports,"lower":self.lower,"rows":self.rows,
            "spring":{"pose":pose(self.spring.pose),"velocity":self.spring.velocity,"lean":self.spring.lean,
                "angular":self.spring.angular},"bounds_pose":self.bounds_pose.map(pose)})
    }

    pub fn output(&self, outcome: &Outcome, event_count: u32) -> super::StepOutput {
        let mut supports = [super::CachedSupport::default(); 16];
        for (target, &(body, x, z, height)) in supports.iter_mut().zip(&self.supports) {
            *target = super::CachedSupport { body, x, z, height };
        }
        super::StepOutput {
            pose: self.pose,
            road: self.road,
            motion: self.motion,
            lean: self.lean,
            rest: outcome.rest,
            spin: outcome.spin,
            recoil: outcome.recoil.unwrap_or([0.; 7]),
            moved: u32::from(outcome.moved),
            grounded: u32::from(outcome.grounded),
            blocked: u32::from(self.blocked),
            carrying_supported: u32::from(self.supported),
            contact: outcome.contact,
            has_recoil: u32::from(outcome.recoil.is_some()),
            event_count,
            support_count: self.supports.len() as u32,
            supports,
            has_latch: u32::from(self.latch.is_some()),
            latch: self.latch.unwrap_or([0.; 4]),
        }
    }
}
