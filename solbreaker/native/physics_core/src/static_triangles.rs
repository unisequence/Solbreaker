//! Conservative bounds for the untransformed triangular OBJ files produced by
//! the map fixture. A missing index leaves all candidate rays with Bullet.
use std::path::Path;

#[derive(Clone, Copy)]
struct Bounds {
    low: [f64; 3],
    high: [f64; 3],
}

impl Bounds {
    fn point(p: [f64; 3]) -> Self {
        Self { low: p, high: p }
    }

    fn union(self, other: Self) -> Self {
        Self {
            low: std::array::from_fn(|axis| self.low[axis].min(other.low[axis])),
            high: std::array::from_fn(|axis| self.high[axis].max(other.high[axis])),
        }
    }

    fn intersects(self, other: Self) -> bool {
        (0..3).all(|axis| self.low[axis] <= other.high[axis] && self.high[axis] >= other.low[axis])
    }
}

enum Contents {
    Branch([usize; 2]),
    Leaf(std::ops::Range<usize>),
}

struct Node {
    bounds: Bounds,
    contents: Contents,
}

pub struct Tree {
    nodes: Vec<Node>,
    triangles: Vec<Bounds>,
}

impl Tree {
    pub fn from_obj(path: &Path) -> Option<Self> {
        Self::parse_obj(&std::fs::read_to_string(path).ok()?)
    }

    fn parse_obj(data: &str) -> Option<Self> {
        let mut vertices: Vec<[f64; 3]> = Vec::new();
        let mut triangles = Vec::new();
        for line in data.lines() {
            let mut columns = line.split_whitespace();
            match columns.next() {
                Some("v") => {
                    let p: [f64; 3] = [
                        columns.next()?.parse().ok()?,
                        columns.next()?.parse().ok()?,
                        columns.next()?.parse().ok()?,
                    ];
                    if columns.next().is_some()
                        || !p.iter().all(|v| v.is_finite() && v.abs() < 100_000.)
                    {
                        return None;
                    }
                    vertices.push(p);
                }
                Some("f") => {
                    // This fixture writer emits positive, triangular OBJ faces.
                    let mut bounds = None;
                    for _ in 0..3 {
                        let i: usize = columns.next()?.parse().ok()?;
                        let p = Bounds::point(*vertices.get(i.checked_sub(1)?)?);
                        bounds = Some(bounds.map_or(p, |b: Bounds| b.union(p)));
                    }
                    if columns.next().is_some() {
                        return None;
                    }
                    triangles.push(bounds?);
                }
                None => {}
                Some(record) if record.starts_with('#') => {}
                _ => return None,
            }
        }
        if triangles.is_empty() {
            return None;
        }
        let mut nodes = Vec::new();
        Self::build(&mut nodes, &mut triangles, 0);
        Some(Self { nodes, triangles })
    }

    fn build(nodes: &mut Vec<Node>, triangles: &mut [Bounds], start: usize) -> usize {
        let bounds = triangles.iter().copied().reduce(Bounds::union).unwrap();
        let id = nodes.len();
        nodes.push(Node {
            bounds,
            contents: Contents::Leaf(start..start + triangles.len()),
        });
        if triangles.len() > 6 {
            let axis = (0..3)
                .max_by(|&a, &b| {
                    (bounds.high[a] - bounds.low[a])
                        .total_cmp(&(bounds.high[b] - bounds.low[b]))
                })
                .unwrap();
            let mid = triangles.len() / 2;
            triangles.select_nth_unstable_by(mid, |a, b| {
                (a.low[axis] + a.high[axis]).total_cmp(&(b.low[axis] + b.high[axis]))
            });
            let (left, right) = triangles.split_at_mut(mid);
            let left = Self::build(nodes, left, start);
            let right = Self::build(nodes, right, start + mid);
            nodes[id].contents = Contents::Branch([left, right]);
        }
        id
    }

    pub fn may_intersect(&self, low: [f64; 3], high: [f64; 3]) -> bool {
        // Widen the query for importer rounding and Bullet's mesh tolerances.
        // The index only rejects candidates and never determines an intersection.
        const PADDING: f64 = 0.25;
        self.search(
            0,
            Bounds {
                low: low.map(|v| v - PADDING),
                high: high.map(|v| v + PADDING),
            },
        )
    }

    fn search(&self, id: usize, query: Bounds) -> bool {
        let node = &self.nodes[id];
        if !node.bounds.intersects(query) {
            return false;
        }
        match &node.contents {
            Contents::Branch([left, right]) => self.search(*left, query) || self.search(*right, query),
            Contents::Leaf(range) => self.triangles[range.clone()]
                .iter()
                .any(|bounds| bounds.intersects(query)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Tree;

    const TRIANGLE: &str = "v 0 1 0\nv 2 1 0\nv 0 1 2\nf 1 2 3\n";

    #[test]
    fn retains_rays_near_bounds_and_rejects_clear_rays() {
        let obj = format!("{TRIANGLE}{}", "f 1 2 3\n".repeat(8));
        let tree = Tree::parse_obj(&obj).expect("generated mesh");
        assert!(tree.may_intersect([1., 0., 1.], [1., 2., 1.]));
        assert!(tree.may_intersect([2.1, 0., 1.], [2.1, 2., 1.]));
        assert!(!tree.may_intersect([3., 0., 1.], [3., 2., 1.]));
        assert!(!tree.may_intersect([1., 2., 1.], [1., 4., 1.]));
    }

    #[test]
    fn unsupported_geometry_disables_filtering_for_the_body() {
        assert!(Tree::parse_obj("v 0 0 0\nf 1 2 3\n").is_none());
        assert!(Tree::parse_obj(&format!("{TRIANGLE}f 1 2 3 1\n")).is_none());
        assert!(Tree::parse_obj(&format!("{TRIANGLE}f -1 -2 -3\n")).is_none());
        assert!(Tree::parse_obj(&format!("{TRIANGLE}v 0 0 0 2\n")).is_none());
    }
}
