/* TFS recon client bridge, pinned 11.21.0.1235_7376187 ia32 only.
 *
 * Translated from the lab proofs (Frida + temporary thunks) into one
 * freestanding unit. client_recon.py embeds the linked bytes, checks every
 * hooked instruction window and every referenced vtable/function before it
 * patches, and pins the output hash. Build (see client_recon.build_blob):
 *   gcc -m32 -O2 -ffreestanding ... -c recon_client.c recon_wire.c
 *   ld -m elf_i386 -Ttext=0x4b1c400 ...; objcopy -O binary -j .text
 *
 * Hooks (all on the owner thread, else no-op):
 *   0x1786630 Avatar base method 10 wrapper: after the original call, send
 *             the versioned hello on the same mailbox (method 31).
 *   0x177e3d5 Avatar client method 37 dispatch: every TFS-magic envelope is
 *             kept from the ordinary listeners; v1 ack/snapshot update state.
 *   0x1bc9370 BattleUILayer virtual slot 39: enemy roster, 3D marker opacity.
 *   0x1d9d410 MinimapObjectsController update callback: opacity of enemies
 *             confirmed by the 3D pass.
 *   0x1b9ac00 BattleUILayer destructor: forget the session of that layer.
 *   0x1e22f10 / 0x1e11426 / 0x1e23043 ComputeMarker: collect ray segments,
 *             commit only the final reticle result once per computation.
 *   0x1d867e9 / 0x1d7a380 UILamp initialization/destruction: lifetime of the
 *             stock lamp driven by the recipient's server snapshot.
 *
 * Enemy UI opacity, silhouette eligibility and the separate penetration
 * overlay are gated by fresh recipient state. Mesh visibility, aiming,
 * weapon parameters and allied markers are never written. Native disables
 * take precedence over restoring our silhouette gate. Without a fresh
 * acknowledged session markers return to native behavior.
 */
#include "recon_client.h"
#ifdef TFS_RECON_HOST_TEST
#include <string.h>
#endif

typedef char tfs_recon_state_size[sizeof(struct tfs_recon_state) == 0x370 ? 1 : -1];
typedef char tfs_recon_state_room[TFS_RECON_STATE_VA + 0x370 <= TFS_RECON_STATE_LIMIT ? 1 : -1];

/* Pinned addresses, ImageBase 0x400000. */
#define IAT_GET_TICK_COUNT    0x036ef0fcu
#define MESSAGE_BEGIN         0x0280dc00u
#define STREAM_VTABLE         0x039e2fb8u
#define STREAM_RESERVE        0x0282e650u
#define CHANNEL_VTABLE        0x039de150u
#define CHANNEL_SEND          0x028097b0u
#define AVATAR_ENTITY_VTABLE  0x037f7058u  /* Avatar; IDs change between battles */
#define LAYER_VTABLE          0x0382ee60u
#define MARKER_VTABLE         0x0382f348u
#define MARKER_VIEW_VTABLE    0x0383066cu
#define OPACITY_VTABLE        0x03781ee4u
#define GET_COMPONENT         0x00ce0530u
#define SET_OPACITY           0x0102ab30u
#define MINIMAP_VTABLE        0x0384aa58u
#define MINIMAP_MARKERS_VTABLE 0x0384acacu
#define HASH_FIND             0x0092cac0u
#define ENEMY_RELATION        2u
#define OPACITY_ONE           0x3f800000u
#define COMPUTE_MARKER_RETURN 0x01e23043u
#define AVATAR_LOGIC_VTABLE   0x03801fc4u  /* AvatarGameLogic */
#define CONTEXT_VTABLE        0x037f86d0u  /* AvatarContextBattle */
#define VEHICLE_LOGIC_VTABLE  0x038023d4u  /* VehicleGameLogic */
#define VEHICLE_VTABLE        0x037f7d8cu  /* Vehicle */
#define TANK_VISUAL_VTABLE    0x03713afcu  /* TankVisual */
#define ENTITY_MAILBOX        0x7cu        /* as 0x1e1d6c4 builds it */
#define MAX_VEHICLES          64u
#define MAX_TREE_NODES        64u
#define MAX_TREE_STACK        32u
#define LAMP_VTABLE           0x0384876cu
#define SET_VISIBLE           0x00d05020u
#define SILHOUETTE_VTABLE     0x037115f4u
#define SILHOUETTE_TYPE       0x04672f80u
#define ENTITY_GET_COMPONENT  0x00d55cb0u
#define SET_SILHOUETTE        0x00786f90u
#define ARMOR_CONTROLLER      0x0385547cu
#define CLEAR_ARMOR_OVERLAY    0x007be4d0u
#define UPDATE_ARMOR           0x01e21a30u

#define FIELD(p, off) (((uint32_t *)(p))[(off) / 4])
#define BYTE(p, off) (((uint8_t *)(p))[off])
#define BELOW(p, off) (((uint32_t *)(p))[-(int)((off) / 4)])

typedef uint32_t (__attribute__((stdcall)) *tick_fn)(void);
typedef uint32_t *(__attribute__((thiscall)) *begin_fn)(uint32_t *, uint32_t, uint32_t, uint8_t *);
typedef uint8_t *(__attribute__((thiscall)) *reserve_fn)(uint32_t *, uint32_t);
typedef uint32_t *(__attribute__((thiscall)) *component_fn)(uint32_t *, const uint32_t *, uint32_t);
typedef void (__attribute__((thiscall)) *opacity_fn)(uint32_t *, uint32_t);
typedef uint32_t **(__attribute__((thiscall)) *find_fn)(uint32_t *, uint32_t **, const uint32_t *);
typedef void (__attribute__((thiscall)) *visible_fn)(uint32_t *, uint32_t);
typedef void (__attribute__((thiscall)) *update_fn)(uint32_t *);

