#ifndef TFS_SIM_CORE_ABI_H
#define TFS_SIM_CORE_ABI_H
#include <stdint.h>
#define TFS_SIM_ABI_VERSION 1

typedef struct { double x, y, z, yaw, pitch, roll; } TfsPose;
typedef struct {
    double velocity[3], suspension[3], angular[2];
} TfsMotion;
typedef struct {
    const uint16_t *heights;
    uint32_t size;
    double min_x, min_z, width, depth, height_min, height_range;
} TfsTerrain;
typedef struct {
    uint32_t abi_version, count;
    TfsPose pose, road;
    TfsMotion motion;
    double lean[4], effort[3];
    double distance, yaw_delta, dt, displacement[2];
    uint32_t has_displacement, was_blocked, has_memory, hint_count;
    uint32_t hints[3];
    const double *a, *b, *p, *weight, *memory_heights, *memory_speeds;
    /* skin, sweep_step, gravity, support_reach, suspension_travel,
       heave_omega, angular_omega, hull_clearance */
    double constants[8];
    TfsTerrain terrain;
} TfsDriveInput;
typedef struct {
    TfsPose pose, road;
    TfsMotion motion;
    double lean[4];
    uint32_t moved, blocked, carrying_supported, hint_count;
    uint32_t hints[3];
    TfsPose spring_pose, bounds_pose;
    double spring_velocity[3], spring_lean[4], spring_angular[2];
    /* Caller-owned output arrays: count, count, count+1 doubles. */
    double *memory_heights, *memory_speeds, *lower_bounds;
    uint32_t reason, support_queries, validation_queries;
} TfsDriveOutput;

/* sample writes count heights; reference is the carrying pose. */
typedef int32_t (*TfsSampleFn)(void *context, uint32_t count,
    const double *xs, const double *zs, const TfsPose *reference, double *heights);
/* Pure validation only: 0 clear, 1 decline/fallback, 2 callback error.
   No destruction, damage, impulses or final pose commitment here. */
typedef int32_t (*TfsValidateFn)(void *context,
    const TfsPose *candidate, const TfsPose *previous);

uint32_t tfs_sim_abi_version(void);
uint32_t tfs_sim_input_size(void);
uint32_t tfs_sim_output_size(void);
/* 0 accepted; nonzero falls back without applying any output state. */
int32_t tfs_ground_step(const TfsDriveInput *, TfsSampleFn, TfsValidateFn,
                        void *context, TfsDriveOutput *);

/* Optional planned-step entry point. Arrays and bounds are borrowed only for
   the synchronous callback; raised_count indices select additional rays. */
typedef int32_t (*TfsSamplePlanFn)(void *context, uint32_t count,
    const double *xs, const double *zs, const TfsPose *reference,
    const double *terrain, const double *tops, const double *starts,
    const double *bounds, const uint32_t *raised, uint32_t raised_count,
    double *heights);
int32_t tfs_ground_step_plan(const TfsDriveInput *, TfsSamplePlanFn, TfsValidateFn,
                             void *context, double max_slope, TfsDriveOutput *);

/* Optional first-translation checkpoint. Same input/output layouts.
   0: complete accepted step, with the usual output contract.
   11: reason=6 and support_queries=validation_queries=2. Only pose (the
       validated first rotation), road/bounds_pose (second support target),
       spring_pose/velocity/lean/angular, hints/hint_count, lower_bounds[count+1]
       and diagnostic fields are published. Other fields are untouched;
       memory_heights and memory_speeds buffers are untouched.
       This does NOT accept the translation. The caller must still perform
       its normal impact/destruction validation and remaining drive logic.
   All other statuses retain the usual decline/error contract. The original
   tfs_ground_step and tfs_ground_step_plan never return this checkpoint. */
int32_t tfs_ground_step_plan_prefix_v1(const TfsDriveInput *, TfsSamplePlanFn,
    TfsValidateFn, void *context, double max_slope, TfsDriveOutput *);

/* Optional v2 includes the rejected first rotation: status11 with both
   query counters1 publishes pose=rejected rotation, road/bounds=its support,
   spring fields=its suspension result. The caller restores its captured
   validation contacts and executes the ordinary rejection branch. Counters2
   retain v1 semantics. Neither status accepts a rejected candidate. */
