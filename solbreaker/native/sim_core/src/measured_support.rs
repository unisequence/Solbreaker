//! Pure preparation of the existing support fit. No world or geometry access.
use crate::{aligned, math};
use std::panic::{catch_unwind, AssertUnwindSafe};

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MeasuredInput {
    abi: u32,
    count: u32,
    extra_count: u32,
    carrying: u32,
    full_rank: u32,
    supported: u32,
    a: *const f64,
    b: *const f64,
    p: *const f64,
    weights: *const f64,
    xs: *const f64,
    zs: *const f64,
    ground: *const f64,
    // Four doubles per extra row: a, b, height, compliant (0 or 1).
    extra: *const f64,
    old_position: [f64; 3],
    up: [f64; 3],
    compression: f64,
    reach: f64,
    skin: f64,
    centre: f64,
    clearance: f64,
    hint_count: u32,
    hints: [u32; 3],
}

#[repr(C)]
pub struct MeasuredOutput {
    plane: [f64; 3],
    active: [u32; 3],
    active_count: u32,
    supported: u32,
    lower: *mut f64,
}

#[no_mangle]
pub extern "C" fn tfs_measured_input_size() -> u32 { std::mem::size_of::<MeasuredInput>() as u32 }
#[no_mangle]
pub extern "C" fn tfs_measured_output_size() -> u32 { std::mem::size_of::<MeasuredOutput>() as u32 }