static void set_visible(uint32_t delta, uint32_t *control, uint32_t visible) {
#ifdef TFS_RECON_HOST_TEST
    extern void tfs_test_visible(uint32_t *, uint32_t);
    (void)delta;
    tfs_test_visible(control, visible);
#else
    ((visible_fn)(SET_VISIBLE + delta))(control, visible);
#endif
}

static void clear_armor(uint32_t delta, uint32_t *scene) {
#ifdef TFS_RECON_HOST_TEST
    extern void tfs_test_clear_armor(uint32_t *);
    (void)delta;
    tfs_test_clear_armor(scene);
#else
    ((visible_fn)(CLEAR_ARMOR_OVERLAY + delta))(scene, 1);
#endif
}

static uint32_t *scene_component(uint32_t delta, uint32_t *scene, uint32_t *type) {
#ifdef TFS_RECON_HOST_TEST
    extern uint32_t *tfs_test_scene_component(uint32_t *, uint32_t *);
    (void)delta;
    return tfs_test_scene_component(scene, type);
#else
    return ((component_fn)(ENTITY_GET_COMPONENT + delta))(scene, type, 0);
#endif
}

#ifndef TFS_RECON_HOST_TEST
/* The decoder and compiler-generated copies need these; no libc is linked. */
void *memset(void *target, int value, size_t length) {
    uint8_t *p = target;
    while (length--) *p++ = (uint8_t)value;
    return target;
}

void *memcpy(void *target, const void *source, size_t length) {
    uint8_t *p = target;
    const uint8_t *q = source;
    while (length--) *p++ = *q++;
    return target;
}

int memcmp(const void *left, const void *right, size_t length) {
    const uint8_t *p = left, *q = right;
    for (; length; --length, ++p, ++q)
        if (*p != *q) return *p < *q ? -1 : 1;
    return 0;
}
#endif

#ifdef TFS_RECON_HOST_TEST
uint32_t tfs_test_thread = 1;
static uint32_t thread_id(void) { return tfs_test_thread; }
static uint32_t cycle_counter(void) { return 0x5a5a5a5au; }
#else
static uint32_t thread_id(void) {
    uint32_t id;
    __asm__ volatile ("movl %%fs:0x24, %0" : "=r"(id));
    return id;
}

static uint32_t cycle_counter(void) {
    uint32_t low;
    __asm__ volatile ("rdtsc" : "=a"(low) : : "edx");
    return low;
}
#endif

static struct tfs_recon_state *state(uint32_t delta) {
    return (struct tfs_recon_state *)(TFS_RECON_STATE_VA + delta);
}

static uint32_t now_ms(uint32_t delta) {
    return (*(tick_fn *)(IAT_GET_TICK_COUNT + delta))();
}

static int owned(struct tfs_recon_state *s) {
    uint32_t id = thread_id();
    if (!s->owner_thread) s->owner_thread = id;
    if (s->owner_thread == id) return 1;
    ++s->foreign;
    return 0;
}

static void forget_session(struct tfs_recon_state *s) {
    s->phase = TFS_RECON_IDLE;
    s->epoch = 0;
    s->has_sequence = 0;
    s->enemy_count = 0;
    s->armor_denied_scene = 0;
    memset(s->battle, 0, sizeof(s->battle));
    memset(&s->message, 0, sizeof(s->message));
}

static int live(const struct tfs_recon_state *s, uint32_t now) {
    return s->phase == TFS_RECON_ACTIVE && now - s->fresh_tick <= TFS_RECON_STALE_MS;
}

static int marked(const struct tfs_recon_state *s, uint32_t id) {
    uint32_t i;
    for (i = 0; i < s->message.count && i < RECON_MAX_IDS; ++i)
        if (s->message.ids[i] == id) return 1;
    return 0;
}

/* UILamp is a stock control with the original SixSense sprite and layout.
 * Unlike enemy marker roots its visibility belongs to this snapshot while
 * recon is negotiated. Nothing is changed for an unmodified server. */
void tfs_recon_lamp_bind(uint32_t delta, uint32_t *lamp) {
    struct tfs_recon_state *s = state(delta);
    if (!lamp || FIELD(lamp, 0) != LAMP_VTABLE + delta || !owned(s)) return;
    if (!s->lamp_control || s->lamp_control == (uint32_t)lamp) {
        s->lamp_control = (uint32_t)lamp;
        s->lamp_owned = 0;
    }
}

void tfs_recon_lamp_unbind(uint32_t delta, uint32_t *lamp) {
    struct tfs_recon_state *s = state(delta);
    if (s->owner_thread != thread_id() || s->lamp_control != (uint32_t)lamp) return;
    s->lamp_control = s->lamp_owned = 0;
}

static void lamp_update(uint32_t delta, struct tfs_recon_state *s, uint32_t now) {
    uint32_t *lamp = (uint32_t *)s->lamp_control;
    if (!lamp || FIELD(lamp, 0) != LAMP_VTABLE + delta) return;
    if (live(s, now)) {
        if (!s->lamp_owned) {
            s->lamp_original = (BYTE(lamp, 0x4c) >> 1) & 1u;
            s->lamp_owned = 1;
        }
        set_visible(delta, lamp, s->message.lamp != 0);
    } else if (s->lamp_owned) {
        set_visible(delta, lamp, s->lamp_original);
        s->lamp_owned = 0;
    }
}

