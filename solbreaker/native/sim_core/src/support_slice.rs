//! Pure sampling/fit/spring boundary used by the unchanged fallback controller.
use crate::{aligned,drive,DriveInput,Motion,PlanContext,Pose,SamplePlanFn};
use std::ffi::c_void;
use std::mem::size_of;
use std::panic::{catch_unwind,AssertUnwindSafe};

#[repr(C)]
#[derive(Clone,Copy)]
pub(crate) struct SupportQuery {
    pub x:f64,
    pub z:f64,
    pub yaw:f64,
    pub reference:Pose,
    pub start:Pose,
    pub road:Pose,
    pub motion:Motion,
    pub lean:[f64;4],
    pub h:f64,
    pub centre:f64,
    pub hint_count:u32,
    pub hints:[u32;3],
}

impl SupportQuery {
    pub fn valid(&self,count:usize)->bool {
        let pose=|p:Pose|[p.x,p.y,p.z,p.yaw,p.pitch,p.roll].iter().all(|v|v.is_finite());
        [self.x,self.z,self.yaw,self.h,self.centre].iter().all(|v|v.is_finite())
            && self.h>0. && self.h<=1.
            && pose(self.reference) && pose(self.start) && pose(self.road)
            && self.motion.velocity.iter().chain(&self.motion.suspension)
                .chain(&self.motion.angular).chain(&self.lean).all(|v|v.is_finite())
            && self.hint_count<=3
            && self.hints[..self.hint_count as usize].iter().all(|&i|(i as usize)<count)
    }
}

#[repr(C)]
pub struct SupportOutput {
    pub candidate:Pose,
    pub road:Pose,
    pub spring_velocity:[f64;3],
    pub spring_lean:[f64;4],
    pub spring_angular:[f64;2],
    pub hint_count:u32,
    pub hints:[u32;3],
    pub stage:u32,
    pub lower:*mut f64,
    pub xs:*mut f64,
    pub zs:*mut f64,
    pub ground:*mut f64,
}

struct Capture {
    plan:PlanContext,
    count:usize,
    samples:Option<(Vec<f64>,Vec<f64>,Vec<f64>)>,
}

unsafe extern "C" fn sample_capture(context:*mut c_void,count:u32,xs:*const f64,
    zs:*const f64,reference:*const Pose,output:*mut f64)->i32 {
    let state=&mut *(context as *mut Capture);
    if count as usize!=state.count {return 2;}
    let status=crate::sample_planned((&mut state.plan as *mut PlanContext).cast(),
                                    count,xs,zs,reference,output);
    if status==0 {
        state.samples=Some((std::slice::from_raw_parts(xs,state.count).to_vec(),
                            std::slice::from_raw_parts(zs,state.count).to_vec(),
                            std::slice::from_raw_parts(output,state.count).to_vec()));
    }
    status
}

unsafe extern "C" fn unused_validation(_: *mut c_void,_:*const Pose,_:*const Pose)->i32 {2}

#[no_mangle]
pub extern "C" fn tfs_support_slice_abi()->u32 {1}
#[no_mangle]
pub extern "C" fn tfs_support_slice_input_size()->u32 {size_of::<SupportQuery>() as u32}
#[no_mangle]
pub extern "C" fn tfs_support_slice_output_size()->u32 {size_of::<SupportOutput>() as u32}

