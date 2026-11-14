//! Complete support-plane solve, including the singular working sets that
//! the speculative kernel delegates to NumPy in the established backend.
use nalgebra::{DMatrix, DVector, Matrix3, Vector3};
use tfs_sim_core::math::{fit_plane, Fit};

fn solve(matrix: DMatrix<f64>, rhs: DVector<f64>) -> Option<DVector<f64>> {
    if let Some(value) = matrix.clone().lu().solve(&rhs) {
        if value.iter().all(|v| v.is_finite()) {
            return Some(value);
        }
    }
    let dimension = matrix.nrows().max(matrix.ncols());
    let svd = matrix.svd(true, true);
    let largest = svd.singular_values.iter().copied().fold(0., f64::max);
    svd.solve(&rhs, f64::EPSILON * dimension as f64 * largest)
        .ok()
}

pub fn fit(
    rows: &[[f64; 3]],
    target: &[f64],
    weights: &[f64],
    lower: &[f64],
    hints: &[usize],
) -> Option<Fit> {
    if let Some(result) = fit_plane(rows, target, weights, lower, hints) {
        return Some(result);
    }
    let count = rows.len();
    if !(1..=512).contains(&count)
        || [target.len(), weights.len(), lower.len()]
            .iter()
            .any(|n| *n != count)
        || rows
            .iter()
            .flatten()
            .chain(target)
            .chain(weights)
            .chain(lower)
            .any(|v| !v.is_finite())
        || weights.iter().any(|v| *v < 0.)
    {
        return None;
    }
    let phi = DMatrix::from_fn(count, 3, |i, j| rows[i][j]);
    let mut hessian = Matrix3::identity() * 1e-9;
    let mut linear = Vector3::zeros();
    for i in 0..count {
        let row = Vector3::from_row_slice(&rows[i]);
        hessian += row * row.transpose() * weights[i];
        linear += row * (weights[i] * target[i]);
    }
    let inverse = hessian.try_inverse()?;
    let limits = DVector::from_column_slice(lower);
    let mut plane = inverse * linear;
    let mut active = Vec::new();
    if !hints.is_empty() && hints.len() <= 3 && hints.iter().all(|&i| i < count) {
        let a = DMatrix::from_fn(hints.len(), 3, |i, j| rows[hints[i]][j]);
        if (&a * a.transpose()).determinant() > 1e-6 {
            let projected = &a * inverse;
            let rhs = DVector::from_iterator(hints.len(), hints.iter().map(|&i| lower[i]))
                - &projected * linear;
            let multiplier = solve(&projected * a.transpose(), rhs)?;
            let guess = inverse * (linear + a.transpose() * multiplier);
            if (&phi * guess - &limits).min() >= -1e-9 {
                plane = guess;
                active.extend_from_slice(hints);
            }
        }
    }
    if active.is_empty() {
        let slack = &phi * plane - &limits;
        let (worst, value) = slack.iter().enumerate().min_by(|a, b| a.1.total_cmp(b.1))?;
        if *value < 0. {
            plane[0] -= value;
            active.push(worst);
        }
    }
    for _ in 0..16 {
        let gradient = hessian * plane - linear;
        let (step, multiplier) = if active.is_empty() {
            (-inverse * gradient, None)
        } else {
            let a = DMatrix::from_fn(active.len(), 3, |i, j| rows[active[i]][j]);
            let projected = &a * inverse;
            let multiplier = solve(&projected * a.transpose(), &projected * gradient)?;
            (
                inverse * (a.transpose() * &multiplier - gradient),
                Some(multiplier),
            )
        };
        if step.iter().any(|v| !v.is_finite()) {
            return None;
        }
        if step.iter().copied().map(f64::abs).fold(0., f64::max) < 1e-10 {
            if let Some(multiplier) = multiplier {
                let (least, value) = multiplier
                    .iter()
                    .enumerate()
                    .min_by(|a, b| a.1.total_cmp(b.1))?;
                if *value < -1e-10 {
                    active.remove(least);
                    continue;
                }
            }
            break;
        }
        let rate = &phi * step;
        let slack = &phi * plane - &limits;
        let mut alpha = 1.;
        let mut blocking = None;
        for i in 0..count {
            if !active.contains(&i) && rate[i] < -1e-12 {
                let limit = slack[i].max(0.) / -rate[i];
                if limit < alpha {
                    alpha = limit;
                    blocking = Some(i);
                }
            }
        }
        plane += step * alpha;
        if let Some(index) = blocking {
            active.push(index);
        }
    }
    if plane.iter().any(|v| !v.is_finite()) {
        return None;
    }
    // Nearly redundant constraints can leave a small infeasible residual
    // after the bounded active-set solve. Every support row has a unit
    // intercept, so raising the fitted plane by that residual is feasible.
    // Never accept a large correction as numerical noise.
    let deficit = rows
        .iter()
        .zip(lower)
        .map(|(row, limit)| limit - Vector3::from_row_slice(row).dot(&plane))
        .fold(0., f64::max);
    if deficit > 0. {
        if deficit > 1e-4 || rows.iter().any(|row| row[0] != 1.) {
            return None;
        }
        plane[0] += deficit;
        active.clear();
    }
    Some(Fit {
        plane: [plane[0], plane[1], plane[2]],
        active,
    })
}

#[cfg(test)]
mod tests {
    use super::fit;

    #[test]
    fn repeated_nearly_coplanar_rock_contacts_have_a_feasible_fit() {
        let mut rows = Vec::new();
        for side in [-1., 1.] {
            for station in 0..9 {
                rows.push([1., side, -2. + station as f64 * 0.5]);
            }
        }
        rows.push([1., 0., 0.]);
        let contacts = [
            [1., 0.865384618144009, 2.0009874654948065],
            [1., -0.8653846181335094, 2.0009874654948057],
            [1., -1.1000000946499437, 2.0008265211859646],
            [1., 0.6999999172708058, 2.0006990674110314],
        ];
        rows.extend(contacts);
        rows.extend(contacts);
        let mut lower = vec![-0.09987792782482643; 18];
        lower.push(-0.44187792782482643);
        lower.extend([0.10000000298023226; 8]);
        let mut targets = vec![0.00012207217517357094; 18];
        targets.extend([0.; 9]);
        let mut weights = vec![1.; 18];
        weights.extend([0.; 9]);
        let result = fit(&rows, &targets, &weights, &lower, &[8, 17]).expect("rock support solve");
        assert!(result.plane.iter().all(|v| v.is_finite()));
        for (row, bound) in rows.iter().zip(lower) {
            let height = row
                .iter()
                .zip(result.plane)
                .map(|(a, b)| a * b)
                .sum::<f64>();
            assert!(height >= bound - 2e-8);
        }
    }
}