#ifdef TFS_RECON_HOST_TEST
void tfs_test_lamp_update(uint32_t delta) {
    struct tfs_recon_state *s = state(delta);
    if (owned(s)) lamp_update(delta, s, now_ms(delta));
}
#endif

/* ---- Avatar method 37 ------------------------------------------------- */

static void accept_ack(struct tfs_recon_state *s, const uint32_t *words, uint32_t now) {
    uint32_t i, nonzero = 0;
    for (i = 4; i < 8; ++i) nonzero |= words[i];
    if (words[2] != 1 || s->phase != TFS_RECON_HELLO || !s->epoch ||
        words[3] != s->epoch || !nonzero) {
        ++s->rejected;
        return;
    }
    forget_session(s);
    s->epoch = words[3];
    memcpy(s->battle, words + 4, sizeof(s->battle));
    s->phase = TFS_RECON_ACTIVE;
    s->fresh_tick = now;
    ++s->acks;
}

static void accept_snapshot(struct tfs_recon_state *s, const uint32_t *words, uint32_t count,
                            uint32_t now) {
    uint8_t raw[RECON_MAX_PAYLOAD];
    struct recon_message message;
    /* Rebuild the method body read by the client: two packed u32 vectors. */
    raw[0] = (uint8_t)count;
    memcpy(raw + 1, words, count * 4u);
    raw[1 + count * 4u] = 0;
    if (s->phase != TFS_RECON_ACTIVE ||
        !tfs_recon_decode(raw, 2u + count * 4u, s->battle, s->message.sequence,
                          s->has_sequence, &message)) {
        ++s->rejected;
        return;
    }
    s->message = message;
    s->has_sequence = 1;
    s->fresh_tick = now;
    ++s->accepted;
}

int tfs_recon_message(uint32_t delta, const uint32_t *first, const uint32_t *first_end,
                      const uint32_t *second, const uint32_t *second_end) {
    struct tfs_recon_state *s = state(delta);
    uint32_t size, count;
    if (!first || first_end < first) return 0;
    size = (uint32_t)((const uint8_t *)first_end - (const uint8_t *)first);
    if (size < 8 || size % 4 || first[0] != TFS_RECON_MAGIC0 || first[1] != TFS_RECON_MAGIC1)
        return 0;
    /* A vanilla listener must never interpret a TFS envelope as entity IDs. */
    ++s->consumed;
    count = size / 4;
    if (!owned(s) || second != second_end || count > 60) {
        ++s->rejected;
        return 1;
    }
    if (count == 8)
        accept_ack(s, first, now_ms(delta));
    else if (count >= 11)
        accept_snapshot(s, first, count, now_ms(delta));
    else
        ++s->rejected;
    return 1;
}

/* ---- Hello on Avatar base method 31 ------------------------------------ */

static int avatar_owner(uint32_t delta, uint32_t *owner) {
    return owner && FIELD(owner, 0) == AVATAR_ENTITY_VTABLE + delta && FIELD(owner, 0x1c) != 0;
}

#ifdef TFS_RECON_HOST_TEST
int tfs_test_avatar_owner(uint32_t delta, uint32_t *owner) { return avatar_owner(delta, owner); }
#endif

/* One "TFS-LAB-<kind>" message, LE words, version 1, on Avatar base method 31. */
static int send_report(uint32_t delta, uint32_t *mailbox, uint8_t kind,
                       const uint32_t *words, uint32_t count) {
    uint32_t *owner, *channel, *stream, i;
    uint8_t *body, status = 0xff;
    if (!mailbox || !(owner = (uint32_t *)FIELD(mailbox, 0)) || !avatar_owner(delta, owner) ||
        !(channel = (uint32_t *)FIELD(owner, 0x30)) ||
        FIELD(channel, 0) != CHANNEL_VTABLE + delta ||
        FIELD(FIELD(channel, 0), 0x40) != CHANNEL_SEND + delta)
        return 0;
    stream = ((begin_fn)(MESSAGE_BEGIN + delta))(mailbox, TFS_RECON_HELLO_METHOD, 1, &status);
    if (status || !stream || FIELD(stream, 0) != STREAM_VTABLE + delta ||
        FIELD(FIELD(stream, 0), 4) != STREAM_RESERVE + delta)
        return 0;
    body = ((reserve_fn)FIELD(FIELD(stream, 0), 4))(stream, 10 + count * 4);
    if (!body) return 0;
    body[0] = 'T'; body[1] = 'F'; body[2] = 'S'; body[3] = '-'; body[4] = 'L';
    body[5] = 'A'; body[6] = 'B'; body[7] = '-'; body[8] = kind;
    for (i = 0; i < count * 4; ++i) body[9 + i] = (uint8_t)(words[i / 4] >> (i % 4 * 8));
    body[9 + count * 4] = 1;
    return 1;
}

void tfs_recon_control(uint32_t delta, uint32_t *mailbox) {
    struct tfs_recon_state *s = state(delta);
    uint32_t now;
    if (!owned(s)) return;
    now = now_ms(delta);
    if (s->phase == TFS_RECON_ACTIVE) {
        if (now - s->fresh_tick <= TFS_RECON_RESET_MS) return;
        forget_session(s);
        ++s->resets;
    }
    if (s->hello_tick && now - s->hello_tick < TFS_RECON_RETRY_MS) return;
    s->hello_tick = now ? now : 1;
    if (s->phase == TFS_RECON_IDLE) {
        uint32_t epoch = now ^ cycle_counter() ^ (++s->epoch_counter * 0x9e3779b9u);
        s->epoch = epoch ? epoch : 1;
    }
    if (send_report(delta, mailbox, 'H', &s->epoch, 1)) {
        s->phase = TFS_RECON_HELLO;
        ++s->hellos;
    } else {
        ++s->hello_failures;
    }
}

