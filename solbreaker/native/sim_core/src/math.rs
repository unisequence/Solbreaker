//! Small, deterministic support-plane quadratic programme.
//!
//! This follows `CollisionWorld._support_plane`: a regularized weighted fit
//! with a feasible primal active set and at most sixteen iterations. Inputs
//! normally have rows `[1, lateral, longitudinal]`. Numerical failures return
//! `None`, allowing the caller to retain the Python solver as its fallback.

#[derive(Clone, Debug)]
pub struct Fit {
    pub plane: [f64; 3],
    pub active: Vec<usize>,
}

type Matrix = [[f64; 3]; 3];

fn dot(a: &[f64; 3], b: &[f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn mat_vec(matrix: &Matrix, vector: &[f64; 3]) -> [f64; 3] {
    [
        dot(&matrix[0], vector),
        dot(&matrix[1], vector),
        dot(&matrix[2], vector),
    ]
}

fn finite_vector(vector: &[f64; 3]) -> bool {
    vector.iter().all(|value| value.is_finite())
}

/// Partial-pivot Gaussian elimination, restricted to the occupied leading
/// `size` rows/columns. Near-singular systems are left to the Python fallback;
/// there is deliberately no least-squares approximation here.
fn small_solve(mut matrix: Matrix, mut vector: [f64; 3], size: usize) -> Option<[f64; 3]> {
    if !(1..=3).contains(&size) {
        return None;
    }
    let mut scale = 0.0_f64;
    for row in 0..size {
        if !vector[row].is_finite() {
            return None;
        }
        for column in 0..size {
            let value = matrix[row][column];
            if !value.is_finite() {
                return None;
            }
            scale = scale.max(value.abs());
        }
    }
    if scale == 0.0 {
        return None;
    }
    let minimum_pivot = f64::EPSILON * size as f64 * scale;
    for column in 0..size {
        let mut pivot = column;
        for row in column + 1..size {
            if matrix[row][column].abs() > matrix[pivot][column].abs() {
                pivot = row;
            }
        }
        let value = matrix[pivot][column];
        if !value.is_finite() || value.abs() <= minimum_pivot {
            return None;
        }
        if pivot != column {
            matrix.swap(pivot, column);
            vector.swap(pivot, column);
        }
        for row in column + 1..size {
            let factor = matrix[row][column] / matrix[column][column];
            matrix[row][column] = 0.0;
            for next in column + 1..size {
                matrix[row][next] -= factor * matrix[column][next];
                if !matrix[row][next].is_finite() {
                    return None;
                }
            }
            vector[row] -= factor * vector[column];
            if !vector[row].is_finite() {
                return None;
            }
        }
    }
    let mut solution = [0.0; 3];
    for row in (0..size).rev() {
        let mut remainder = vector[row];
        for column in row + 1..size {
            remainder -= matrix[row][column] * solution[column];
        }
        solution[row] = remainder / matrix[row][row];
        if !solution[row].is_finite() {
            return None;
        }
    }
    Some(solution)
}

fn inverse(matrix: Matrix) -> Option<Matrix> {
    let mut result = [[0.0; 3]; 3];
    for column in 0..3 {
        let mut basis = [0.0; 3];
        basis[column] = 1.0;
        let solution = small_solve(matrix, basis, 3)?;
        for row in 0..3 {
            result[row][column] = solution[row];
        }
    }
    Some(result)
}

fn determinant(matrix: &Matrix, size: usize) -> f64 {
    match size {
        1 => matrix[0][0],
        2 => matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0],
        3 => {
            matrix[0][0]
                * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
                - matrix[0][1]
                    * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
                + matrix[0][2]
                    * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
        }
        _ => 0.0,
    }
}

/// `rows @ inverse` and its range-space matrix `rows @ inverse @ rows.T`.
fn active_system(
    phi: &[[f64; 3]],
    active: &[usize],
    inverse: &Matrix,
) -> Option<(Matrix, Matrix)> {
    if active.is_empty() || active.len() > 3 {
        return None;
    }
    let mut projected = [[0.0; 3]; 3];
    for (row, &index) in active.iter().enumerate() {
        for column in 0..3 {
            projected[row][column] = phi[index][0] * inverse[0][column]
                + phi[index][1] * inverse[1][column]
                + phi[index][2] * inverse[2][column];
        }
        if !finite_vector(&projected[row]) {
            return None;
        }
    }
    let mut system = [[0.0; 3]; 3];
    for row in 0..active.len() {
        for column in 0..active.len() {
            system[row][column] = dot(&projected[row], &phi[active[column]]);
        }
    }
    Some((projected, system))
}

fn transpose_product(phi: &[[f64; 3]], active: &[usize], values: &[f64; 3]) -> [f64; 3] {
    let mut product = [0.0; 3];
    for (row, &index) in active.iter().enumerate() {
        for column in 0..3 {
            product[column] += phi[index][column] * values[row];
        }
    }
    product
}

/// Fit a support plane above every lower bound.
///
/// The array lengths must match and contain 1–512 finite samples. Weights
/// must be nonnegative. A feasible, independent hint of one to three station
/// indices is reused, with the same Gram determinant threshold as Python.
/// Invalid indices, nonfinite intermediate results, singular systems, or a
/// final lower-bound violation exceeding `2e-8 * max(1, |height|, |lower|)`
/// return `None`. Larger or dependent hints are ignored, as in Python.
pub fn fit_plane(
    phi: &[[f64; 3]],
    target: &[f64],
    weight: &[f64],
    lower: &[f64],
    hint: &[usize],
) -> Option<Fit> {
    let count = phi.len();
    if !(1..=512).contains(&count)
        || target.len() != count
        || weight.len() != count
        || lower.len() != count
        || phi.iter().any(|row| !finite_vector(row))
        || target.iter().any(|value| !value.is_finite())
        || lower.iter().any(|value| !value.is_finite())
        || weight.iter().any(|value| !value.is_finite() || *value < 0.0)
        || hint.iter().any(|index| *index >= count)
    {
        return None;
    }

    let mut hessian = [[0.0; 3]; 3];
    let mut linear = [0.0; 3];
    for sample in 0..count {
        for row in 0..3 {
            let weighted = phi[sample][row] * weight[sample];
            linear[row] += weighted * target[sample];
            for column in 0..3 {
                hessian[row][column] += weighted * phi[sample][column];
            }
        }
    }
    for diagonal in 0..3 {
        hessian[diagonal][diagonal] += 1e-9;
    }
    if !finite_vector(&linear) {
        return None;
    }
    let inverse = inverse(hessian)?;
    let mut active = Vec::with_capacity(3);
    let mut candidate = None;

    if !hint.is_empty() && hint.len() <= 3 {
        let mut gram = [[0.0; 3]; 3];
        for row in 0..hint.len() {
            for column in 0..hint.len() {
                gram[row][column] = dot(&phi[hint[row]], &phi[hint[column]]);
            }
        }
        let independence = determinant(&gram, hint.len());
        if !independence.is_finite() {
            return None;
        }
        if independence > 1e-6 {
            let (projected, system) = active_system(phi, hint, &inverse)?;
            let mut rhs = [0.0; 3];
            for row in 0..hint.len() {
                rhs[row] = lower[hint[row]] - dot(&projected[row], &linear);
            }
            let multipliers = small_solve(system, rhs, hint.len())?;
            let correction = transpose_product(phi, hint, &multipliers);
            let guess = mat_vec(
                &inverse,
                &[
                    linear[0] + correction[0],
                    linear[1] + correction[1],
                    linear[2] + correction[2],
                ],
            );
            if !finite_vector(&guess) {
                return None;
            }
            let mut feasible = true;
            for sample in 0..count {
                let slack = dot(&phi[sample], &guess) - lower[sample];
                if !slack.is_finite() {
                    return None;
                }
                feasible &= slack >= -1e-9;
            }
            if feasible {
                candidate = Some(guess);
                active.extend_from_slice(hint);
            }
        }
    }

    let mut plane = if let Some(guess) = candidate {
        guess
    } else {
        let mut guess = mat_vec(&inverse, &linear);
        if !finite_vector(&guess) {
            return None;
        }
        let mut worst = 0;
        let mut worst_slack = f64::INFINITY;
        for sample in 0..count {
            let slack = dot(&phi[sample], &guess) - lower[sample];
            if !slack.is_finite() {
                return None;
            }
            if slack < worst_slack {
                worst_slack = slack;
                worst = sample;
            }
        }
        if worst_slack < 0.0 {
            guess[0] -= worst_slack;
            active.push(worst);
        }
        if !finite_vector(&guess) {
            return None;
        }
        guess
    };

    for _ in 0..16 {
        let product = mat_vec(&hessian, &plane);
        let gradient = [
            product[0] - linear[0],
            product[1] - linear[1],
            product[2] - linear[2],
        ];
        if !finite_vector(&gradient) {
            return None;
        }
        let (step, multipliers) = if active.is_empty() {
            let product = mat_vec(&inverse, &gradient);
            ([-product[0], -product[1], -product[2]], None)
        } else {
            let (projected, system) = active_system(phi, &active, &inverse)?;
            let mut rhs = [0.0; 3];
            for row in 0..active.len() {
                rhs[row] = dot(&projected[row], &gradient);
            }
            let multipliers = small_solve(system, rhs, active.len())?;
            let correction = transpose_product(phi, &active, &multipliers);
            let step = mat_vec(
                &inverse,
                &[
                    correction[0] - gradient[0],
                    correction[1] - gradient[1],
                    correction[2] - gradient[2],
                ],
            );
            (step, Some(multipliers))
        };
        if !finite_vector(&step) {
            return None;
        }
        if step.iter().fold(0.0_f64, |maximum, value| maximum.max(value.abs())) < 1e-10 {
            if let Some(values) = multipliers {
                let mut least = 0;
                for row in 1..active.len() {
                    if values[row] < values[least] {
                        least = row;
                    }
                }
                if values[least] < -1e-10 {
                    active.remove(least);
                    continue;
                }
            }
            break;
        }

        let mut alpha = 1.0;
        let mut blocking = None;
        for sample in 0..count {
            let rate = dot(&phi[sample], &step);
            if !rate.is_finite() {
                return None;
            }
            if !active.contains(&sample) && rate < -1e-12 {
                let slack = dot(&phi[sample], &plane) - lower[sample];
                if !slack.is_finite() {
                    return None;
                }
                let limit = slack.max(0.0) / -rate;
                if !limit.is_finite() {
                    return None;
                }
                if limit < alpha {
                    alpha = limit;
                    blocking = Some(sample);
                }
            }
        }
        for coordinate in 0..3 {
            plane[coordinate] += alpha * step[coordinate];
        }
        if !finite_vector(&plane) {
            return None;
        }
        if let Some(index) = blocking {
            if active.len() >= 3 {
                return None;
            }
            active.push(index);
        }
    }

    for sample in 0..count {
        let height = dot(&phi[sample], &plane);
        let tolerance = 2e-8 * 1.0_f64.max(height.abs()).max(lower[sample].abs());
        if !height.is_finite() || height - lower[sample] < -tolerance {
            return None;
        }
    }
    Some(Fit { plane, active })
}

#[cfg(test)]
mod tests {
    use super::*;

    const CORNERS: [[f64; 3]; 4] = [
        [1.0, -1.0, -1.0],
        [1.0, -1.0, 1.0],
        [1.0, 1.0, -1.0],
        [1.0, 1.0, 1.0],
    ];

    fn near(actual: f64, expected: f64, tolerance: f64) {
        assert!((actual - expected).abs() <= tolerance, "{actual} != {expected}");
    }

    fn assert_feasible(phi: &[[f64; 3]], lower: &[f64], fit: &Fit) {
        for (row, &floor) in phi.iter().zip(lower) {
            assert!(dot(row, &fit.plane) >= floor - 1e-8);
        }
    }

    #[test]
    fn flat_fit_matches_analytic_regularized_minimum() {
        let fit = fit_plane(&CORNERS, &[2.0; 4], &[1.0; 4], &[-10.0; 4], &[]).unwrap();
        near(fit.plane[0], 8.0 / (4.0 + 1e-9), 1e-13);
        near(fit.plane[1], 0.0, 1e-13);
        near(fit.plane[2], 0.0, 1e-13);
        assert!(fit.active.is_empty());
    }

    #[test]
    fn slope_fit_matches_analytic_regularized_minimum() {
        let target: Vec<_> = CORNERS.iter().map(|row| dot(row, &[4.0, 0.25, -0.5])).collect();
        let fit = fit_plane(&CORNERS, &target, &[1.0; 4], &[-10.0; 4], &[]).unwrap();
        for (actual, expected) in fit.plane.iter().zip([4.0, 0.25, -0.5]) {
            near(*actual, expected * 4.0 / (4.0 + 1e-9), 1e-13);
        }
        assert!(fit.active.is_empty());
    }

    #[test]
    fn zero_weight_centre_is_a_hard_floor() {
        let mut phi = CORNERS.to_vec();
        phi.push([1.0, 0.0, 0.0]);
        let lower = [-10.0, -10.0, -10.0, -10.0, 3.0];
        let fit = fit_plane(&phi, &[0.0; 5], &[1.0, 1.0, 1.0, 1.0, 0.0], &lower, &[]).unwrap();
        near(fit.plane[0], 3.0, 1e-12);
        near(fit.plane[1], 0.0, 1e-12);
        near(fit.plane[2], 0.0, 1e-12);
        assert_eq!(fit.active, [4]);
        assert_feasible(&phi, &lower, &fit);
    }

    #[test]
    fn independent_hint_preserves_solution_and_working_set() {
        let lower = [2.0, 1.0, 2.0, -10.0];
        let fit = fit_plane(&CORNERS, &[0.0; 4], &[1.0; 4], &lower, &[0, 1, 2]).unwrap();
        near(fit.plane[0], 1.5, 1e-12);
        near(fit.plane[1], 0.0, 1e-12);
        near(fit.plane[2], -0.5, 1e-12);
        assert_eq!(fit.active, [0, 1, 2]);
        assert_feasible(&CORNERS, &lower, &fit);
    }

    #[test]
    fn hint_releases_a_negative_multiplier() {
        let lower = [1.0, 2.0, 3.0, -10.0];
        let fit = fit_plane(&CORNERS, &[0.0; 4], &[1.0; 4], &lower, &[0, 1, 2]).unwrap();
        near(fit.plane[0], 2.5, 1e-12);
        near(fit.plane[1], 0.25, 1e-12);
        near(fit.plane[2], -0.25, 1e-12);
        assert_eq!(fit.active, [1, 2]);
        assert_feasible(&CORNERS, &lower, &fit);
    }

    #[test]
    fn dependent_hint_is_ignored() {
        let fit = fit_plane(&CORNERS, &[0.0; 4], &[1.0; 4], &[1.0; 4], &[0, 0]).unwrap();
        near(fit.plane[0], 1.0, 1e-10);
        near(fit.plane[1], 0.0, 1e-10);
        near(fit.plane[2], 0.0, 1e-10);
        assert_feasible(&CORNERS, &[1.0; 4], &fit);
    }

    #[test]
    fn different_station_floors_remain_feasible() {
        let mut phi = Vec::new();
        let mut lower = Vec::new();
        for x in -3..=3 {
            for z in -3..=3 {
                phi.push([1.0, f64::from(x), f64::from(z)]);
                lower.push(f64::from((x * x + 3 * z * z + x * z) % 7) * 0.15);
            }
        }
        let fit = fit_plane(&phi, &vec![0.0; phi.len()], &vec![1.0; phi.len()], &lower, &[]).unwrap();
        assert_feasible(&phi, &lower, &fit);
        assert!(fit.active.len() <= 3);
    }

    #[test]
    fn small_solver_pivots_and_rejects_singular_systems() {
        let solved = small_solve([[0.0, 2.0, 0.0], [1.0, 3.0, 0.0], [0.0; 3]], [4.0, 7.0, 0.0], 2).unwrap();
        near(solved[0], 1.0, 1e-14);
        near(solved[1], 2.0, 1e-14);
        assert!(small_solve([[1.0, 1.0, 0.0], [2.0, 2.0, 0.0], [0.0; 3]], [1.0, 2.0, 0.0], 2).is_none());
        assert!(small_solve([[0.0; 3]; 3], [0.0; 3], 1).is_none());
    }

    #[test]
    fn bad_inputs_and_nonfinite_arithmetic_fall_back() {
        assert!(fit_plane(&[], &[], &[], &[], &[]).is_none());
        assert!(fit_plane(&CORNERS, &[0.0; 3], &[1.0; 4], &[0.0; 4], &[]).is_none());
        assert!(fit_plane(&CORNERS, &[f64::NAN; 4], &[1.0; 4], &[0.0; 4], &[]).is_none());
        assert!(fit_plane(&CORNERS, &[0.0; 4], &[-1.0; 4], &[0.0; 4], &[]).is_none());
        assert!(fit_plane(&CORNERS, &[0.0; 4], &[1.0; 4], &[0.0; 4], &[4]).is_none());
        assert!(fit_plane(&[[1.0, f64::MAX, 0.0]], &[1.0], &[1.0], &[0.0], &[]).is_none());
        assert!(fit_plane(&vec![[1.0, 0.0, 0.0]; 513], &vec![0.0; 513], &vec![1.0; 513], &vec![0.0; 513], &[]).is_none());
    }

    #[test]
    fn infeasible_non_support_rows_do_not_return_a_penetrating_plane() {
        // Real support rows start with one; this inconsistent generic system
        // exercises the final feasibility guard on otherwise finite inputs.
        let phi = [[0.0, 1.0, 0.0], [0.0, -1.0, 0.0]];
        assert!(fit_plane(&phi, &[0.0; 2], &[1.0; 2], &[1.0; 2], &[]).is_none());
    }
}