/// Status0 publishes a full fit/spring and stage2. Status12 publishes only
/// xs/zs/ground and stage1 for an unsupported footprint; other fields and lower
/// remain untouched. All other failures leave every output buffer untouched.
/// Caller owns valid, disjoint buffers: lower[count+1], xs/zs/ground[count].
#[no_mangle]
pub unsafe extern "C" fn tfs_ground_support_slice_v1(input:*const DriveInput,
    query:*const SupportQuery,sample:Option<SamplePlanFn>,context:*mut c_void,
    slope:f64,output:*mut SupportOutput)->i32 {
    if !aligned(input) || !aligned(query) || !aligned(output) || sample.is_none()
        || !slope.is_finite() || slope<0. || slope>=std::f64::consts::FRAC_PI_2 {return 1;}
    let result=catch_unwind(AssertUnwindSafe(|| {
        let input=input.read();let query=query.read();let count=input.count as usize;
        if !(1..=256).contains(&count) || !query.valid(count) {return 1;}
        let lower=(*output).lower;let xs=(*output).xs;
        let zs=(*output).zs;let ground=(*output).ground;
        if ![lower,xs,zs,ground].iter().all(|&p|aligned(p)) {return 1;}
        let mut capture=Capture{plan:PlanContext{sample:sample.unwrap(),
            validate:unused_validation,original:context,terrain:input.terrain,slope,
            reach:input.constants[3],skin:input.constants[0]},count,samples:None};
        let result=drive::support_slice(input,query,sample_capture,
                                       (&mut capture as *mut Capture).cast());
        match result {
            Ok(prefix)=>{
                let Some((sample_x,sample_z,sample_ground))=capture.samples else {return 4;};
                if prefix.lower_bounds.len()!=count+1 || prefix.hints.len()>3 {return 5;}
                let mut hints=[0;3];
                for (slot,&value) in hints.iter_mut().zip(&prefix.hints) {*slot=value as u32;}
                std::ptr::copy_nonoverlapping(prefix.lower_bounds.as_ptr(),lower,count+1);
                std::ptr::copy_nonoverlapping(sample_x.as_ptr(),xs,count);
                std::ptr::copy_nonoverlapping(sample_z.as_ptr(),zs,count);
                std::ptr::copy_nonoverlapping(sample_ground.as_ptr(),ground,count);
                output.write(SupportOutput{candidate:prefix.rotated,road:prefix.road,
                    spring_velocity:prefix.spring.velocity,spring_lean:prefix.spring.lean,
                    spring_angular:prefix.spring.angular,hint_count:prefix.hints.len() as u32,
                    hints,stage:2,lower,xs,zs,ground});
                0
            }
            Err(failure)=>{
                if failure.reason==3 {
                    if let Some((sample_x,sample_z,sample_ground))=capture.samples {
                        std::ptr::copy_nonoverlapping(sample_x.as_ptr(),xs,count);
                        std::ptr::copy_nonoverlapping(sample_z.as_ptr(),zs,count);
                        std::ptr::copy_nonoverlapping(sample_ground.as_ptr(),ground,count);
                        (*output).stage=1;
                        return 12;
                    }
                }
                failure.reason as i32
            }
        }
    }));
    result.unwrap_or(10)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn input(a:&[f64;4],b:&[f64;4],zeros:&[f64;4],ones:&[f64;4],heights:&[u16;4])->DriveInput {
        DriveInput{abi_version:crate::ABI_VERSION,count:4,pose:Pose::default(),road:Pose::default(),
            motion:Motion::default(),lean:[0.;4],effort:[0.;3],distance:0.,yaw_delta:0.,dt:0.02,
            displacement:[0.;2],has_displacement:0,was_blocked:0,has_memory:0,hint_count:0,hints:[0;3],
            a:a.as_ptr(),b:b.as_ptr(),p:zeros.as_ptr(),weight:ones.as_ptr(),
            memory_heights:std::ptr::null(),memory_speeds:std::ptr::null(),
            constants:[0.008,0.06,9.81,0.28,0.1,32.,12.,0.],
            terrain:crate::Terrain{heights:heights.as_ptr(),size:2,min_x:-100.,min_z:-100.,
                width:200.,depth:200.,height_min:0.,height_range:0.}}
    }
    fn query()->SupportQuery {
        SupportQuery{x:0.,z:0.,yaw:0.,reference:Pose::default(),start:Pose::default(),
            road:Pose::default(),motion:Motion::default(),lean:[0.;4],h:0.02,centre:0.,hint_count:0,hints:[0;3]}
    }
    fn output(lower:&mut[f64;5],xs:&mut[f64;4],zs:&mut[f64;4],ground:&mut[f64;4])->SupportOutput {
        SupportOutput{candidate:Pose{x:123.,..Pose::default()},road:Pose{x:124.,..Pose::default()},
            spring_velocity:[125.;3],spring_lean:[126.;4],spring_angular:[127.;2],
            hint_count:128,hints:[129;3],stage:77,lower:lower.as_mut_ptr(),
            xs:xs.as_mut_ptr(),zs:zs.as_mut_ptr(),ground:ground.as_mut_ptr()}
    }
    fn pose_bits(p:Pose)->[u64;6] {[p.x,p.y,p.z,p.yaw,p.pitch,p.roll].map(f64::to_bits)}
    unsafe extern "C" fn flat(_: *mut c_void,count:u32,_:*const f64,_:*const f64,
        _:*const Pose,output:*mut f64)->i32 {
        std::slice::from_raw_parts_mut(output,count as usize).fill(0.);0
    }
    unsafe extern "C" fn reject(_: *mut c_void,_:*const Pose,_:*const Pose)->i32 {1}
    unsafe extern "C" fn planned(context:*mut c_void,count:u32,_:*const f64,_:*const f64,
        _:*const Pose,terrain:*const f64,_:*const f64,_:*const f64,_:*const f64,
        _:*const u32,_:u32,output:*mut f64)->i32 {
        if !context.is_null() {*(context as *mut usize)+=1;}
        std::ptr::copy_nonoverlapping(terrain,output,count as usize);0
    }
    unsafe extern "C" fn ledge(context:*mut c_void,count:u32,xs:*const f64,zs:*const f64,
        reference:*const Pose,terrain:*const f64,tops:*const f64,starts:*const f64,bounds:*const f64,
        raised:*const u32,nraised:u32,output:*mut f64)->i32 {
        planned(context,count,xs,zs,reference,terrain,tops,starts,bounds,raised,nraised,output);
        output.write(-5.);0
    }
    unsafe extern "C" fn failed(_: *mut c_void,_:u32,_:*const f64,_:*const f64,
        _:*const Pose,_:*const f64,_:*const f64,_:*const f64,_:*const f64,
        _:*const u32,_:u32,_:*mut f64)->i32 {7}
    #[test]
    fn old_layouts_are_unchanged_and_slice_layouts_are_explicit() {
        assert_eq!(size_of::<DriveInput>(),472);
        assert_eq!(size_of::<crate::DriveOutput>(),432);
        assert_eq!(size_of::<SupportQuery>(),296);
        assert_eq!(size_of::<SupportOutput>(),224);
    }
    #[test]
    fn invalid_alignment_or_missing_callback_never_reads_pointees() {
        assert_eq!(unsafe{tfs_ground_support_slice_v1(std::ptr::null(),std::ptr::null(),
            None,std::ptr::null_mut(),0.7,std::ptr::null_mut())},1);
    }
    #[test]
    fn complete_support_and_spring_match_the_old_drive_checkpoint_bits() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];let zero=[0.;4];let one=[1.;4];let heights=[0;4];
        let input=input(&a,&b,&zero,&one,&heights);
        let old=unsafe{drive::run(input,flat,reject,std::ptr::null_mut(),2)}.err().unwrap().prefix.unwrap();
        let mut lower=[44.;5];let mut xs=[45.;4];let mut zs=[46.;4];let mut ground=[47.;4];
        let mut out=output(&mut lower,&mut xs,&mut zs,&mut ground);let mut calls=0usize;
        assert_eq!(unsafe{tfs_ground_support_slice_v1(&input,&query(),Some(planned),
            (&mut calls as *mut usize).cast(),0.7,&mut out)},0);
        assert_eq!(calls,1);assert_eq!(out.stage,2);
        assert_eq!(pose_bits(out.candidate),pose_bits(old.rotated));
        assert_eq!(pose_bits(out.road),pose_bits(old.road));
        assert_eq!(out.spring_velocity.map(f64::to_bits),old.spring.velocity.map(f64::to_bits));
        assert_eq!(out.spring_lean.map(f64::to_bits),old.spring.lean.map(f64::to_bits));
        assert_eq!(out.spring_angular.map(f64::to_bits),old.spring.angular.map(f64::to_bits));
        assert_eq!(lower.map(f64::to_bits).as_slice(),old.lower_bounds.iter().map(|v|v.to_bits()).collect::<Vec<_>>());
        assert_eq!(xs,a);assert_eq!(zs,b);assert_eq!(ground,zero);
    }
    #[test]
    fn unsupported_footprint_returns_one_sample_prefix_without_fit_publication() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];let zero=[0.;4];let one=[1.;4];let heights=[0;4];
        let input=input(&a,&b,&zero,&one,&heights);
        let mut lower=[44.;5];let mut xs=[45.;4];let mut zs=[46.;4];let mut ground=[47.;4];
        let mut out=output(&mut lower,&mut xs,&mut zs,&mut ground);let mut calls=0usize;
        assert_eq!(unsafe{tfs_ground_support_slice_v1(&input,&query(),Some(ledge),
            (&mut calls as *mut usize).cast(),0.7,&mut out)},12);
        assert_eq!(calls,1);assert_eq!(out.stage,1);assert_eq!(out.candidate.x,123.);
        assert_eq!(out.road.x,124.);assert_eq!(out.hint_count,128);assert_eq!(lower,[44.;5]);
        assert_eq!(xs,a);assert_eq!(zs,b);assert_eq!(ground,[-5.,0.,0.,0.]);
    }
    #[test]
    fn callback_error_leaves_all_output_fields_and_buffers_untouched() {
        let a=[-1.,-1.,1.,1.];let b=[-2.,2.,-2.,2.];let zero=[0.;4];let one=[1.;4];let heights=[0;4];
        let input=input(&a,&b,&zero,&one,&heights);
        let mut lower=[44.;5];let mut xs=[45.;4];let mut zs=[46.;4];let mut ground=[47.;4];
        let mut out=output(&mut lower,&mut xs,&mut zs,&mut ground);
        assert_eq!(unsafe{tfs_ground_support_slice_v1(&input,&query(),Some(failed),
            std::ptr::null_mut(),0.7,&mut out)},4);
        assert_eq!(out.stage,77);assert_eq!(out.candidate.x,123.);assert_eq!(out.hint_count,128);
        assert_eq!(lower,[44.;5]);assert_eq!(xs,[45.;4]);assert_eq!(zs,[46.;4]);assert_eq!(ground,[47.;4]);
    }
}