void tfs_recon_reset(uint32_t delta, uint32_t *layer) {
    struct tfs_recon_state *s = state(delta);
    /* Only the destruction of the BattleUILayer this bridge drives ends it. */
    if (!layer || (uint32_t)layer != s->layer || !s->owner_thread ||
        s->owner_thread != thread_id())
        return;
    forget_session(s);
    s->layer = 0;
    s->armor_controller = s->armor_waiting = 0;
    memset(s->hidden3d, 0, sizeof(s->hidden3d));  /* roots died with the layer */
    memset(s->hiddenoutline, 0, sizeof(s->hiddenoutline));
    s->hello_tick = 0;
    ++s->resets;
}

/* ---- Marker opacity ---------------------------------------------------- */

static uint32_t *opacity_component(uint32_t delta, struct tfs_recon_state *s, uint32_t *root) {
    uint32_t *component;
    if (!s->name[0]) {
        s->name[0] = 0x704f4955u; s->name[1] = 0x74696361u; s->name[2] = 0x6d6f4379u;
        s->name[3] = 0x656e6f70u; s->name[4] = 0x0000746eu;
    }
    memset(s->name_string, 0, sizeof(s->name_string));
    s->name_string[0] = (uint32_t)s->name;
    s->name_string[4] = 18;
    s->name_string[5] = 18;
    component = ((component_fn)(GET_COMPONENT + delta))(root, s->name_string, 0);
    if (!component || FIELD(component, 0) != OPACITY_VTABLE + delta ||
        FIELD(component, 8) != (uint32_t)root)
        return 0;
    return component;
}

static struct tfs_recon_hidden *hidden(struct tfs_recon_hidden *table, uint32_t id,
                                       uint32_t control) {
    uint32_t i;
    for (i = 0; i < TFS_RECON_MAX_HIDDEN; ++i)
        if (table[i].id == id && table[i].control == control) return table + i;
    return 0;
}

/* Desired opacity for one confirmed enemy root. Returns the slot to keep. */
static int apply(uint32_t delta, struct tfs_recon_state *s, struct tfs_recon_hidden *table,
                 uint32_t id, uint32_t *root, int visible) {
    uint32_t *component = opacity_component(delta, s, root), i;
    struct tfs_recon_hidden *slot = hidden(table, id, (uint32_t)root);
    opacity_fn set = (opacity_fn)(SET_OPACITY + delta);
    if (!component) return -1;
    if (visible) {
        if (slot) {
            if (FIELD(component, 0x14) == 0) set(component, OPACITY_ONE);
            slot->id = slot->control = 0;
        }
        return -1;
    }
    if (!slot) {
        /* Never hide what could not be restored later. */
        for (i = 0; i < TFS_RECON_MAX_HIDDEN && table[i].id; ++i) {}
        if (i == TFS_RECON_MAX_HIDDEN) return -1;
        slot = table + i;
        slot->id = id;
        slot->control = (uint32_t)root;
    }
    if (FIELD(component, 0x14) != 0) set(component, 0);
    return (int)(slot - table);
}

/* One live instance owns the tables; a second concurrent one is ignored. */
static int single(uint32_t *identity, uint32_t *tick, uint32_t object, uint32_t now) {
    if (*identity != object && *identity && now - *tick <= TFS_RECON_ENEMY_MS) return 0;
    *identity = object;
    *tick = now;
    return 1;
}

/* Slots not reached in a complete pass belong to destroyed roots. */
static void prune(struct tfs_recon_hidden *table, uint32_t kept) {
    uint32_t i;
    for (i = 0; i < TFS_RECON_MAX_HIDDEN; ++i)
        if (!(kept & (1u << i))) table[i].id = table[i].control = 0;
}

/* Preserve a later native disable (death, UI mode change, etc.) even if
 * our gate already wrote zero. A matching zero alone cannot tell who owns it. */
uint32_t tfs_recon_silhouette_request(uint32_t delta, uint32_t *component, uint32_t enabled) {
    struct tfs_recon_state *s = state(delta);
    uint32_t i, scene;
    if (!component || s->owner_thread != thread_id() || s->outline_writing ||
        FIELD(component, 0) != SILHOUETTE_VTABLE + delta) return enabled;
    scene = FIELD(component, 8);
    for (i = 0; i < TFS_RECON_MAX_HIDDEN; ++i) {
        struct tfs_recon_hidden *slot = s->hiddenoutline + i;
        if (slot->id && slot->control == scene) {
            if (!(enabled & 0xffu)) slot->id = slot->control = 0;
            else if (live(s, now_ms(delta)) && !marked(s, slot->id)) enabled = 0;
            break;
        }
    }
    return enabled;
}

static void set_outline(uint32_t delta, struct tfs_recon_state *s, uint32_t *component,
                         uint32_t enabled) {
    s->outline_writing = 1;
    ((visible_fn)(SET_SILHOUETTE + delta))(component, enabled);
    s->outline_writing = 0;
    ++s->outline_changed;
}

static void armor_refresh(uint32_t delta, struct tfs_recon_state *s, uint32_t *controller) {
    uint32_t previous;
    if (s->armor_refreshing) return;
    if (!controller || !BYTE(controller, 0x14)) {
        if (controller) tfs_recon_armor_unbind(delta, controller);
        return;
    }
    previous = s->armor_waiting;
    if (!tfs_recon_armor_gate(delta, controller) && previous) {
        /* The native controller is event-driven, so a server-only eligibility
         * change needs one refresh even when the player holds the mouse still. */
        s->armor_refreshing = 1;
#ifdef TFS_RECON_HOST_TEST
        extern void tfs_test_update_armor(uint32_t *);
        tfs_test_update_armor(controller);
#else
        ((update_fn)(UPDATE_ARMOR + delta))(controller);
#endif
        s->armor_refreshing = 0;
    }
}

