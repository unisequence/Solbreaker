/* TFS recon client bridge for the pinned 11.21.0.1235_7376187 ia32 client.
 *
 * Freestanding code linked at TFS_RECON_CODE_VA and appended to the owned RX
 * .mkray section by solbreaker/client_recon.py. All game addresses below are
 * link-time VAs for ImageBase 0x400000; the hook stubs pass the ASLR delta.
 * The state lives in the zero-initialised tail of .data (RW, NX). It is never
 * executed. Marker pointers are identities only. The lamp pointer has explicit
 * native initialization/destruction hooks, both on the owning UI thread.
 */
#ifndef TFS_RECON_CLIENT_H
#define TFS_RECON_CLIENT_H

#include <stdint.h>
#include "recon_wire.h"

#define TFS_RECON_CODE_VA      0x04b1c400u  /* .mkray + 0x400 */
#define TFS_RECON_STATE_VA     0x0471fc80u  /* .data tail, below _RDATA */
#define TFS_RECON_STATE_LIMIT  0x04720000u

/* Wire contract shared with solbreaker/recon_transport.py. */
#define TFS_RECON_MAGIC0       0x31534654u
#define TFS_RECON_MAGIC1       0x4e4f4345u
#define TFS_RECON_HELLO_SIZE   14u          /* "TFS-LAB-H", epoch, version 1 */
#define TFS_RECON_AIM_SIZE     22u          /* "TFS-LAB-A", epoch, seq, target, 1 */
#define TFS_RECON_HELLO_METHOD 31u          /* Avatar base method, kind 166 */

/* Timing, milliseconds of GetTickCount. The server heartbeat is one second. */
#define TFS_RECON_STALE_MS     3000u        /* restore native markers */
#define TFS_RECON_RESET_MS     5000u        /* drop the session, hello again */
#define TFS_RECON_RETRY_MS     2000u        /* unanswered hello */
#define TFS_RECON_ENEMY_MS     1000u        /* minimap trusts the 3D roster */
#define TFS_RECON_AIM_MS       100u         /* reticle report at least 10 Hz */

#define TFS_RECON_MAX_ENEMIES  16u
#define TFS_RECON_MAX_HIDDEN   16u

enum tfs_recon_phase {
    TFS_RECON_IDLE = 0,
    TFS_RECON_HELLO = 1,
    TFS_RECON_ACTIVE = 2
};

/* One marker root whose opacity this bridge set to zero. Identity only. */
struct tfs_recon_hidden {
    uint32_t id;
    uint32_t control;
};

/* Fixed layout: client_recon.py and runtime probes read these offsets. */
struct tfs_recon_state {
    uint32_t owner_thread;          /* 0x00 first hook thread; others refused */
    uint32_t phase;                 /* 0x04 enum tfs_recon_phase */
    uint32_t epoch;                 /* 0x08 nonzero while HELLO/ACTIVE */
    uint32_t epoch_counter;         /* 0x0c */
    uint32_t hello_tick;            /* 0x10 last hello attempt */
    uint32_t fresh_tick;            /* 0x14 last ack or accepted snapshot */
    uint32_t has_sequence;          /* 0x18 */
    uint32_t enemy_count;           /* 0x1c */
    uint32_t enemy_tick;            /* 0x20 */
    uint32_t hellos;                /* 0x24 diagnostic counters */
    uint32_t hello_failures;        /* 0x28 */
    uint32_t acks;                  /* 0x2c */
    uint32_t accepted;              /* 0x30 */
    uint32_t rejected;              /* 0x34 */
    uint32_t consumed;              /* 0x38 TFS envelopes kept from listeners */
    uint32_t foreign;               /* 0x3c hook calls on another thread */
    uint32_t resets;                /* 0x40 */
    uint32_t layer;                 /* 0x44 identity of the active 3D layer */
    uint32_t minimap;               /* 0x48 identity of the active minimap */
    uint32_t minimap_tick;          /* 0x4c */
    uint8_t battle[16];             /* 0x50 battle UUID from the ack */
    struct recon_message message;   /* 0x60 last accepted snapshot, 224 bytes */
    uint32_t enemies[TFS_RECON_MAX_ENEMIES];                 /* 0x140 */
    struct tfs_recon_hidden hidden3d[TFS_RECON_MAX_HIDDEN];  /* 0x180 */
    struct tfs_recon_hidden hiddenmap[TFS_RECON_MAX_HIDDEN]; /* 0x200 */
    uint32_t name[5];               /* 0x280 "UIOpacityComponent" */
    uint32_t name_string[6];        /* 0x294 MSVC std::string view of name */
    uint32_t aim_target;            /* 0x2ac last reported target, 0 = miss */
    uint32_t aim_tick;              /* 0x2b0 */
    uint32_t aim_sequence;          /* 0x2b4 process-wide, strictly increasing */
    uint32_t aims;                  /* 0x2b8 reports sent */
    uint32_t candidate;             /* 0x2bc completed segment in this marker computation */
    uint32_t candidate_valid;       /* 0x2c0 reset at ComputeMarker entry */
    uint32_t lamp_control;          /* 0x2c4 live UILamp, cleared by its destructor */
    uint32_t lamp_original;         /* 0x2c8 original visibility before ownership */
    uint32_t lamp_owned;            /* 0x2cc restore only our own visibility change */
    struct tfs_recon_hidden hiddenoutline[TFS_RECON_MAX_HIDDEN]; /* 0x2d0 */
    uint32_t outline_seen;          /* 0x350 diagnostic: valid enemy components */
    uint32_t outline_changed;       /* 0x354 diagnostic: native setter calls */
    uint32_t outline_writing;       /* 0x358 distinguish our writes from native intent */
    uint32_t armor_denied_scene;    /* 0x35c identity only: overlay already cleared */
    uint32_t armor_denied;          /* 0x360 diagnostic: denied native updates */
    uint32_t armor_controller;      /* 0x364 live controller; unbind/destructor clear it */
    uint32_t armor_waiting;         /* 0x368 last denied eligibility */
    uint32_t armor_refreshing;      /* 0x36c native refresh reentrancy guard */
};                                  /* 0x370 bytes */

/* C entry points called by the stubs in recon_client.c (cdecl). */
int tfs_recon_message(uint32_t delta, const uint32_t *first, const uint32_t *first_end,
                      const uint32_t *second, const uint32_t *second_end);
void tfs_recon_control(uint32_t delta, uint32_t *mailbox);
void tfs_recon_markers(uint32_t delta, uint32_t *layer);
void tfs_recon_minimap(uint32_t delta, uint32_t *minimap);
void tfs_recon_reset(uint32_t delta, uint32_t *layer);
void tfs_recon_ray(uint32_t delta, uint32_t *frame, uint32_t hit);
void tfs_recon_aim_begin(uint32_t delta);
void tfs_recon_aim_end(uint32_t delta, uint32_t *rotator);
void tfs_recon_lamp_bind(uint32_t delta, uint32_t *lamp);
void tfs_recon_lamp_unbind(uint32_t delta, uint32_t *lamp);
uint32_t tfs_recon_silhouette_request(uint32_t delta, uint32_t *component, uint32_t enabled);
int tfs_recon_armor_gate(uint32_t delta, uint32_t *controller);
void tfs_recon_armor_unbind(uint32_t delta, uint32_t *controller);

#endif