int32_t tfs_ground_step_plan_prefix_v2(const TfsDriveInput *, TfsSamplePlanFn,
    TfsValidateFn, void *context, double max_slope, TfsDriveOutput *);

/* Pure support fit: rows is 1..512, phi_flat contains rows*3 doubles,
   hint_len is 0..3 (hint may be NULL only when hint_len is zero).
   Caller owns plane_out[3], active_out[3], and active_len. Outputs change
   only on success: 0 accepted, 1 decline/fallback, 2 caught native panic. */
int32_t tfs_support_plane(uint32_t rows, const double *phi_flat,
    const double *target, const double *weight, const double *lower,
    uint32_t hint_len, const uint32_t *hint, double *plane_out,
    uint32_t *active_out, uint32_t *active_len);

/* Pure ray plan; no Bullet queries or state changes. count is 1..256.
   ref_kind: 0 no reference (reference may be NULL), 1 scalar reference.y,
   2 carrying pose. Caller owns terrain_out[count], tops_out[count],
   starts_out[count], bounds_out[6] (loX,loY,loZ,hiX,hiY,hiZ),
   raised_out[count] (ascending indices; unused entries zero), raised_count.
   Outputs change only on success: 0 accepted, 1 decline, 2 caught panic. */
int32_t tfs_ground_plan(const TfsTerrain *terrain, uint32_t count,
    const double *xs, const double *zs, uint32_t ref_kind,
    const TfsPose *reference, double max_slope, double support_reach, double skin,
    double *terrain_out, double *tops_out, double *starts_out,
    double *bounds_out, uint32_t *raised_out, uint32_t *raised_count);

/* Optional pure preparation of the existing measured support fit. No geometry
   queries or world state. ABI field is 1, count is 1..256 and
   count+1+extra_count <= 512. a/b/p/weights/xs/zs/ground each contain count
   doubles. extra contains four doubles per row (a,b,height,compliant 0 or 1),
   and may be NULL when extra_count is zero. Held stations retain the caller's
   original path. full_rank and supported describe its unchanged full patch. */
typedef struct {
    uint32_t abi, count, extra_count, carrying, full_rank, supported;
    const double *a, *b, *p, *weights, *xs, *zs, *ground, *extra;
    double old_position[3], up[3];
    double compression, reach, skin, centre, clearance;
    uint32_t hint_count, hints[3];
} TfsMeasuredSupportInput;
typedef struct {
    double plane[3];
    uint32_t active[3], active_count, supported;
    /* Caller owns count+1+extra_count doubles. */
    double *lower;
} TfsMeasuredSupportOutput;
uint32_t tfs_measured_input_size(void);
uint32_t tfs_measured_output_size(void);
/* Outputs change only on success. 0 accepted; 1 invalid input;
   3 unsupported partial patch; 5 failed fit; 10 caught native panic. */
int32_t tfs_measured_support(const TfsMeasuredSupportInput *, TfsMeasuredSupportOutput *);
/* Optional pure support + spring slice; legacy drive layouts are unchanged. */
typedef struct {
    double x,z,yaw;
    TfsPose reference,start,road;
    TfsMotion motion;
    double lean[4],h,centre;
    uint32_t hint_count,hints[3];
} TfsSupportSliceInput;
typedef struct {
    TfsPose candidate,road;
    double spring_velocity[3],spring_lean[4],spring_angular[2];
    uint32_t hint_count,hints[3],stage;
    double *lower,*xs,*zs,*ground;
} TfsSupportSliceOutput;
uint32_t tfs_support_slice_abi(void);
uint32_t tfs_support_slice_input_size(void);
uint32_t tfs_support_slice_output_size(void);
/* Caller owns disjoint lower[count+1], xs/zs/ground[count].
   0: stage2 and full output; 12: stage1 and only sampled xs/zs/ground.
   Other errors leave all output buffers untouched. Borrowed callbacks. */
int32_t tfs_ground_support_slice_v1(const TfsDriveInput *,const TfsSupportSliceInput *,
    TfsSamplePlanFn,void *context,double max_slope,TfsSupportSliceOutput *);
#endif