/* The silhouette gate is independent of the mesh. Never follow a retained
 * scene address: every component comes from the current VehiclesInBattle.
 * Track only a native true->false change which this bridge can restore. */
static void outlines_update(uint32_t delta, struct tfs_recon_state *s, uint32_t *avatar) {
    uint32_t *context, *vehicles, *begin, *end, *item, *vehicle, *scene, *component, *type;
    uint32_t i, j, count, id, now, kept = 0;
    uint32_t *armor = (uint32_t *)s->armor_controller, *bound = 0, *target = 0;
    int current_owner = 0, current_target = 0;
    int enemy, visible;
    struct tfs_recon_hidden *slot;
    if (!(context = (uint32_t *)FIELD(avatar, 0x1f8)) ||
        FIELD(context, 0) != CONTEXT_VTABLE + delta ||
        !(vehicles = (uint32_t *)FIELD(context, 0x30))) return;
    begin = (uint32_t *)FIELD(vehicles, 8);
    end = (uint32_t *)FIELD(vehicles, 12);
    if (!begin || end < begin || (uint32_t *)FIELD(vehicles, 16) < end ||
        (count = (uint32_t)(end - begin)) > MAX_VEHICLES ||
        !(type = (uint32_t *)FIELD(SILHOUETTE_TYPE + delta, 0))) return;
    now = now_ms(delta);
    if (armor && FIELD(armor, 0) == ARMOR_CONTROLLER + delta) {
        bound = (uint32_t *)FIELD(armor, 4);
        target = (uint32_t *)FIELD(armor, 0x10);
        /* GameLogicAware can bind an interior context-provider pointer.
         * Its pinned +4 getter exposes AvatarContextBattle, not necessarily
         * the AvatarGameLogic primary pointer. The unbind hook bounds its life. */
        current_owner = bound == avatar || (bound && FIELD(bound, 4) == (uint32_t)context);
    }
    for (i = 0; i < count; ++i) {
        if (!(item = (uint32_t *)begin[i]) || FIELD(item, 0) != VEHICLE_LOGIC_VTABLE + delta ||
            !(vehicle = (uint32_t *)FIELD(item, 4)) || FIELD(vehicle, 0) != VEHICLE_VTABLE + delta ||
            !(scene = (uint32_t *)FIELD(item, 0x74)) || FIELD(scene, 0) != TANK_VISUAL_VTABLE + delta)
            return; /* Incomplete roster: don't prune or guess identities. */
        if (item == bound) current_owner = 1;
        if (item == target) current_target = 1;
        id = FIELD(vehicle, 0x1c);
        enemy = 0;
        if (now - s->enemy_tick <= TFS_RECON_ENEMY_MS)
            for (j = 0; j < s->enemy_count && j < TFS_RECON_MAX_ENEMIES; ++j)
                if (s->enemies[j] == id) { enemy = 1; break; }
        slot = hidden(s->hiddenoutline, id, (uint32_t)scene);
        if (!enemy && !slot) continue;
        component = scene_component(delta, scene, type);
        if (!component || FIELD(component, 0) != SILHOUETTE_VTABLE + delta ||
            FIELD(component, 8) != (uint32_t)scene || BYTE(component, 0x10) > 1) continue;
        ++s->outline_seen;
        visible = !enemy || !live(s, now) || marked(s, id);
        if (visible) {
            if (slot && !BYTE(component, 0x10)) {
                set_outline(delta, s, component, 1);
            }
            if (slot) slot->id = slot->control = 0;
        } else {
            if (!slot && BYTE(component, 0x10)) {
                for (j = 0; j < TFS_RECON_MAX_HIDDEN && s->hiddenoutline[j].id; ++j) {}
                if (j == TFS_RECON_MAX_HIDDEN) continue;
                slot = s->hiddenoutline + j;
                slot->id = id;
                slot->control = (uint32_t)scene;
            }
            if (slot) {
                if (BYTE(component, 0x10)) {
                    set_outline(delta, s, component, 0);
                }
                kept |= 1u << (uint32_t)(slot - s->hiddenoutline);
            }
        }
    }
    prune(s->hiddenoutline, kept);
    /* Controller lifetime is hooked separately. Its bound logic and target
     * must additionally be current world objects before any deferred call. */
    if (current_owner && current_target) armor_refresh(delta, s, armor);
}

/* Native ArmorHighlightController computes a separate penetration overlay.
 * Let it handle all allowed targets. A denied update clears its own overlay
 * once and skips that update; it never changes the tank mesh or aim target. */