#[no_mangle]
pub unsafe extern "C" fn tfs_measured_support(input: *const MeasuredInput, output: *mut MeasuredOutput) -> i32 {
    if !aligned(input) || !aligned(output) { return 1; }
    let result = catch_unwind(AssertUnwindSafe(|| {
        let value = input.read();
        let n=value.count as usize;
        let extras=value.extra_count as usize;
        let total=n+1+extras;
        if value.abi!=1 || !(1..=256).contains(&n) || total>512
            || value.carrying>1 || value.supported>1 || value.hint_count>3
            || ![value.compression,value.reach,value.skin,value.centre,value.clearance]
                .iter().all(|x| x.is_finite())
            || value.compression<0. || value.reach<0. || value.skin<0.
            || !value.old_position.iter().chain(&value.up).all(|x|x.is_finite())
            || ![value.a,value.b,value.p,value.weights,value.xs,value.zs,value.ground]
                .iter().all(|&p|aligned(p))
            || (extras>0 && !aligned(value.extra)) { return Err(1); }
        let destination=std::ptr::addr_of!((*output).lower).read();
        if !aligned(destination) { return Err(1); }
        let a=std::slice::from_raw_parts(value.a,n);
        let b=std::slice::from_raw_parts(value.b,n);
        let p=std::slice::from_raw_parts(value.p,n);
        let weights=std::slice::from_raw_parts(value.weights,n);
        let xs=std::slice::from_raw_parts(value.xs,n);
        let zs=std::slice::from_raw_parts(value.zs,n);
        let ground=std::slice::from_raw_parts(value.ground,n);
        let extra=if extras==0 { &[][..] } else { std::slice::from_raw_parts(value.extra,extras*4) };
        if !a.iter().chain(b).chain(p).chain(weights).chain(xs).chain(zs).chain(ground)
            .chain(extra).all(|x|x.is_finite()) || weights.iter().any(|&w|w<0.) {return Err(1);}
        let relief:Vec<f64>=ground.iter().zip(p).map(|(&g,&p)|g-p).collect();
        if !relief.iter().all(|x|x.is_finite()) { return Err(1); }
        let low=relief.iter().copied().chain(extra.chunks_exact(4).map(|r|r[2]))
            .fold(f64::INFINITY,f64::min);
        let high=relief.iter().copied().chain(extra.chunks_exact(4).map(|r|r[2]))
            .fold(f64::NEG_INFINITY,f64::max);
        if value.carrying!=0 {
            // Partial footprints retain the complete original Python path.
            if value.full_rank<3 {return Err(3);}
            if high-low>value.reach+value.skin {
                let denominator=value.up[1].max(0.05);
                for i in 0..n {
                    let prediction=value.old_position[1]-(value.up[0]*(xs[i]-value.old_position[0])
                        +value.up[2]*(zs[i]-value.old_position[2]))/denominator;
                    if !prediction.is_finite() {return Err(1);}
                    if weights[i]>0. && relief[i]<prediction-value.reach {return Err(3);}
                }
            }
        }
        let mut phi:Vec<[f64;3]>=a.iter().zip(b).map(|(&a,&b)|[1.,a,b]).collect();
        phi.push([1.,0.,0.]);
        let mut lower:Vec<f64>=relief.iter().map(|&g|g-value.compression).collect();
        lower.push(if value.compression!=0. {value.centre-value.clearance+value.skin} else {value.centre});
        for row in extra.chunks_exact(4) {
            if row[3]!=0. && row[3]!=1. {return Err(1);}
            phi.push([1.,row[0],row[1]]);
            lower.push(row[2]-if row[3]!=0. {value.compression} else {0.});
        }
        let mut target=ground.to_vec();target.resize(total,0.);
        let mut weight=weights.to_vec();weight.resize(total,0.);
        let hints:Vec<usize>=value.hints[..value.hint_count as usize].iter().map(|&i|i as usize).collect();
        if hints.iter().any(|&i|i>=n) || !lower.iter().all(|x|x.is_finite()) {return Err(1);}
        let fit=math::fit_plane(&phi,&target,&weight,&lower,&hints).ok_or(5)?;
        if !fit.plane.iter().all(|x|x.is_finite()) || fit.active.len()>3
            || fit.active.iter().any(|&i|i>=total) {return Err(5);}
        Ok((fit,lower,destination,value.supported))
    }));
    match result {
        Ok(Ok((fit,lower,destination,supported))) => {
            let mut active=[0u32;3];
            for (slot,&index) in active.iter_mut().zip(&fit.active) {*slot=index as u32;}
            std::ptr::copy_nonoverlapping(lower.as_ptr(),destination,lower.len());
            output.write(MeasuredOutput{plane:fit.plane,active,active_count:fit.active.len() as u32,
                supported,lower:destination});
            0
        }
        Ok(Err(code)) => code,
        Err(_) => 10,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture<'a>(a:&'a[f64;4],b:&'a[f64;4],ground:&'a[f64;4],zeros:&'a[f64;4],ones:&'a[f64;4]) -> MeasuredInput {
        MeasuredInput{abi:1,count:4,extra_count:0,carrying:1,full_rank:3,supported:1,
            a:a.as_ptr(),b:b.as_ptr(),p:zeros.as_ptr(),weights:ones.as_ptr(),
            xs:a.as_ptr(),zs:b.as_ptr(),ground:ground.as_ptr(),extra:std::ptr::null(),
            old_position:[0.;3],up:[0.,1.,0.],compression:0.1,reach:0.28,skin:0.008,
            centre:0.,clearance:0.2,hint_count:0,hints:[0;3]}
    }
    #[test]
    fn measured_support_matches_original_fit_inputs() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];
        let ground=[0.1,0.15,0.12,0.17];let zeros=[0.;4];let ones=[1.;4];
        let input=fixture(&a,&b,&ground,&zeros,&ones);
        let mut lower=[42.;5];
        let mut output=MeasuredOutput{plane:[43.;3],active:[44;3],active_count:45,supported:46,lower:lower.as_mut_ptr()};
        assert_eq!(unsafe{tfs_measured_support(&input,&mut output)},0);
        let phi=[[-1.,-2.],[-1.,2.],[1.,-2.],[1.,2.]]
            .map(|p|[1.,p[0],p[1]]).into_iter().chain([[1.,0.,0.]]).collect::<Vec<_>>();
        let expected_lower=[0.1-0.1,0.15-0.1,0.12-0.1,0.17-0.1,0.-0.2+0.008];
        let expected=math::fit_plane(&phi,&[0.1,0.15,0.12,0.17,0.],&[1.,1.,1.,1.,0.],&expected_lower,&[]).unwrap();
        assert_eq!(lower,expected_lower);assert_eq!(output.plane,expected.plane);
        let expected_active=expected.active.iter().map(|&v|v as u32).collect::<Vec<_>>();
        assert_eq!(&output.active[..output.active_count as usize],expected_active.as_slice());
    }
    #[test]
    fn partial_footprint_leaves_every_output_untouched() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];
        let ground=[-5.,0.,0.,0.];let zeros=[0.;4];let ones=[1.;4];
        let input=fixture(&a,&b,&ground,&zeros,&ones);
        let mut lower=[42.;5];let mut output=MeasuredOutput{plane:[43.;3],active:[44;3],active_count:45,supported:46,lower:lower.as_mut_ptr()};
        assert_eq!(unsafe{tfs_measured_support(&input,&mut output)},3);
        assert_eq!(lower,[42.;5]);assert_eq!(output.plane,[43.;3]);assert_eq!(output.active_count,45);
    }
    #[test]
    fn bad_count_and_null_pointer_fail_before_array_access() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];let zeros=[0.;4];let ones=[1.;4];
        let mut input=fixture(&a,&b,&zeros,&zeros,&ones);input.count=513;
        let mut lower=[42.;5];let mut output=MeasuredOutput{plane:[43.;3],active:[44;3],active_count:45,supported:46,lower:lower.as_mut_ptr()};
        assert_eq!(unsafe{tfs_measured_support(&input,&mut output)},1);
        input.count=4;input.ground=std::ptr::null();
        assert_eq!(unsafe{tfs_measured_support(&input,&mut output)},1);
        assert_eq!(lower,[42.;5]);assert_eq!(output.plane,[43.;3]);
    }
}