int tfs_recon_armor_gate(uint32_t delta, uint32_t *controller) {
    struct tfs_recon_state *s = state(delta);
    uint32_t *target, *entity, *scene, id, i, now;
    if (!controller || FIELD(controller, 0) != ARMOR_CONTROLLER + delta || !owned(s)) return 0;
    /* Match the native update's first guard before touching target/rendering
     * state. An inactive controller must not become a deferred callback. */
    if (!BYTE(controller, 0x14)) {
        tfs_recon_armor_unbind(delta, controller);
        return 0;
    }
    s->armor_controller = (uint32_t)controller;
    s->armor_waiting = 0;
    now = now_ms(delta);
    if (!live(s, now) || now - s->enemy_tick > TFS_RECON_ENEMY_MS ||
        !(target = (uint32_t *)FIELD(controller, 0x10)) || FIELD(target, 0) != VEHICLE_LOGIC_VTABLE + delta ||
        !(entity = (uint32_t *)FIELD(target, 4)) || FIELD(entity, 0) != VEHICLE_VTABLE + delta ||
        !(scene = (uint32_t *)FIELD(target, 0x74)) || FIELD(scene, 0) != TANK_VISUAL_VTABLE + delta) {
        s->armor_denied_scene = 0;
        return 0;
    }
    id = FIELD(entity, 0x1c);
    for (i = 0; i < s->enemy_count && i < TFS_RECON_MAX_ENEMIES; ++i)
        if (s->enemies[i] == id) break;
    if (i == s->enemy_count || i == TFS_RECON_MAX_ENEMIES || marked(s, id)) {
        s->armor_denied_scene = 0;
        return 0;
    }
    if (s->armor_denied_scene != (uint32_t)scene) {
        clear_armor(delta, scene);
        s->armor_denied_scene = (uint32_t)scene;
    }
    ++s->armor_denied;
    s->armor_waiting = 1;
    return 1;
}

void tfs_recon_armor_unbind(uint32_t delta, uint32_t *controller) {
    struct tfs_recon_state *s = state(delta);
    if (s->owner_thread != thread_id() || s->armor_controller != (uint32_t)controller) return;
    s->armor_controller = s->armor_waiting = s->armor_denied_scene = 0;
}

void tfs_recon_markers(uint32_t delta, uint32_t *layer) {
    struct tfs_recon_state *s = state(delta);
    uint32_t *stack[MAX_TREE_STACK], *head, *node, *marker, *view, *root;
    uint32_t depth = 0, nodes = 0, enemies = 0, kept = 0, now, key;
    int show_all, slot;
    if (!layer || FIELD(layer, 0) != LAYER_VTABLE + delta || !owned(s)) return;
    now = now_ms(delta);
    lamp_update(delta, s, now);
    if (!single(&s->layer, &s->enemy_tick, (uint32_t)layer, now)) {
        ++s->foreign;
        return;
    }
    show_all = !live(s, now);
    /* MSVC std::map<int, VehicleUIObject*>: head->parent is the root. */
    if (!(head = (uint32_t *)FIELD(layer, 0x19c))) return;
    stack[depth++] = (uint32_t *)FIELD(head, 4);
    while (depth) {
        node = stack[--depth];
        if (!node || BYTE(node, 0xd)) continue;
        if (++nodes > MAX_TREE_NODES || depth + 2 > MAX_TREE_STACK) {
            s->enemy_count = 0;   /* incomplete pass: no roster, no pruning */
            return;
        }
        stack[depth++] = (uint32_t *)FIELD(node, 0);
        stack[depth++] = (uint32_t *)FIELD(node, 8);
        key = FIELD(node, 0x10);
        marker = (uint32_t *)FIELD(node, 0x14);
        if (!key || !marker || FIELD(marker, 0) != MARKER_VTABLE + delta ||
            FIELD(marker, 0x28) != key || FIELD(marker, 0xa0) != ENEMY_RELATION)
            continue;
        if (enemies < TFS_RECON_MAX_ENEMIES) s->enemies[enemies++] = key;
        if (!(view = (uint32_t *)FIELD(marker, 0x70)) ||
            FIELD(view, 0) != MARKER_VIEW_VTABLE + delta ||
            !(root = (uint32_t *)FIELD(view, 0x14)))
            continue;
        slot = apply(delta, s, s->hidden3d, key, root, show_all || marked(s, key));
        if (slot >= 0) kept |= 1u << slot;
    }
    prune(s->hidden3d, kept);
    s->enemy_count = enemies;
    s->enemy_tick = now;
}

static uint32_t *find(uint32_t delta, uint32_t *map, uint32_t key) {
    uint32_t *node = 0;
    ((find_fn)(HASH_FIND + delta))(map, &node, &key);
    return !node || node == (uint32_t *)FIELD(map, 4) ? 0 : node;
}

void tfs_recon_minimap(uint32_t delta, uint32_t *minimap) {
    struct tfs_recon_state *s = state(delta);
    uint32_t ids[TFS_RECON_MAX_ENEMIES + TFS_RECON_MAX_HIDDEN];
    uint32_t count = 0, enemies = 0, kept = 0, i, j, now, id;
    uint32_t *markers, *node, *root;
    int show_all, slot;
    if (!minimap || FIELD(minimap, 0) != MINIMAP_VTABLE + delta || !owned(s)) return;
    markers = (uint32_t *)FIELD(minimap, 0x12c);
    if (!markers || FIELD(markers, 0) != MINIMAP_MARKERS_VTABLE + delta) return;
    now = now_ms(delta);
    if (!single(&s->minimap, &s->minimap_tick, (uint32_t)minimap, now)) {
        ++s->foreign;
        return;
    }
    show_all = !live(s, now);
    /* Enemy identity comes only from the 3D layer's relation check. */
    if (s->enemy_count <= TFS_RECON_MAX_ENEMIES && now - s->enemy_tick <= TFS_RECON_ENEMY_MS)
        for (; enemies < s->enemy_count; ++enemies) ids[count++] = s->enemies[enemies];
    /* Roots hidden earlier are restored even when the roster is stale. */
    for (i = 0; i < TFS_RECON_MAX_HIDDEN; ++i) {
        if (!(id = s->hiddenmap[i].id)) continue;
        for (j = 0; j < count && ids[j] != id; ++j) {}
        if (j == count) ids[count++] = id;
    }
    for (i = 0; i < count; ++i) {
        if (!(node = find(delta, minimap + 0x130 / 4, ids[i]))) continue;
        if (!(node = find(delta, markers + 1, FIELD(node, 0xc)))) continue;
        if (!(root = (uint32_t *)FIELD(node, 0xc))) continue;
        slot = apply(delta, s, s->hiddenmap, ids[i], root,
                     i >= enemies || show_all || marked(s, ids[i]));
        if (slot >= 0) kept |= 1u << slot;
    }
    prune(s->hiddenmap, kept);
}

/* ---- Reticle target ---------------------------------------------------- */

/* Vehicle ID whose TankVisual the reticle ray hit: 0 miss, -1 unknown. */
static int64_t reticle_target(uint32_t delta, uint32_t *frame, uint32_t **entity, int hit) {
    uint32_t *visual = hit ? (uint32_t *)BELOW(frame, 0x5c) : 0, *rotator;
    uint32_t *logic, *context, *vehicles, *begin, *end, *vehicle, *scene, *item;
    uint32_t i, count, found = 0, matches = 0;
    /* Read the completed ray's output visual, never a BVH candidate. Unknown
       visuals (terrain/props) are compared only, not dereferenced. */
    if (!(rotator = (uint32_t *)BELOW(frame, 0x24)) ||
        !(logic = (uint32_t *)FIELD(rotator, 0x10)) ||
        FIELD(logic, 0) != AVATAR_LOGIC_VTABLE + delta ||
        !(*entity = (uint32_t *)FIELD(logic, 4)) ||
        !(context = (uint32_t *)FIELD(logic, 0x1f8)) ||
        FIELD(context, 0) != CONTEXT_VTABLE + delta ||
        !(vehicles = (uint32_t *)FIELD(context, 0x30)))
        return -1;
    begin = (uint32_t *)FIELD(vehicles, 8);
    end = (uint32_t *)FIELD(vehicles, 12);
    if (!begin || end < begin || (uint32_t *)FIELD(vehicles, 16) < end ||
        (count = (uint32_t)(end - begin)) > MAX_VEHICLES)
        return -1;
    for (i = 0; i < count; ++i) {
        if (!(item = (uint32_t *)begin[i]) || FIELD(item, 0) != VEHICLE_LOGIC_VTABLE + delta ||
            !(vehicle = (uint32_t *)FIELD(item, 4)) || FIELD(vehicle, 0) != VEHICLE_VTABLE + delta ||
            !(scene = (uint32_t *)FIELD(item, 0x74)) || FIELD(scene, 0) != TANK_VISUAL_VTABLE + delta)
            return -1;
        if (visual && scene == visual) {
            found = FIELD(vehicle, 0x1c);
            ++matches;
        }
    }
    if (matches > 1) return -1;
    return found;
}

#ifdef TFS_RECON_HOST_TEST
int64_t tfs_test_reticle(uint32_t delta, uint32_t *frame) {
    uint32_t *entity = 0;
    return reticle_target(delta, frame, &entity, 1);
}
#endif

void tfs_recon_ray(uint32_t delta, uint32_t *frame, uint32_t hit) {
    struct tfs_recon_state *s = state(delta);
    uint32_t *entity = 0;
    int64_t target;
    if (!frame || FIELD(frame, 4) != COMPUTE_MARKER_RETURN + delta) return;
    if (!owned(s)) return;
    if ((target = reticle_target(delta, frame, &entity, hit != 0)) < 0 || !entity) return;
    s->candidate = (uint32_t)target;
    s->candidate_valid = 1;
}

void tfs_recon_aim_begin(uint32_t delta) {
    struct tfs_recon_state *s = state(delta);
    if (!owned(s)) return;
    s->candidate = 0;
    s->candidate_valid = 0;
}

void tfs_recon_aim_end(uint32_t delta, uint32_t *rotator) {
    struct tfs_recon_state *s = state(delta);
    uint32_t *avatar, *entity, words[3], target, now;
    if (!owned(s) || !rotator || !(avatar = (uint32_t *)FIELD(rotator, 0x10)) ||
        FIELD(avatar, 0) != AVATAR_LOGIC_VTABLE + delta ||
        !(entity = (uint32_t *)FIELD(avatar, 4))) return;
    /* GetGunMarkerPosition can trace several ballistic segments. Commit
       only after it has finished, not on each intermediate miss. */
    target = s->candidate_valid ? s->candidate : 0;
    tfs_recon_control(delta, entity + ENTITY_MAILBOX / 4);
    outlines_update(delta, s, avatar);
    if (s->phase != TFS_RECON_ACTIVE) return;
    now = now_ms(delta);
    if (target == s->aim_target && s->aims && now - s->aim_tick < TFS_RECON_AIM_MS)
        return;
    words[0] = s->epoch;
    words[1] = ++s->aim_sequence;
    words[2] = target;
    if (send_report(delta, entity + ENTITY_MAILBOX / 4, 'A', words, 3)) {
        s->aim_target = target;
        s->aim_tick = now;
        ++s->aims;
    }
}

/* ---- Hook stubs --------------------------------------------------------- */
#ifndef TFS_RECON_HOST_TEST

/* Hooks may call native rendering helpers which use SSE/x87. Preserve the
 * entire floating-point state as well as GPRs/flags, including at mid-function
 * hooks. EDI holds the saved pushad frame while the 512-byte FXSAVE area is
 * aligned independently; native thiscall/cdecl preserve EDI. */
#define TFS_PUSH_STATE \
    "    pushfd\n" \
    "    pushad\n" \
    "    mov edi, esp\n" \
    "    sub esp, 0x210\n" \
    "    and esp, -16\n" \
    "    fxsave [esp]\n" \
    "    fninit\n" \
    "    fldcw [esp]\n" \
    "    push edi\n"
#define TFS_POP_STATE \
    "    lea eax, [esp+4]\n" \
    "    fxrstor [eax]\n" \
    "    mov esp, [esp]\n" \
    "    popad\n" \
    "    popfd\n"

__asm__(
    ".intel_syntax noprefix\n"
    "tfs_delta:\n"                       /* eax = load address - link address */
    "    call tfs_delta_anchor\n"
    "tfs_delta_anchor:\n"
    "    pop eax\n"
    "    sub eax, offset tfs_delta_anchor\n"
    "    ret\n"

    ".globl tfs_hook_message\n"          /* from 0x177e3d5: cmp esi,eax; je; mov ecx,[esi] */
    "tfs_hook_message:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push dword ptr [ebp-0xc4]\n"
    "    push dword ptr [ebp-0xc8]\n"
    "    push dword ptr [ebp-0xd4]\n"
    "    push dword ptr [ebp-0xd8]\n"
    "    push eax\n"
    "    call tfs_recon_message\n"
    "    add esp, 20\n"
    "    test eax, eax\n"
    "    jnz tfs_message_consumed\n"
    TFS_POP_STATE
    "    cmp esi, eax\n"
    "    je 0x177e3f6\n"
    "    mov ecx, [esi]\n"
    "    jmp 0x177e3db\n"
    "tfs_message_consumed:\n"
    TFS_POP_STATE
    "    jmp 0x177e3f6\n"

    ".globl tfs_hook_control\n"          /* from 0x1786630, thiscall(mailbox, u16*) */
    "tfs_hook_control:\n"
    "    push ecx\n"
    "    push dword ptr [esp+8]\n"
    "    call tfs_control_original\n"
    "    pop ecx\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_control\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    ret 4\n"
    "tfs_control_original:\n"            /* replayed 7-byte prologue */
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push ecx\n"
    "    lea eax, [ebp-1]\n"
    "    jmp 0x1786637\n"

    ".globl tfs_hook_markers\n"          /* from 0x1bc9370 */
    "tfs_hook_markers:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_markers\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1bc9375\n"

    ".globl tfs_hook_minimap\n"          /* from 0x1d9d410 */
    "tfs_hook_minimap:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_minimap\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1d9d415\n"

    ".globl tfs_hook_reset\n"            /* from 0x1b9ac00, thiscall(layer) */
    "tfs_hook_reset:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_reset\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1b9ac05\n"

    ".globl tfs_hook_ray\n"             /* completed segment returns AL hit */
    "tfs_hook_ray:\n"
    TFS_PUSH_STATE
    "    movzx ecx, byte ptr [edi+28]\n"
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push ebp\n"
    "    push eax\n"
    "    call tfs_recon_ray\n"
    "    add esp, 12\n"
    TFS_POP_STATE
    "    test al, al\n"
    "    jne 0x1e115f0\n"
    "    jmp 0x1e1142e\n"
    ".globl tfs_hook_lamp_bind\n"
    "tfs_hook_lamp_bind:\n"
    "    mov [esi+0x150], eax\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push esi\n"
    "    push eax\n"
    "    call tfs_recon_lamp_bind\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    jmp 0x1d867ef\n"
    ".globl tfs_hook_lamp_unbind\n"
    "tfs_hook_lamp_unbind:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_lamp_unbind\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1d7a385\n"
    ".globl tfs_hook_silhouette\n"
    "tfs_hook_silhouette:\n"
    "    push ebp\n"
    "    mov ebp, esp\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push dword ptr [ebp+8]\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_silhouette_request\n"
    "    add esp, 12\n"
    "    mov [ebp+8], eax\n"
    TFS_POP_STATE
    "    mov al, [ebp+8]\n"
    "    jmp 0x786f96\n"
    ".globl tfs_hook_armor\n"
    "tfs_hook_armor:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_armor_gate\n"
    "    add esp, 8\n"
    "    test eax, eax\n"
    "    jnz tfs_armor_denied\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1e21a35\n"
    "tfs_armor_denied:\n"
    TFS_POP_STATE
    "    ret\n"
    ".globl tfs_hook_armor_unbind\n"
    "tfs_hook_armor_unbind:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_armor_unbind\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push esi\n"
    "    mov esi, ecx\n"
    "    mov ecx, [esi+4]\n"
    "    jmp 0x1e16186\n"
    ".globl tfs_hook_armor_destroy\n"
    "tfs_hook_armor_destroy:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ecx\n"
    "    push eax\n"
    "    call tfs_recon_armor_unbind\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    push -1\n"
    "    jmp 0x1b9e755\n"
    ".globl tfs_hook_aim_begin\n"
    "tfs_hook_aim_begin:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push eax\n"
    "    call tfs_recon_aim_begin\n"
    "    add esp, 4\n"
    TFS_POP_STATE
    "    push ebp\n"
    "    mov ebp, esp\n"
    "    sub esp, 0x118\n"
    "    jmp 0x1e22f19\n"
    ".globl tfs_hook_aim_end\n"
    "tfs_hook_aim_end:\n"
    TFS_PUSH_STATE
    "    call tfs_delta\n"
    "    push ebx\n"
    "    push eax\n"
    "    call tfs_recon_aim_end\n"
    "    add esp, 8\n"
    TFS_POP_STATE
    "    cmp byte ptr [ebx+0x15], 0\n"
    "    lea edx, [ebx+0x50]\n"
    "    jmp 0x1e2304a\n"
    ".att_syntax prefix\n");
#endif
