/* Solbreaker sandbox bridge for the pinned PE32 client. Game addresses are
 * link-time virtual addresses. The only mutable global is a pointer in the
 * unused tail of the recon state page; the state itself is allocated RW/NX. */
#include <stdint.h>

#define FIELD(p, o) (*(uint32_t *)((uintptr_t)(p) + (o)))
#define STDCALL __attribute__((stdcall))
#define THISCALL __attribute__((thiscall))
#define BOOST 1u
#define FLIGHT 2u
#define ASCEND 4u
#define DESCEND 8u
#define MISSILE_VIEW 16u
#define SPAWN_TARGET 32u
#define GRAVITY_JUMP 64u
#define FAST_RELOAD 128u
#define TELEPORT 256u
#define NEXT_TARGET 512u
#define TOGGLE_TARGET_FIRE 1024u
#define CLEAR_TARGETS 2048u
#define RESTORE_HP 4096u
#define BOSS_PICK_ROLE 8192u
#define BOSS_RESPAWN 16384u
#define CHANGE_TANK 65536u
#define TIME_STOP (1u << 17)
#define BRANCH_SHOT_BASE (1u << 30)
#define GOD_MODE 32768u
#define REMOVE_TARGET (1u << 18)
#define PICK_TARGET (1u << 19)
#define USE_ABILITY (1u << 21)
#define TARGET_SELECTED (1u << 20)
#define TARGET_UNAVAILABLE (1u << 21)
#define TARGET_AI (1u << 22)
#define TARGET_AI_CHANGED (1u << 23)
#define TARGET_AI_MISSING (1u << 24)
#define TOGGLE_MAP_BORDER (1u << 25)
#define TANK_STATS (1u << 29)
#define PRACTICE_MENU (1u << 30)
#define PRACTICE_FEEDBACK (1u << 31)
#define MAP_BORDER_OPEN (1u << 26)
#define MAP_BORDER_CLOSED (1u << 27)
#define MAP_BORDER_OUTSIDE (1u << 28)
#define MAP_BORDER_NOTICES (MAP_BORDER_OPEN | MAP_BORDER_CLOSED | MAP_BORDER_OUTSIDE)
#define PRACTICE_ACTIONS (NEXT_TARGET | TOGGLE_TARGET_FIRE | CLEAR_TARGETS | REMOVE_TARGET)
#define ONE_SHOT_ACTIONS (SPAWN_TARGET | GRAVITY_JUMP | TELEPORT | PRACTICE_ACTIONS | RESTORE_HP)
/* The pinned keyboard registry uses 0 through 0x32 for stock actions. */
#define FIRST_SANDBOX_ACTION 0x33u
#define SANDBOX_ACTION_COUNT 15u
#define TELEPORT_KEY (1u << 5)
#define RESTORE_HP_KEY (1u << 9)
#define GOD_MODE_KEY (1u << 10)
#define REMOVE_TARGET_KEY (1u << 11)
#define JUMP_KEY (1u << 12)
#define SANDBOX_KEYS (JUMP_KEY - 1u)
#define ABILITY_ONE_ACTION 7u /* Native Consumables4 / ABILITY 1. */
#define ABILITY_TWO_ACTION (FIRST_SANDBOX_ACTION + 13u)
#define ABILITY_SHIFT 26u
#define ABILITY_KEYS (7u << ABILITY_SHIFT)

typedef struct { char text[16]; uint32_t size, capacity; } NativeString;
typedef struct { uint32_t device, key, modifiers, phase; } KeyBinding;
typedef struct { const uint32_t *begin, *end, *capacity; } ActionVector;
typedef int (THISCALL *NativeBind)(void *, uint32_t, NativeString, const KeyBinding *);
typedef struct { uint32_t key, modifiers; } SavedKey;
/* Keep the old fifth entry, reserved for jump, in the on-disk layout. */
typedef struct { uint32_t magic; SavedKey keys[SANDBOX_ACTION_COUNT]; } SavedBindings;
#define BINDINGS_MAGIC 0x314b4253u  /* SBK1 */

/* The launcher runs the executable from the prepared client's client/ directory. */
static const char bindings_path[] = "..\\documents\\solbreaker-keybinds.bin";
static const char blur_path[] = "..\\documents\\solbreaker-motion-blur.bin";
static const char fov_path[] __attribute__((section(".boss_rodata"))) = "..\\documents\\solbreaker-fov.bin";
static const char fov_temp[] __attribute__((section(".boss_rodata"))) = "..\\documents\\solbreaker-fov.tmp";
static const char blur_temp[] = "..\\documents\\solbreaker-motion-blur.tmp";
static const char bindings_temp[] = "..\\documents\\solbreaker-keybinds.tmp";
__attribute__((section(".boss_rodata")))
static const char keyboard_path[] = "..\\documents\\solbreaker-keyboard.bin";
__attribute__((section(".boss_rodata")))
static const char keyboard_temp[] = "..\\documents\\solbreaker-keyboard.tmp";

__attribute__((section(".boss_rodata")))
static const NativeString binding_names[] = {
    {"SolBoost", 8, 15}, {"SolFlight", 9, 15}, {"SolMissile", 10, 15},
    {"SolTarget", 9, 15}, {"SolJump", 7, 15}, {"SolReload", 9, 15},
    {"SolTeleport", 11, 15},
    {"SolNextTank", 11, 15}, {"SolTargetFire", 13, 15}, {"SolClear", 8, 15},
    {"SolHeal", 7, 15}, {"SolGod", 6, 15}, {"SolRemove", 9, 15},
    {"SolAbility2", 11, 15}, {"SolAbility3", 11, 15},
};
__attribute__((section(".boss_rodata")))
static const NativeString binding_captions[][2] = {
    {{"Local/SbBoost", 13, 15}, {"", 0, 15}},
    {{"Local/SbFlight", 14, 15}, {"", 0, 15}},
    {{"Local/SbMissile", 15, 15}, {"", 0, 15}},
    {{"Local/SbTarget", 14, 15}, {"", 0, 15}},
    {{"Local/SbJump", 12, 15}, {"", 0, 15}},
    {{"Local/SbReload", 14, 15}, {"", 0, 15}},
    {{"Local/SbTP", 10, 15}, {"", 0, 15}},
    {{"Local/SbNext", 12, 15}, {"", 0, 15}},
    {{"Local/SbFire", 12, 15}, {"", 0, 15}},
    {{"Local/SbClear", 13, 15}, {"", 0, 15}},
    {{"Local/SbHeal", 12, 15}, {"", 0, 15}},
    {{"Local/SbGod", 11, 15}, {"", 0, 15}},
    {{"Local/SbRemove", 14, 15}, {"", 0, 15}},
    {{"Local/SbAbl2", 12, 15}, {"", 0, 15}},
    {{"Local/SbAbl3", 12, 15}, {"", 0, 15}},
};
static const uint8_t visible_bindings[] = {1, 2, 6, 10, 11, 13, 14};
#define VISIBLE_SANDBOX_ACTIONS (sizeof(visible_bindings) - 2u)

typedef struct { float x, y, z; } Vector;
typedef struct { Vector origin, velocity; } AimRay;
typedef struct { float x, y; } Point;
typedef struct { float x, y, width, height; } Rect;
typedef struct { Vector position, target; } CameraPose;
typedef struct { void *control, *hidden_row; } TeamPanel;
typedef struct { uint32_t effect, entity, renderer; } ParticleIdentity;
typedef struct { void **begin, **end; } ParticleVector;
typedef struct {
    void *row, *time, *dim, *key;
    uint32_t state, ends, shown;
} AbilitySlot;
enum { PICKER_RECORDS = 1024, PICKER_ROWS = 8, PICKER_MODULES = 8192, PICKER_SLOTS = 4, MODULE_COLUMNS = 5, TANK_STAT_COUNT = 11 };
#define PICKER_MAGIC 0x33544253u  /* SBT3 */
typedef struct {
    uint16_t id;
    uint8_t tier, nation, kind, reserved;
    uint16_t modules_start, modules_count;
    char key[64], search[182];
} PickerEntry;
typedef struct { uint8_t slot, turret; char key[62]; } PickerModule;
typedef struct {
    uint32_t magic, count, modules_count;
    PickerEntry entry[PICKER_RECORDS];
    PickerModule module[PICKER_MODULES];
} PickerCatalog;
_Static_assert(sizeof(PickerEntry) == 256, "picker index record size");
_Static_assert(sizeof(PickerModule) == 64, "picker module record size");
typedef struct {
    void *menu, *stock, *panel, *command, *search;
    void *row[PICKER_ROWS], *filter[3], *marker[3], *status, *previous, *next, *hint;
    PickerCatalog *catalog;
    uint16_t matches[PICKER_RECORDS];
    unsigned selected[3], last, page, count;
    char query[64];
    uint8_t open;
    void *modules_panel, *module_vehicle;
    void *hub, *ai, *stats, *target_menu, *stat_slider[TANK_STAT_COUNT], *stat_amount[TANK_STAT_COUNT];
    void *module_card[PICKER_SLOTS][MODULE_COLUMNS];
    const PickerEntry *module_entry;
    unsigned module_selected[PICKER_SLOTS];
} TargetPicker;
typedef struct __attribute__((packed)) {
    uint8_t custom, active;
    uint32_t slots[13];
    uint8_t values[4][TANK_STAT_COUNT+1];
} SetupProfile;
typedef struct __attribute__((packed)) {
    uint8_t custom;
    uint32_t slots[14];
} RoomLineup;
_Static_assert(sizeof(RoomLineup) == 57, "room lineup envelope");

typedef struct {
    void *identity, *room_identity;
    SetupProfile profile;
    RoomLineup lineup;
    unsigned room;
    uint32_t slot, preset, tier, kind, received, team, stat_group;
    TargetPicker picker;
} SetupEditor;
_Static_assert(sizeof(SetupProfile) == 102, "prebattle setup envelope");
typedef struct {
    uint32_t thread, avatar, owner, scene, token, sequence, previous_keys;
    uint32_t toggles, last_report, missile_tick, shot, active_view, view_tick;
    uint32_t sent_flags, key_state, window_process;
    int32_t mouse_x, mouse_y;
    Vector position, velocity, view, heading;
    uint32_t pending_actions, allowed_keys;
    uint32_t score_active, ally_kills, enemy_kills;
    void *score_panel;
    uint32_t blur_percent, blur_tick, blur_camera, blur_scene;
    uint32_t settings_thread;
    uint8_t fov, saved_fov;
    Vector blur_direction;
    float blur_offset[4];
    void *bindings_layout;
    uint32_t aim_frame_tick;
    CameraPose marker_pose;
    uint32_t marker_pose_ready;
    void *notice_controller;
    void *role_picker, *role_value, *stock_boss_panel;
    uint32_t role_token, role_state, role_sent, respawn_sent, role_keys;
    void *respawn_menu, *respawn_value;
    void *swap_value;
    uint32_t swap_token, swap_sent;
    uint32_t temporal_enabled, temporal_key, temporal_end, temporal_energy, temporal_hud;
    uint32_t temporal_started, temporal_merge;
    uint32_t temporal_gain, temporal_release_gain;
    void *temporal_panel, *temporal_label;
    void *recovery_panel, *recovery_label, *recovery_rings[3];
    uint32_t recovery_seconds;
    uint32_t peer_started, peer_duration, peer_merge;
    Vector peer_origin;
    void *peer_root, *peer_clock, *peer_hand, *peer_hour, *peer_opacity;
    uint32_t temporal_scene_sampled;
    float temporal_scene_time;
    Vector temporal_transition_position;
    uint32_t temporal_transition_tick, temporal_returning, temporal_glitch_property;
    float temporal_glitch[4];
    void *battle_controls_root;
    uint32_t peer_hidden_entity;
    TeamPanel team_panels[4];
    ParticleIdentity *temporal_particles;
    uint32_t temporal_particle_count, temporal_particle_capacity;
    void *temporal_particle_system;
    void *marker_rotator;
    uint32_t marker_tick;
    AimRay marker_from, marker_ray;
    float marker_dispersion;
    void *aim_time_label;
    uint32_t aim_tenths;
    uint8_t stats[TANK_STAT_COUNT];
    TargetPicker picker;
    SetupEditor setup;
    AbilitySlot abilities[3];
    uint32_t ability_token, pending_abilities;
} Sandbox;

extern uint32_t sandbox_delta(void);
extern void sandbox_mouse_hook(void);

const NativeString *sandbox_binding_name(uint32_t action) {
    uint32_t index = action-FIRST_SANDBOX_ACTION;
    if (index < SANDBOX_ACTION_COUNT) return &binding_names[index];
    typedef const NativeString *(*Original)(uint32_t);
    return ((Original)(0x01216be0u + sandbox_delta()))(action);
}

const NativeString *sandbox_binding_caption(uint32_t action) {
    uint32_t index = action-FIRST_SANDBOX_ACTION;
    if (index < SANDBOX_ACTION_COUNT) return binding_captions[index];
    typedef const NativeString *(*Original)(uint32_t);
    return ((Original)(0x01216c00u + sandbox_delta()))(action);
}

static void *keyboard_layout(uint32_t delta) {
    typedef void *(*Options)(void);
    typedef void *(THISCALL *Layout)(void *);
    void *options = ((Options)(0x01216b90u + delta))();
    return options ? ((Layout)(0x00a75f40u + delta))(options) : 0;
}

static KeyBinding get_binding(uint32_t delta, void *layout, uint32_t action) {
    typedef KeyBinding *(THISCALL *Lookup)(void *, KeyBinding *, uint32_t);
    KeyBinding binding = {0, 0, 0, 0};
    ((Lookup)(0x01217710u + delta))(layout, &binding, action);
    return binding;
}

static void *open_binding_file(uint32_t delta, const char *path, uint32_t access, uint32_t mode) {
    typedef void *(STDCALL *Open)(const char *, uint32_t, uint32_t, void *, uint32_t, uint32_t, void *);
    return (*(Open *)(0x036ef140u + delta))(path, access, 1, 0, mode, 0x80, 0);
}

static int read_setting(uint32_t delta, const char *path, void *data, uint32_t size) {
    typedef int (STDCALL *Read)(void *, void *, uint32_t, uint32_t *, void *);
    typedef int (STDCALL *Close)(void *);
    void *file = open_binding_file(delta, path, 0x80000000u, 3);
    if (file == (void *)UINT32_MAX) return 0;
    uint32_t count = 0;
    int ok = (*(Read *)(0x036ef194u + delta))(file, data, size, &count, 0);
    (*(Close *)(0x036ef320u + delta))(file);
    return ok && count == size;
}

static int read_bindings(uint32_t delta, SavedBindings *data) {
    static const uint8_t saved_counts[] = {SANDBOX_ACTION_COUNT, 13, 12, 11, 10, 7, 6, 5};
    for (unsigned index = 5; index < SANDBOX_ACTION_COUNT; index++)
        data->keys[index] = (SavedKey){0};
    for (unsigned index = 0; index < sizeof(saved_counts); index++) {
        if (read_setting(delta, bindings_path, data,
                         sizeof(data->magic) + saved_counts[index]*sizeof(SavedKey)))
            return data->magic == BINDINGS_MAGIC;
    }
    return 0;
}

static int write_setting(uint32_t delta, const char *path, const char *temp,
                         const void *data, uint32_t size) {
    typedef int (STDCALL *Write)(void *, const void *, uint32_t, uint32_t *, void *);
    typedef int (STDCALL *Flush)(void *);
    typedef int (STDCALL *Close)(void *);
    typedef int (STDCALL *Delete)(const char *);
    typedef void *(STDCALL *Module)(const char *);
    typedef void *(STDCALL *Procedure)(void *, const char *);
    typedef int (STDCALL *Move)(const char *, const char *, uint32_t);
    void *kernel = (*(Module *)(0x036ef08cu + delta))("kernel32.dll");
    if (!kernel) return 0;
    Move move = (Move)(*(Procedure *)(0x036ef1c0u + delta))(kernel, "MoveFileExA");
    if (!move) return 0;
    void *file = open_binding_file(delta, temp, 0x40000000u, 2);
    if (file == (void *)UINT32_MAX) return 0;
    uint32_t count = 0;
    int ok = (*(Write *)(0x036ef0d4u + delta))(file, data, size, &count, 0)
             && count == size && (*(Flush *)(0x036ef080u + delta))(file);
    (*(Close *)(0x036ef320u + delta))(file);
    if (ok && move(temp, path, 9)) return 1;
    (*(Delete *)(0x036ef068u + delta))(temp);
    return 0;
}

static int save_bindings(uint32_t delta, void *layout) {
    SavedBindings data = {.magic = BINDINGS_MAGIC};
    for (unsigned index = 0; index < sizeof(visible_bindings); index++) {
        uint32_t binding = visible_bindings[index];
        KeyBinding key = get_binding(delta, layout, FIRST_SANDBOX_ACTION + binding);
        if (key.device == 0x400) data.keys[binding] = (SavedKey){key.key, key.modifiers};
    }
    return write_setting(delta, bindings_path, bindings_temp, &data, sizeof(data));
}

/* Keep the keyboard/mouse layout in the local profile across account sessions. */
__attribute__((section(".late_sandbox"), noinline))
static void restore_keyboard(uint32_t delta, void *layout) {
    KeyBinding saved[FIRST_SANDBOX_ACTION];
    if (!read_setting(delta, keyboard_path, saved, sizeof(saved))) return;
    typedef void (THISCALL *CopyString)(NativeString *, const NativeString *);
    CopyString copy_string = (CopyString)(0x006a5120u + delta);
    NativeBind bind = (NativeBind)(0x0123c830u + delta);
    for (uint32_t action = 0; action < FIRST_SANDBOX_ACTION; action++) {
        const KeyBinding *key = &saved[action];
        if ((key->device == 0x200 && key->key < 6)
                || (key->device == 0x400 && key->key < 0x7f)
                || (!key->device && !key->key && !key->modifiers && key->phase == 1)) {
            /* NativeBind destroys its by-value string, so give it an owned copy. */
            NativeString name;
            copy_string(&name, sandbox_binding_name(action));
            bind(layout, action, name, key);
        }
    }
}

__attribute__((section(".late_sandbox"), noinline))
void **sandbox_load_keyboard(void **output, void *source, const NativeString *name) {
    typedef void **(*Create)(void **, void *, const NativeString *);
    uint32_t delta = sandbox_delta();
    void **result = ((Create)(0x011de9f0u + delta))(output, source, name);
    if (*output) restore_keyboard(delta, *output);
    return result;
}

__attribute__((section(".late_sandbox"), noinline))
static void save_keyboard(uint32_t delta, void *layout) {
    if (layout != keyboard_layout(delta)) return;
    KeyBinding bindings[FIRST_SANDBOX_ACTION];
    for (uint32_t action = 0; action < FIRST_SANDBOX_ACTION; action++)
        bindings[action] = get_binding(delta, layout, action);
    write_setting(delta, keyboard_path, keyboard_temp, bindings, sizeof(bindings));
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_save_keyboard(void *layout) {
    typedef void (THISCALL *Save)(void *);
    uint32_t delta = sandbox_delta();
    ((Save)(0x0120ebe0u + delta))(layout);
    save_keyboard(delta, layout);
}

static void seed_bindings(uint32_t delta, void *layout, int restore) {
    NativeBind bind = (NativeBind)(0x0123c830u + delta);
    SavedBindings saved;
    int have_saved = restore && read_bindings(delta, &saved);
    const KeyBinding unbound = {0,0,0,1};
    static const uint8_t retired_bindings[] = {0, 3, 5, 7, 8, 9, 12};
    for (unsigned index = 0; index < sizeof(retired_bindings); ++index) {
        uint32_t binding = retired_bindings[index];
        uint32_t action = FIRST_SANDBOX_ACTION + binding;
        if (get_binding(delta, layout, action).device)
            bind(layout, action, binding_names[binding], &unbound);
    }

    for (uint32_t index = 0; index < sizeof(visible_bindings); index++) {
        uint32_t binding = visible_bindings[index];
        uint32_t action = FIRST_SANDBOX_ACTION + binding;
        if (!get_binding(delta, layout, action).device) {
            /* Native keyboard device, Numpad 1 through 9 then 0. */
            KeyBinding key = {0x400, binding == 10 ? 0x49 : 0x4a + binding - (binding > 4), 0, 9};
            if (binding == 11) key = (KeyBinding){0x400, 0x53, 0, 9};
            if (binding >= 13) key = (KeyBinding){0x400, 34 + binding - 13, 0, 9};
            if (have_saved && saved.keys[binding].key && saved.keys[binding].key < 0x7f
                    && saved.keys[binding].modifiers < 16) {
                key.key = saved.keys[binding].key;
                key.modifiers = saved.keys[binding].modifiers;
            }
            bind(layout, action, binding_names[binding], &key);
        }
    }
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_migrate_controls(void *options) {
    typedef void (THISCALL *Reset)(void *);
    uint32_t delta = sandbox_delta();
    KeyBinding saved[FIRST_SANDBOX_ACTION];
    /* The stock post-login migration resets the local controls at version 0.
       On an existing private profile the layout already belongs to the user. */
    if (!read_setting(delta, keyboard_path, saved, sizeof(saved)))
        ((Reset)(0x01232a20u + delta))(options);
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_controls_version(void *controls, uint32_t version) {
    typedef void (THISCALL *SetVersion)(void *, uint32_t);
    uint32_t delta = sandbox_delta();
    /* Disconnect supplies zero. It is a session sentinel, not a new schema. */
    if (!version && FIELD(controls, 0x18) == 1) return;
    ((SetVersion)(0x0123dd70u + delta))(controls, version);
    void *layout = version ? keyboard_layout(delta) : 0;
    if (layout) {
        restore_keyboard(delta, layout);
        seed_bindings(delta, layout, 1);
    }
}

void THISCALL sandbox_reset_bindings(void *layout) {
    typedef void (THISCALL *Reset)(void *);
    uint32_t delta = sandbox_delta();
    ((Reset)(0x01232ab0u + delta))(layout);
    if (layout == keyboard_layout(delta)) {
        seed_bindings(delta, layout, 0);
        save_bindings(delta, layout);
        save_keyboard(delta, layout);
    }
}

int THISCALL sandbox_set_binding(void *layout, uint32_t action, NativeString name, const KeyBinding *key) {
    uint32_t delta = sandbox_delta();
    NativeBind bind = (NativeBind)(0x0123c830u + delta);
    uint32_t index = action - FIRST_SANDBOX_ACTION;
    if (index >= SANDBOX_ACTION_COUNT || index == 4) {
        int result = bind(layout, action, name, key);
        if (!result && action < FIRST_SANDBOX_ACTION) save_keyboard(delta, layout);
        return result;
    }
    if (key->device != 0x400 || !key->key || key->key >= 0x7f || key->modifiers >= 16)
        return 2; /* Keep the native keyboard capture open for a key event. */
    KeyBinding previous = get_binding(delta, layout, action);
    int result = bind(layout, action, name, key);
    if (result || save_bindings(delta, layout)) return result;
    bind(layout, action, name, &previous);
    return 2;
}

__attribute__((section(".late_sandbox"), noinline))
static uint32_t binding_keys(uint32_t delta, Sandbox *s) {
    typedef void *(*Context)(void);
    typedef void *(THISCALL *GetKeyboard)(void *);
    typedef uint16_t *(THISCALL *DigitalState)(void *, uint16_t *, uint32_t);
    void *layout = keyboard_layout(delta);
    if (!layout) return 0;
    if (layout != s->bindings_layout) {
        seed_bindings(delta, layout, 1);
        s->bindings_layout = layout;
    }
    void *context = ((Context)(0x00ab01f0u + delta))();
    if (!context) return 0;
    void *manager = (void *)FIELD(context, 0x70);
    if (!manager) return 0;
    void *keyboard = ((GetKeyboard)FIELD(FIELD(manager, 0), 0x20))(manager);
    if (!keyboard) return 0;
    DigitalState down = (DigitalState)(0x00921d50u + delta);
    static const uint8_t modifier_keys[] = {8, 7, 0x6b, 0x6a};
    uint32_t modifiers = 0, pressed = 0;
    uint16_t value = 0;
    for (unsigned index = 0; index < sizeof(modifier_keys); index++)
        if ((uint8_t)*down(keyboard, &value, modifier_keys[index])) modifiers |= 1u << index;
    for (unsigned index = 0; index <= VISIBLE_SANDBOX_ACTIONS; index++) {
        uint32_t binding = index == VISIBLE_SANDBOX_ACTIONS ? 0 : visible_bindings[index];
        uint32_t bit = index == VISIBLE_SANDBOX_ACTIONS ? JUMP_KEY : 1u << (binding-(binding>4));
        if (!(s->allowed_keys & bit)) continue;
        uint32_t action = index == VISIBLE_SANDBOX_ACTIONS ? ABILITY_ONE_ACTION
                          : FIRST_SANDBOX_ACTION + binding;
        KeyBinding key = get_binding(delta, layout, action);
        if (key.device == 0x400 && key.modifiers == modifiers && key.key
                && (uint8_t)*down(keyboard, &value, key.key)) pressed |= bit;
    }
    if (s->ability_token == s->token) {
        for (unsigned slot = 0; slot < 3; slot++) {
            if (!s->abilities[slot].state) continue;
            unsigned action = slot ? ABILITY_TWO_ACTION + slot - 1 : ABILITY_ONE_ACTION;
            KeyBinding key = get_binding(delta, layout, action);
            if (key.device == 0x400 && key.modifiers == modifiers && key.key
                    && (uint8_t)*down(keyboard, &value, key.key))
                pressed |= 1u << (ABILITY_SHIFT + slot);
        }
    }
    return pressed;
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_binding_menu(void *controller, void *page, const ActionVector *actions,
                                  uint32_t device, uint32_t columns) {
    typedef void (THISCALL *Original)(void *, void *, const ActionVector *, uint32_t, uint32_t);
    uint32_t delta = sandbox_delta();
    Original original = (Original)(0x01c0c700u + delta);
    uint32_t ids[64];
    unsigned count = actions->end - actions->begin;
    if (device != 0x400 || columns != 3 || count > 64 - sizeof(visible_bindings)) {
        original(controller, page, actions, device, columns);
        return;
    }
    void *layout = keyboard_layout(delta);
    if (layout) seed_bindings(delta, layout, 1);
    for (unsigned index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++)
        ids[index] = FIRST_SANDBOX_ACTION + visible_bindings[index];
    unsigned length = VISIBLE_SANDBOX_ACTIONS;
    for (unsigned index = 0; index < count; index++) {
        unsigned action = actions->begin[index];
        ids[length++] = action;
        if (action == ABILITY_ONE_ACTION) {
            ids[length++] = ABILITY_TWO_ACTION;
            ids[length++] = ABILITY_TWO_ACTION + 1;
        }
    }
    ActionVector combined = {ids, ids+length, ids+64};
    original(controller, page, &combined, device, columns);
}

static uint32_t thread_id(void) {
    uint32_t thread;
    __asm__ volatile("movl %%fs:0x24, %0" : "=r"(thread));
    return thread;
}

static Sandbox *state(uint32_t delta, int allocate) {
    Sandbox **slot = (Sandbox **)(0x0471fff0u + delta);
    Sandbox *value = *slot;
    if (!value && allocate) {
        typedef void *(STDCALL *Allocate)(void *, uint32_t, uint32_t, uint32_t);
        value = (*(Allocate *)(0x036ef224u + delta))(0, sizeof(*value), 0x3000, 4);
        if (value) {
            typedef void *(STDCALL *Module)(const char *);
            typedef void *(STDCALL *Procedure)(void *, const char *);
            void *user32 = (*(Module *)(0x036ef08cu + delta))("user32.dll");
            Procedure address = *(Procedure *)(0x036ef1c0u + delta);
            value->key_state = (uint32_t)address(user32, "GetAsyncKeyState");
            value->window_process = (uint32_t)address(user32, "GetWindowThreadProcessId");
            uint8_t percent;
            if (read_setting(delta, blur_path, &percent, sizeof(percent)) && percent <= 100)
                value->blur_percent = percent;
            value->fov = value->saved_fov =
                read_setting(delta, fov_path, &percent, sizeof(percent)) && percent >= 40 && percent <= 100
                ? percent : 54;
            *slot = value;
        }
    }
    return value;
}

static uint32_t clock_ms(uint32_t delta) {
    typedef uint32_t (STDCALL *Tick)(void);
    return (*(Tick *)(0x036ef0fcu + delta))();
}

static int foreground(uint32_t delta, Sandbox *s) {
    typedef void *(STDCALL *Window)(void);
    typedef uint32_t (STDCALL *WindowProcess)(void *, uint32_t *);
    typedef uint32_t (STDCALL *Process)(void);
    void *window = (*(Window *)(0x036ef6f4u + delta))();
    uint32_t pid = 0;
    return window && s->window_process && ((WindowProcess)s->window_process)(window, &pid)
           && pid == (*(Process *)(0x036ef330u + delta))();
}

static int battle_input_active(uint32_t delta) {
    typedef void *(*CurrentWindow)(void);
    void *window = ((CurrentWindow)(0x00ab0560u + delta))();
    return window && FIELD(window, 0xb0) == 2 && *(uint8_t *)((uintptr_t)window + 0xa7);
}

static void clear_view(Sandbox *s) {
    __atomic_store_n(&s->active_view, 0, __ATOMIC_RELAXED);
    s->view_tick = 0;
    s->marker_pose_ready = 0;
    __atomic_exchange_n(&s->mouse_x, 0, __ATOMIC_RELAXED);
    __atomic_exchange_n(&s->mouse_y, 0, __ATOMIC_RELAXED);
}

static void boss_picker_update(uint32_t delta, Sandbox *s);
static void boss_picker_message(uint32_t delta, const uint32_t *message);
static void boss_respawn_menu(void *menu, uint32_t delta);
static void range_ability_message(uint32_t delta, const uint32_t *words);
static void range_ability_hud(uint32_t delta, Sandbox *s, uint32_t now);
static void send_range_abilities(uint32_t delta, Sandbox *s, uint32_t changes, uint32_t now);
static void *find_control(void *root, const char *name, uint32_t delta);
static void bind_battle_controls(uint32_t delta, Sandbox *s, void *root);
static void boss_visible(void *control, uint32_t visible, uint32_t delta);
static void update_blur_label(void *label, uint32_t percent, uint32_t delta);
static void bind_stats(uint32_t delta, Sandbox *s, void *menu);
static int setup_slider(uint32_t delta, void *slider, unsigned phase);
static void setup_save_description(uint32_t delta, void *view, NativeString *description);
static const char *setup_read_room_tag(const char *text, RoomLineup *lineup);
static void setup_load_room(uint32_t delta, void *view, RoomLineup lineup, int bots1, int bots2);
static unsigned setup_room_tag(uint32_t delta, void *view, char *output);
static void temporal_audio_gain(uint32_t delta, Sandbox *s, uint32_t percent);
static void temporal_roster(uint32_t delta, Sandbox *s, uint32_t entity);
static void practice_roster(uint32_t delta, Sandbox *s, const uint32_t *ids, const uint32_t *end);

static void reset(uint32_t delta, Sandbox *s) {
    temporal_audio_gain(delta, s, 100u);
    s->temporal_release_gain = 0;
    clear_view(s);
    s->owner = s->avatar = s->scene = s->token = s->sequence = 0;
    s->previous_keys = s->toggles = s->last_report = s->sent_flags = s->shot = 0;
    s->pending_actions = s->allowed_keys = 0;
    s->ability_token = s->pending_abilities = 0;
    for (unsigned slot = 0; slot < 3; slot++) s->abilities[slot].state = 0;
    __builtin_memset(s->stats, 0, sizeof(s->stats));
    s->picker.menu = 0;
    s->bindings_layout = 0;
    s->respawn_menu = s->respawn_value = 0;
    s->swap_value = 0;
    s->role_sent = s->respawn_sent = 0;
    s->aim_frame_tick = 0;
    s->marker_rotator = 0;
    s->blur_tick = s->blur_camera = s->blur_scene = 0;
    s->blur_offset[0] = s->blur_offset[1] = 0;
    s->temporal_enabled = s->temporal_key = s->temporal_end = s->temporal_hud = 0;
    s->temporal_started = s->temporal_merge = 0;
    s->temporal_particle_system = 0;
    s->temporal_particle_count = 0;
    s->peer_started = s->peer_merge = 0;
    s->temporal_transition_tick = 0;
    s->temporal_glitch[2] = 0;
}

static float limit(float value, float bound) {
    if (value > bound) return bound;
    if (value < -bound) return -bound;
    return value;
}

static float root(float value) {
    __asm__("sqrtss %0, %0" : "+x"(value));
    return value;
}

static Vector normalized(Vector value) {
    float length = root(value.x*value.x + value.y*value.y + value.z*value.z);
    if (!(length > .001f)) return (Vector){0, 0, 1};
    return (Vector){value.x/length, value.y/length, value.z/length};
}

__attribute__((section(".boss_ui"), noinline))
static int send_control_value(uint32_t delta, Sandbox *s, uint32_t flags, Vector direction, uint32_t value) {
    typedef uint32_t *(THISCALL *Begin)(uint32_t *, uint32_t, uint32_t, uint8_t *);
    typedef uint8_t *(THISCALL *Reserve)(uint32_t *, uint32_t);
    uint8_t status = 0xff;
    uint32_t *mailbox = (uint32_t *)(s->owner + 0x7c);
    uint32_t channel = FIELD(s->owner, 0x30);
    if (!channel || FIELD(channel, 0) != 0x039de150u + delta
            || FIELD(FIELD(channel, 0), 0x40) != 0x028097b0u + delta)
        return 0;
    uint32_t *stream = ((Begin)(0x0280dc00u + delta))(mailbox, 31, 1, &status);
    if (status || !stream || FIELD(stream, 0) != 0x039e2fb8u + delta
            || FIELD(FIELD(stream, 0), 4) != 0x0282e650u + delta)
        return 0;
    uint8_t *body = ((Reserve)(0x0282e650u + delta))(stream, 38);
    if (!body) return 0;
    FIELD(body, 0) = 0x2d534654u;  /* TFS- */
    FIELD(body, 4) = 0x2d42414cu;  /* LAB- */
    body[8] = 'S';
    FIELD(body, 9) = flags == CHANGE_TANK ? s->swap_token :
        flags & (BOSS_PICK_ROLE | BOSS_RESPAWN) ? s->role_token : s->token;
    FIELD(body, 13) = ++s->sequence;
    FIELD(body, 17) = flags;
    FIELD(body, 21) = value;
    *(Vector *)(body + 25) = direction;
    body[37] = 1;
    return 1;
}

static int send_control(uint32_t delta, Sandbox *s, uint32_t flags, Vector direction) {
    return send_control_value(delta, s, flags, direction, flags & MISSILE_VIEW ? s->shot : 0);
}

/* Native battle HUD refresh, with an override scoped to the current arena. */
static void apply_kill_score(uint32_t delta, Sandbox *s) {
    typedef void (*Format)(NativeString *, uint32_t);
    typedef void (THISCALL *SetText)(void *, const NativeString *);
    typedef void (THISCALL *Destroy)(NativeString *);
    if (!s->score_active || !s->score_panel) return;
    const uint32_t scores[] = {s->ally_kills, s->enemy_kills};
    for (unsigned i = 0; i < 2; ++i) {
        void *text = (void *)FIELD(s->score_panel, 0x154 + 4*i);
        if (!text) continue;
        NativeString label;
        ((Format)(0x815b80u + delta))(&label, scores[i]);
        ((SetText)(0xd01d70u + delta))(text, &label);
        ((Destroy)(0x69a720u + delta))(&label);
    }
}

void THISCALL sandbox_refresh_score(void *panel) {
    typedef void (THISCALL *Refresh)(void *);
    uint32_t delta = sandbox_delta();
    ((Refresh)(0x1dd0190u + delta))(panel);
    Sandbox *s = state(delta, 1);
    if (s && s->thread == thread_id()) {
        if (FIELD(panel, 0x150))
            s->score_panel = panel;
        else if (s->score_panel == panel)
            s->score_panel = 0;
        apply_kill_score(delta, s);
    }
}

/* TextNotificationController's battle attach/detach interface lives at +8. */
void THISCALL sandbox_notice_attach(void *listener) {
    typedef void (THISCALL *Attach)(void *);
    uint32_t delta = sandbox_delta();
    ((Attach)(0x1deb640u + delta))(listener);
    Sandbox *s = state(delta, 1);
    if (s) s->notice_controller = (char *)listener - 8;
}

void THISCALL sandbox_notice_detach(void *listener) {
    typedef void (THISCALL *Detach)(void *);
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->notice_controller == (char *)listener - 8)
        s->notice_controller = 0;
    ((Detach)(0x1debe30u + delta))(listener);
}

__attribute__((section(".boss_ui"), noinline))
static void sandbox_notice(uint32_t delta, Sandbox *s, uint32_t index) {
    static const NativeString keys[] __attribute__((section(".boss_rodata"))) = {
        {"Local/SbBoost0", 14, 15}, {"Local/SbBoost1", 14, 15},
        {"Local/SbFly0", 12, 15}, {"Local/SbFly1", 12, 15},
        {"Local/SbCam0", 12, 15}, {"Local/SbCam1", 12, 15},
        {"Local/SbLoad0", 13, 15}, {"Local/SbLoad1", 13, 15},
        {"Local/SbSpawn", 13, 15},
        {"Local/SbMoved", 13, 15},
        {"Local/SbOwn", 11, 15}, {"Local/SbT59", 11, 15},
        {"Local/SbKV1", 11, 15}, {"Local/SbPzIV", 12, 15},
        {"Local/SbFire0", 13, 15}, {"Local/SbFire1", 13, 15},
        {"Local/SbClean", 13, 15}, {"Local/SbFull", 12, 15},
        {"Local/SbHealed", 14, 15}, {"Local/SbGod0", 12, 15}, {"Local/SbGod1", 12, 15},
        {"Local/SbTsGear", 14, 15},
        {"Local/SbPicked", 14, 15}, {"Local/SbNoTank", 14, 15},
        {"Local/SbAiSet", 13, 15}, {"Local/SbAiMiss", 14, 15},
        {"Local/SbBorder0", 15, 15}, {"Local/SbBorder1", 15, 15}, {"Local/SbBorder2", 15, 15},
        {"Local/SbTmMiss", 14, 15}, {"Local/SbTmHeal", 14, 15}, {"Local/SbTmKill", 14, 15},
        {"Local/SbTmGone", 14, 15}, {"Local/SbTmFull", 14, 15},
    };
    void *controller = s->notice_controller;
    void *localization = (void *)FIELD(0x471eea0u + delta, 0);
    if (!controller || !localization || FIELD(controller, 0) != 0x3851e08u + delta ||
            !FIELD(controller, 0x24)) return;
    typedef void (THISCALL *Localize)(void *, NativeString *, const NativeString *);
    typedef void (THISCALL *Show)(void *, const NativeString *, uint32_t);
    typedef void (THISCALL *Destroy)(NativeString *);
    NativeString text;
    ((Localize)(0x9bd130u + delta))(localization, &text, &keys[index]);
    if (text.size) {
        /* The adjacent 0x115 is the live +HP notice. */
        uint32_t notice = 0x100 + index + (index >= 21);
        ((Show)(0x1de19b0u + delta))(controller, &text, notice);
    }
    ((Destroy)(0x69a720u + delta))(&text);
}

__attribute__((section(".late_sandbox"), noinline))
static void healed_amount(uint32_t delta, Sandbox *s, uint32_t amount) {
    if (!s || s->thread != thread_id() || !amount || amount > 32767) return;
    void *controller = s->notice_controller;
    if (!controller || FIELD(controller, 0) != 0x3851e08u + delta || !FIELD(controller, 0x24)) return;
    NativeString text = {{'+'}, 1, 15};
    char reversed[5];
    unsigned digits = 0;
    while (amount) {
        reversed[digits++] = '0' + amount % 10;
        amount /= 10;
    }
    while (digits) text.text[text.size++] = reversed[--digits];
    text.text[text.size++] = ' ';
    text.text[text.size++] = 'H';
    text.text[text.size++] = 'P';
    typedef void (THISCALL *Show)(void *, const NativeString *, uint32_t);
    ((Show)(0x1de19b0u + delta))(controller, &text, 0x115);
}

static void crew_voice(uint32_t delta, Sandbox *s, uint32_t cue) {
    if (!s || s->thread != thread_id() || !s->avatar) return;
    void *context = (void *)FIELD(s->avatar, 0x1f8);
    if (!context) return;
    typedef void *(THISCALL *SoundController)(void *);
    typedef void (THISCALL *Play)(void *, uint32_t, uint32_t);
    void *sound = ((SoundController)FIELD(FIELD(context, 0), 0x54))(context);
    if (sound) ((Play)(0x176ab50u + delta))(sound, cue, 0);
}

__attribute__((section(".late_sandbox"), noinline))
static void temporal_sound(uint32_t delta, uint32_t status) {
    typedef void *(STDCALL *Module)(const char *);
    typedef void *(STDCALL *Procedure)(void *, const char *);
    typedef int (STDCALL *Play)(const char *, void *, uint32_t);
    Procedure resolve = *(Procedure *)(0x036ef1c0u + delta);
    Module module = *(Module *)(0x036ef08cu + delta);
    void *audio = module("winmm.dll");
    if (!audio) {
        void *kernel = module("kernel32.dll");
        Module load = kernel ? (Module)resolve(kernel, "LoadLibraryA") : 0;
        audio = load ? load("winmm.dll") : 0;
    }
    Play play = audio ? (Play)resolve(audio, "PlaySoundA") : 0;
    if (play) play(status < 3 ? "Data\\SolbreakerTimeStop\\enter.wav"
                              : "Data\\SolbreakerTimeStop\\merge.wav", 0, 0x20003u);
}

/* The primary Wwise output gain multiplies the in-game volume settings. */
__attribute__((section(".late_sandbox"), noinline))
static void temporal_audio_gain(uint32_t delta, Sandbox *s, uint32_t percent) {
    uint32_t stored = percent == 100u ? 0 : percent;
    if (s->temporal_gain == stored) return;
    typedef uint8_t (*Initialized)(void);
    typedef uint32_t (*SetOutputVolume)(uint64_t, float);
    if (!((Initialized)(0x2622a50u + delta))()) {
        s->temporal_gain = 0;
        return;
    }
    if (((SetOutputVolume)(0x26276d0u + delta))(0, (float)percent * .01f) == 1u)
        s->temporal_gain = stored;
}

__attribute__((section(".late_sandbox"), noinline))
static void temporal_audio_update(uint32_t delta, Sandbox *s, uint32_t now) {
    if (s->temporal_end) {
        uint32_t elapsed = now - s->temporal_started;
        temporal_audio_gain(delta, s, elapsed < 260u ? 100u - 88u * elapsed / 260u : 12u);
    } else {
        uint32_t elapsed = now - s->temporal_merge;
        uint32_t start = s->temporal_release_gain;
        temporal_audio_gain(delta, s, start && elapsed < 420u
            ? start + (100u - start) * elapsed / 420u : 100u);
    }
}

/* Wine's Explorer can fail to show the selected replay. Send a request only
 * for the prepared client's replay directory, using paths set by the launcher. */
__attribute__((section(".late_sandbox")))
void THISCALL sandbox_open_replay_folder(void *platform, const void *folder, const void *items) {
    typedef void (THISCALL *Open)(void *, const void *, const void *);
    typedef uint32_t (STDCALL *Environment)(const uint16_t *, uint16_t *, uint32_t);
    typedef void *(STDCALL *Module)(const char *);
    typedef void *(STDCALL *Procedure)(void *, const char *);
    typedef int (STDCALL *PidPath)(const void *, uint16_t *);
    typedef void (STDCALL *Free)(void *);
    typedef void *(STDCALL *Create)(const uint16_t *, uint32_t, uint32_t, void *,
                                    uint32_t, uint32_t, void *);
    typedef int (STDCALL *Close)(void *);
    static const uint16_t request_key[] = {
        'S','O','L','B','R','E','A','K','E','R','_','R','E','P','L','A','Y','_','R','E','Q','U','E','S','T',0
    };
    static const uint16_t folder_key[] = {
        'S','O','L','B','R','E','A','K','E','R','_','R','E','P','L','A','Y','_','F','O','L','D','E','R',0
    };
    uint32_t delta = sandbox_delta();
    Environment environment = *(Environment *)(0x036ef0f0u + delta);
    uint16_t request[260], expected[260], path[260];
    uint32_t size = environment(request_key, request, 260);
    if (size && size < 260) {
        size = environment(folder_key, expected, 260);
        Module module = *(Module *)(0x036ef08cu + delta);
        Procedure resolve = *(Procedure *)(0x036ef1c0u + delta);
        void *shell = module("shell32.dll");
        PidPath get_path = shell ? (PidPath)resolve(shell, "SHGetPathFromIDListW") : 0;
        if (size && size < 260 && get_path) {
            void *pid = ((void *(*)(const void *))(0x2020460u + delta))(folder);
            int valid = pid && get_path(pid, path);
            if (pid) (*(Free *)(0x036eff50u + delta))(pid);
            uint32_t index = 0;
            if (valid) {
                while (index < size && path[index] == expected[index]) ++index;
            }
            if (valid && index == size && (!path[index] ||
                    (index+1 < 260 && path[index] == '\\' && !path[index+1]))) {
                Create create = *(Create *)(0x036ef27cu + delta);
                void *file = create(request, 0x40000000u, 7, 0, 2, 0x80u, 0);
                if (file != (void *)-1) {
                    (*(Close *)(0x036ef320u + delta))(file);
                    return;
                }
            }
        }
    }
    ((Open)(0x2020840u + delta))(platform, folder, items);
}

/* TracerComponent +0x20 stores the server shot ID. Keep the retained
 * trajectory for world shots and advance newly fired private shots. */
__attribute__((section(".late_sandbox")))
void THISCALL sandbox_tracer_update(void *system, void *tracer, void *node, float dt) {
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->temporal_end && FIELD(tracer, 0x20) < BRANCH_SHOT_BASE)
        return;
    typedef void (THISCALL *Update)(void *, void *, void *, float);
    ((Update)(0x132ad50u + delta))(system, tracer, node, dt);
}

__attribute__((section(".late_sandbox")))
void THISCALL sandbox_tracer_position(void *tracer, const Vector *position, const Vector *velocity) {
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && FIELD(tracer, 0x20) < BRANCH_SHOT_BASE) {
        if (s->temporal_end) return;
        void *path = (void *)FIELD(tracer, 0x84);
        if (s->temporal_merge && clock_ms(delta)-s->temporal_merge < 420u &&
                path && FIELD(path, 0) == 0x3855ae8u + delta) {
            /* Rebase the ballistic path and discard queued interpolation
             * targets from before the fork. Retain its storage and lifetime. */
            FIELD(path, 0x14) = 0;
            *(Vector *)((uintptr_t)path + 0x18) = *position;
            *(Vector *)((uintptr_t)path + 0x28) = *velocity;
            *(Vector *)((uintptr_t)path + 0x4c) = *position;
            return;
        }
    }
    typedef void (THISCALL *Update)(void *, const Vector *, const Vector *);
    ((Update)(0x1314f10u + delta))(tracer, position, velocity);
}

__attribute__((section(".late_sandbox"), noinline))
static void temporal_scene_process(void *system, float dt, uint32_t address) {
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    /* SceneSystem's scene pointer is at +0x28. Leave other scenes running. */
    if (s && s->temporal_end && s->scene == FIELD(system, 0x28)) return;
    typedef void (THISCALL *Process)(void *, float);
    ((Process)(address + delta))(system, dt);
}

#define SCENE_PROCESS(name, address) \
    __attribute__((section(".late_sandbox"))) \
    void THISCALL name(void *system, float dt) { temporal_scene_process(system, dt, address); }
SCENE_PROCESS(sandbox_scene_animation, 0xc2dc40u)
SCENE_PROCESS(sandbox_scene_wind, 0xbb1d70u)
SCENE_PROCESS(sandbox_scene_wave, 0xbb1bc0u)
SCENE_PROCESS(sandbox_scene_trees, 0xaf6220u)
#undef SCENE_PROCESS

/* Rendering keeps its registered objects. The expiry loop and both job
 * queues skip only emitters that were active at the owner scene's fork. */
__attribute__((section(".late_sandbox")))
void THISCALL sandbox_scene_particles(void *system, float dt) {
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->temporal_end && s->scene == FIELD(system, 0x28) &&
            s->temporal_particle_system != system) {
        const ParticleVector *active = (const ParticleVector *)((uintptr_t)system + 0x50);
        uint32_t count = ((uintptr_t)active->end - (uintptr_t)active->begin) / sizeof(void *);
        if (count > s->temporal_particle_capacity && count <= UINT32_MAX / sizeof(ParticleIdentity)) {
            typedef void *(STDCALL *Allocate)(void *, uint32_t, uint32_t, uint32_t);
            typedef int (STDCALL *Free)(void *, uint32_t, uint32_t);
            ParticleIdentity *storage = (*(Allocate *)(0x036ef224u + delta))(
                0, count * sizeof(*storage), 0x3000, 4);
            if (storage) {
                if (s->temporal_particles)
                    (*(Free *)(0x036ef220u + delta))(s->temporal_particles, 0, 0x8000);
                s->temporal_particles = storage;
                s->temporal_particle_capacity = count;
            }
        }
        s->temporal_particle_system = system;
        s->temporal_particle_count = 0;
        if (count <= s->temporal_particle_capacity) {
            for (uint32_t i = 0; i < count; ++i) {
                void *effect = active->begin[i];
                ParticleIdentity entry = {(uintptr_t)effect, FIELD(effect, 8), FIELD(effect, 0x90)};
                uint32_t j = i;
                while (j && s->temporal_particles[j-1].effect > entry.effect) {
                    s->temporal_particles[j] = s->temporal_particles[j-1];
                    --j;
                }
                s->temporal_particles[j] = entry;
            }
            s->temporal_particle_count = count;
        }
    }
    typedef void (THISCALL *Process)(void *, float);
    ((Process)(0xaf52b0u + delta))(system, dt);
}

__attribute__((section(".late_sandbox"), noinline))
static int temporal_particle_in_snapshot(const Sandbox *s, const void *effect) {
    uint32_t first = 0, end = s->temporal_particle_count;
    while (first < end) {
        uint32_t middle = first + (end-first)/2;
        const ParticleIdentity *entry = &s->temporal_particles[middle];
        if (entry->effect == (uintptr_t)effect)
            return entry->entity == FIELD(effect, 8) && entry->renderer == FIELD(effect, 0x90);
        if (entry->effect < (uintptr_t)effect) first = middle+1;
        else end = middle;
    }
    return 0;
}

__attribute__((section(".late_sandbox"), used, noinline))
static int temporal_particle_frozen(void *system, const void *effect) {
    const Sandbox *s = state(sandbox_delta(), 0);
    return s && s->temporal_end && s->temporal_particle_system == system &&
           temporal_particle_in_snapshot(s, effect);
}

__attribute__((section(".late_sandbox")))
void THISCALL sandbox_particle_update_queue(ParticleVector *queue, const ParticleVector *active) {
    uint32_t delta = sandbox_delta();
    typedef void (THISCALL *Assign)(ParticleVector *, const ParticleVector *);
    ((Assign)(0x7b70b0u + delta))(queue, active);
    const Sandbox *s = state(delta, 0);
    /* Filter the copy before the engine dispatches its worker. */
    if (s && s->temporal_end && s->temporal_particle_system == (const char *)active - 0x50) {
        void **write = queue->begin;
        for (void **read = queue->begin; read != queue->end; ++read)
            if (!temporal_particle_in_snapshot(s, *read)) *write++ = *read;
        queue->end = write;
    }
}

__asm__(
    ".pushsection .late_sandbox,\"ax\"\n"
    ".intel_syntax noprefix\n"
    ".globl sandbox_particle_lifetime\n"
    "sandbox_particle_lifetime:\n"
    /* The stock expiry loop keeps its system in EDI, its iterator in
     * EBX, its end in EAX, and its timestep in XMM0. */
    "mov esi, [ebx]\n"
    "push eax\n"
    "push ecx\n"
    "push edx\n"
    "sub esp, 4\n"
    "movss [esp], xmm0\n"
    "push esi\n"
    "push edi\n"
    "call temporal_particle_frozen\n"
    "add esp, 8\n"
    "test eax, eax\n"
    "movss xmm0, [esp]\n"
    "lea esp, [esp+4]\n"
    "pop edx\n"
    "pop ecx\n"
    "pop eax\n"
    "jnz 0xaf54e6\n"
    "mov ecx, [esi+0x94]\n"
    "jmp 0xaf5458\n"
    ".att_syntax prefix\n"
    ".popsection\n"
);

__attribute__((section(".late_sandbox")))
void THISCALL sandbox_scene_shader_clock(void *bindings, void *material, float time) {
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    /* Scene::Draw passes the address of its material field and copies time
     * into the shader bindings. The simulation clock can continue advancing. */
    if (s && s->temporal_end && s->scene && (uint32_t)material == s->scene + 0x11cu) {
        if (!s->temporal_scene_sampled) {
            s->temporal_scene_time = time;
            s->temporal_scene_sampled = 1;
        }
        time = s->temporal_scene_time;
    }
    typedef void (THISCALL *Apply)(void *, void *, float);
    ((Apply)(0xa95eb0u + delta))(bindings, material, time);
}

__attribute__((section(".late_sandbox"), noinline))
static void temporal_notice(uint32_t delta, Sandbox *s, uint32_t seconds, uint32_t energy) {
    if (!s->temporal_panel || !s->temporal_label) return;
    if (seconds > 99) seconds = 99;
    if (energy > 999) energy = 999;
    NativeString text = {{"00s | 000 E"}, 11, 15};
    text.text[0] += seconds / 10;
    text.text[1] += seconds % 10;
    text.text[6] += energy / 100;
    text.text[7] += energy / 10 % 10;
    text.text[8] += energy % 10;
    typedef void (THISCALL *SetText)(void *, const NativeString *);
    typedef void (THISCALL *SetVisible)(void *, uint32_t);
    ((SetText)(0xb8e900u + delta))(s->temporal_label, &text);
    ((SetVisible)(0xd05020u + delta))(s->temporal_panel, 1);
}

__attribute__((section(".late_sandbox"), noinline))
static void temporal_update_hud(uint32_t delta, Sandbox *s, uint32_t now) {
    if (!s->temporal_end || (s->temporal_hud && now-s->temporal_hud < 200u)) return;
    s->temporal_hud = now;
    int32_t remaining = (int32_t)(s->temporal_end-now);
    temporal_notice(delta, s, remaining > 0 ? ((uint32_t)remaining+999u)/1000u : 0u,
                    s->temporal_energy);
}

__attribute__((section(".late_sandbox"), noinline))
static void update_recovery_warning(uint32_t delta, Sandbox *s) {
    if (!s->recovery_panel || !s->recovery_label) return;
    if (s->recovery_seconds) {
        NativeString text = {{'0' + s->recovery_seconds}, 1, 15};
        typedef void (THISCALL *SetText)(void *, const NativeString *);
        ((SetText)(0xb8e900u + delta))(s->recovery_label, &text);
    }
    for (unsigned i = 0; i < 3; ++i)
        boss_visible(s->recovery_rings[i], s->recovery_seconds == i + 1, delta);
    boss_visible(s->recovery_panel, !!s->recovery_seconds, delta);
}

__attribute__((section(".late_sandbox"), noinline))
int sandbox_message(uint32_t delta, const uint32_t *first, const uint32_t *end,
                    const uint32_t *second, const uint32_t *second_end) {
    typedef int (*Original)(uint32_t, const uint32_t *, const uint32_t *,
                            const uint32_t *, const uint32_t *);
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x54525254u
            && second == second_end) {
        Sandbox *s = state(delta, 0);
        uint32_t entity = first[2];
        if (s && s->scene && s->thread == thread_id() && entity && entity < (1u << 29)) {
            typedef void (THISCALL *AttachTurret)(void *, uint32_t);
            ((AttachTurret)(0x7bb000u + delta))((void *)s->scene, entity);
        }
        return 1;
    }
    if (first && end == first+6 && first[0] == 0x31534654u && first[1] == 0x54415453u) {
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id() && s->token && first[2] == s->token) {
            const uint8_t *values = (const uint8_t *)(first+3);
            for (unsigned i = 0; i < TANK_STAT_COUNT; ++i)
                s->stats[i] = values[i] <= 100 ? values[i] : 0;
        }
        return 1;
    }
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x544d4941u) {
        Sandbox *s = state(delta, 0);
        uint32_t tenths = first[2];
        if (s && s->thread == thread_id() && s->aim_time_label
                && tenths <= 999u && tenths != s->aim_tenths) {
            s->aim_tenths = tenths;
            if (tenths) {
                NativeString text = {{"00.0"}, 4, 15};
                text.text[0] += tenths / 100u;
                text.text[1] += tenths / 10u % 10u;
                text.text[3] += tenths % 10u;
                if (tenths < 100u) text.text[0] = ' ';
                ((void (THISCALL *)(void *, const NativeString *))(0xb8e900u + delta))(s->aim_time_label, &text);
            }
            ((void (THISCALL *)(void *, uint32_t))(0xd05020u + delta))(s->aim_time_label, !!tenths);
        }
        return 1;
    }
    if (first && end == first+4 && first[0] == 0x31534654u && first[1] == 0x4c494241u) {
        range_ability_message(delta, first);
        return 1;
    }
    if (first && end >= first+4 && first[0] == 0x31534654u && first[1] == 0x594d5544u) {
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id() && s->token && first[2] == s->token
                && (s->allowed_keys & REMOVE_TARGET_KEY)
                && second && second_end > second && second_end - second <= 7)
            practice_roster(delta, s, second, second_end);
        return 1;
    }
    if (first && end == first+4 && first[0] == 0x31534654u && first[1] == 0x454c4f52u) {
        boss_picker_message(delta, first);
        return 1;
    }
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x50415753u) {
        Sandbox *s = state(delta, 1);
        if (s) s->swap_token = first[2];
        return 1;
    }
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x4c414548u) {
        healed_amount(delta, state(delta, 0), first[2]);
        return 1;
    }
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x43494f56u) {
        /* Stock enemy/player sighted, kill and track-damage cues. */
        if (first[2] == 2 || first[2] == 3 || first[2] == 0x22 || first[2] == 0x4f)
            crew_voice(delta, state(delta, 0), first[2]);
        return 1;
    }
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x54534f52u
            && second == second_end) {
        Sandbox *s = state(delta, 1);
        if (s) {
            if (!s->thread) s->thread = thread_id();
            if (s->thread == thread_id()) temporal_roster(delta, s, first[2]);
        }
        return 1;
    }
    if (first && end == first+4 && first[0] == 0x31534654u && first[1] == 0x454d4954u
            && second && second_end == second+3) {
        uint32_t status = first[2];
        if (status == 8) {
            uint32_t kind = first[3] >> 16, seconds = first[3] & 65535u;
            if ((kind == 0 && !seconds) || (kind == 2 && seconds && seconds <= 3)) {
                Sandbox *s = state(delta, 1);
                if (s && !s->thread) s->thread = thread_id();
                if (s && s->thread == thread_id()) {
                    s->recovery_seconds = seconds;
                    update_recovery_warning(delta, s);
                }
            }
            return 1;
        }
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id()) {
            if (status == 1) {
                s->temporal_started = clock_ms(delta);
                s->temporal_end = s->temporal_started+(first[3] & 65535u)*100u;
                s->temporal_energy = first[3] >> 16;
                s->temporal_hud = 0;
                s->temporal_scene_sampled = 0;
                s->temporal_particle_system = 0;
                s->temporal_particle_count = 0;
                s->shot = 0;
                clear_view(s);
            } else if (status == 2 || status == 6) {
                s->peer_duration = (first[3] & 65535u)*100u;
                s->peer_started = clock_ms(delta);
                s->peer_merge = 0;
                Vector point;
                __builtin_memcpy(&point, second, sizeof(point));
                s->peer_origin = (Vector){point.x, point.z, point.y + .08f};
            } else if (status == 3 || status == 4) {
                if (s->temporal_end) {
                    s->temporal_merge = clock_ms(delta);
                    s->temporal_release_gain = s->temporal_gain;
                    typedef void (THISCALL *SetVisible)(void *, uint32_t);
                    if (s->temporal_panel)
                        ((SetVisible)(0xd05020u + delta))(s->temporal_panel, 0);
                }
                s->temporal_end = 0;
                if (s->peer_started) s->peer_merge = clock_ms(delta);
            } else if (status == 5) {
                s->temporal_energy = first[3] >> 16;
                s->temporal_hud = 0;
            } else if (status == 7) {
                sandbox_notice(delta, s, 21);
            }
            if (status >= 1 && status <= 4) {
                Vector point;
                __builtin_memcpy(&point, second, sizeof(point));
                s->temporal_transition_position = (Vector){point.x, point.z, point.y + 1.15f};
                s->temporal_transition_tick = clock_ms(delta);
                s->temporal_returning = status >= 3;
                temporal_sound(delta, status);
            }
            if (status == 1 || status == 5)
                temporal_update_hud(delta, s, clock_ms(delta));
        }
        return 1;
    }
    if (first && end == first+4 && first[0] == 0x31534654u && first[1] == 0x47415246u) {
        Sandbox *s = state(delta, 1);
        if (s && s->thread == thread_id()) {
            s->score_active = 1;
            s->ally_kills = first[2];
            s->enemy_kills = first[3];
            apply_kill_score(delta, s);
        }
        return 1;
    }
    if (first && end >= first+2 && first[0] == 0x31534654u && first[1] == 0x584f4253u) {
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id() && s->owner && end == first+3
                && (second == second_end || (second && second_end == second+1)) && first[2]) {
            uint32_t capabilities = second == second_end ? 15 : second[0];
            if (capabilities & (64 | 256 | 1024 | 0x8000 | TARGET_SELECTED | TARGET_UNAVAILABLE |
                                TARGET_AI_CHANGED | TARGET_AI_MISSING | MAP_BORDER_NOTICES | PRACTICE_FEEDBACK)) {
                if (s->token == first[2]) {
                    if ((capabilities & 64) && (s->allowed_keys & 8))
                        sandbox_notice(delta, s, 8);
                    if ((capabilities & 256) && (s->allowed_keys & TELEPORT_KEY))
                        sandbox_notice(delta, s, 9);
                    if ((capabilities & 1024) && (s->allowed_keys & 0x1c0))
                        sandbox_notice(delta, s, 10 + ((capabilities >> 11) & 7));
                    if ((capabilities & 0x8000) && (s->allowed_keys & RESTORE_HP_KEY))
                        sandbox_notice(delta, s, 18);
                    if (capabilities & (TARGET_SELECTED | TARGET_UNAVAILABLE))
                        sandbox_notice(delta, s, (capabilities & TARGET_SELECTED) ? 22 : 23);
                    if (capabilities & (TARGET_AI_CHANGED | TARGET_AI_MISSING))
                        sandbox_notice(delta, s, (capabilities & TARGET_AI_CHANGED) ? 24 : 25);
                    unsigned feedback = (capabilities >> 11) & 7;
                    if ((capabilities & PRACTICE_FEEDBACK) && feedback < 5)
                        sandbox_notice(delta, s, 29 + feedback);
                    if (capabilities & MAP_BORDER_NOTICES)
                        sandbox_notice(delta, s, (capabilities & MAP_BORDER_OPEN) ? 26 :
                                       (capabilities & MAP_BORDER_CLOSED) ? 27 : 28);
                }
                return 1;
            }
            s->token = first[2];
            s->temporal_enabled = !!(capabilities & TIME_STOP);
            s->allowed_keys = (capabilities & 15) | ((capabilities & 16) ? JUMP_KEY : 0)
                              | ((capabilities & 32) ? 16 : 0)
                              | ((capabilities & 128) ? TELEPORT_KEY : 0)
                              | ((capabilities & 512) ? 0x1c0 : 0)
                              | ((capabilities & 0x4000) ? RESTORE_HP_KEY : 0)
                              | ((capabilities & 0x10000) ? GOD_MODE_KEY : 0)
                              | ((capabilities & REMOVE_TARGET) ? REMOVE_TARGET_KEY : 0)
                              | (capabilities & (PICK_TARGET | TARGET_AI | TOGGLE_MAP_BORDER | TANK_STATS | PRACTICE_MENU));
            s->last_report = 0;
        }
        return 1;
    }
    return ((Original)(0x04b1cc42u + delta))(delta, first, end, second, second_end);
}

__attribute__((section(".late_sandbox"), noinline))
void sandbox_reset(uint32_t delta, uint32_t *layer) {
    typedef void (*Original)(uint32_t, uint32_t *);
    ((Original)(0x04b1ce1du + delta))(delta, layer);
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id()) {
        s->score_active = 0;
        s->score_panel = 0;
        s->role_picker = s->role_value = s->stock_boss_panel = 0;
        s->role_token = s->role_state = s->role_keys = 0;
        s->swap_token = 0;
        s->battle_controls_root = 0;
        __builtin_memset(s->abilities, 0, sizeof(s->abilities));
        s->aim_time_label = 0;
        s->temporal_panel = s->temporal_label = 0;
        s->recovery_panel = s->recovery_label = 0;
        __builtin_memset(s->recovery_rings, 0, sizeof(s->recovery_rings));
        s->recovery_seconds = 0;
        s->peer_root = s->peer_clock = s->peer_hand = s->peer_hour = s->peer_opacity = 0;
        s->peer_hidden_entity = 0;
        __builtin_memset(s->team_panels, 0, sizeof(s->team_panels));
        reset(delta, s);
    }
}

static Sandbox *battle_state(uint32_t delta, uint32_t avatar) {
    if (!avatar || FIELD(avatar, 0) != 0x03801fc4u + delta) return 0;
    uint32_t owner = FIELD(avatar, 4);
    if (!owner || FIELD(owner, 0) != 0x037f7058u + delta || !FIELD(owner, 0x1c)) return 0;
    Sandbox *s = state(delta, 1);
    if (!s) return 0;
    if (!s->thread) s->thread = thread_id();
    if (s->thread != thread_id()) return 0;
    if (s->owner != owner || s->avatar != avatar) {
        reset(delta, s);
        s->owner = owner;
        s->avatar = avatar;
    }
    return s;
}

static void poll_controls(uint32_t delta, Sandbox *s, uint32_t now) {
    range_ability_hud(delta, s, now);
    if (!foreground(delta, s) || !s->key_state || !battle_input_active(delta)) {
        s->previous_keys = SANDBOX_KEYS | JUMP_KEY | ABILITY_KEYS;
        s->pending_actions = 0;
        s->pending_abilities = 0;
        clear_view(s);
        return;
    }
    if (!s->token) {
        if (!s->last_report || now-s->last_report >= 1250) {
            s->last_report = now ? now : 1;
            send_control(delta, s, 0, (Vector){0, 0, 0});
        }
        return;
    }
    typedef int16_t (STDCALL *Key)(int);
    Key key = (Key)s->key_state;
    uint32_t temporal_pressed = s->temporal_enabled && key(0x77) < 0; /* F8 */
    if (temporal_pressed && !s->temporal_key) {
        s->temporal_key = 1;
        send_control(delta, s, TIME_STOP, (Vector){0, 0, 0});
        return;
    }
    s->temporal_key = temporal_pressed;
    uint32_t pressed = binding_keys(delta, s) & (s->allowed_keys | ABILITY_KEYS);
    uint32_t changes = pressed & ~s->previous_keys;
    s->previous_keys = pressed;
    if (changes & 2) {
        s->toggles ^= FLIGHT;
        sandbox_notice(delta, s, 2 + !!(s->toggles & FLIGHT));
    }
    if (changes & 4) {
        s->toggles ^= MISSILE_VIEW;
        sandbox_notice(delta, s, 4 + !!(s->toggles & MISSILE_VIEW));
    }
    if (changes & JUMP_KEY) s->pending_actions |= GRAVITY_JUMP;
    if (changes & TELEPORT_KEY) s->pending_actions |= TELEPORT;
    if (changes & GOD_MODE_KEY) {
        s->toggles ^= GOD_MODE;
        sandbox_notice(delta, s, 19 + !!(s->toggles & GOD_MODE));
    }
    if (changes & RESTORE_HP_KEY) s->pending_actions |= RESTORE_HP;
    uint32_t flags = s->toggles & (BOOST | FLIGHT | FAST_RELOAD | GOD_MODE);
    if ((flags & FLIGHT) && key(0x20) < 0) flags |= ASCEND;
    if ((flags & FLIGHT) && key(0x11) < 0) flags |= DESCEND;
    Vector direction = {0, 0, 0};
    if ((s->toggles & MISSILE_VIEW) && s->shot && now-s->missile_tick < 350) {
        if (!__atomic_load_n(&s->active_view, __ATOMIC_RELAXED)) {
            s->heading = normalized(s->velocity);
            s->view_tick = 0;
            __atomic_store_n(&s->active_view, 1, __ATOMIC_RELAXED);
        }
        float yaw = limit((float)__atomic_exchange_n(&s->mouse_x, 0, __ATOMIC_RELAXED)*.0028f, .7f);
        float pitch = limit((float)__atomic_exchange_n(&s->mouse_y, 0, __ATOMIC_RELAXED)*.0028f, .7f);
        float vertical = limit(s->heading.y-pitch, .92f);
        direction = normalized((Vector){s->heading.x+yaw*s->heading.z,
                                        0, s->heading.z-yaw*s->heading.x});
        float horizontal = root(1.f-vertical*vertical);
        direction = (Vector){direction.x*horizontal, vertical, direction.z*horizontal};
        s->heading = direction;
        flags |= MISSILE_VIEW;
    } else if (__atomic_load_n(&s->active_view, __ATOMIC_RELAXED)) {
        clear_view(s);
    }
    flags |= s->pending_actions;
    uint32_t interval = (flags & (MISSILE_VIEW | ONE_SHOT_ACTIONS)) ? 55 : 170;
    if ((flags || s->sent_flags) && (!s->last_report || now-s->last_report >= interval)) {
        s->last_report = now ? now : 1;
        if (send_control(delta, s, flags, direction)) {
            s->sent_flags = flags & ~ONE_SHOT_ACTIONS;
            s->pending_actions = 0;
        }
    }
    send_range_abilities(delta, s, changes, now);
}

void sandbox_frame(uint32_t delta, uint32_t *rotator) {
    typedef void (*Original)(uint32_t, uint32_t *);
    ((Original)(0x04b1d6cdu + delta))(delta, rotator);
    Sandbox *s = rotator ? battle_state(delta, FIELD(rotator, 0x10)) : 0;
    if (!s) return;
    uint32_t now = clock_ms(delta);
    s->aim_frame_tick = now;
    uint32_t consumer = FIELD(rotator, 0x13c);
    s->scene = 0;
    if (consumer && FIELD(consumer, 0) == 0x0382efd8u + delta) {
        uint32_t battle_layer = consumer-0x160;
        uint32_t link = FIELD(battle_layer, 0x144);
        if (link) s->scene = FIELD(link, 0xc);
        /* BattleUILayer -> BattleHUD -> Screen. The game can reuse this
         * screen in the next battle without calling UIPackage::GetControl. */
        uint32_t hud = FIELD(battle_layer, 0x38);
        uint32_t root = hud ? FIELD(hud, 0x38) : 0;
        uint32_t screen = root ? FIELD(root, 0x38) : 0;
        if (screen && FIELD(screen, 0) == 0x37f9244u+delta)
            bind_battle_controls(delta, s, (void *)root);
    }
    poll_controls(delta, s, now);
}

static inline Vector marker_mix(Vector a, Vector b, float weight) {
    return (Vector){a.x + (b.x-a.x)*weight, a.y + (b.y-a.y)*weight,
                    a.z + (b.z-a.z)*weight};
}

__attribute__((section(".late_sandbox"), noinline))
static AimRay marker_ray_at(const Sandbox *s, uint32_t now) {
    const uint32_t blend_ms = 32;
    uint32_t elapsed = now - s->marker_tick;
    if (elapsed >= blend_ms) return s->marker_ray;
    float weight = (float)elapsed / (float)blend_ms;
    return (AimRay){marker_mix(s->marker_from.origin, s->marker_ray.origin, weight),
                    marker_mix(s->marker_from.velocity, s->marker_ray.velocity, weight)};
}

/* Some gun controllers bypass the local aim hook. The periodic server
 * gun-marker update still runs on the local AvatarGameLogic. */
__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_gun_marker(void *avatar, const Vector *origin,
                                const Vector *velocity, const float *extra) {
    typedef void (THISCALL *Update)(void *, const Vector *, const Vector *, const float *);
    uint32_t delta = sandbox_delta();
    ((Update)(0x1846390u + delta))(avatar, origin, velocity, extra);
    Sandbox *s = battle_state(delta, (uint32_t)avatar);
    if (s) {
        uint32_t now = clock_ms(delta);
        if (*extra > 0.f && __builtin_isfinite(*extra)) {
            /* BigWorld packets are Y-up. The native trace is DAVA Z-up. */
            AimRay incoming = {{origin->x, origin->z, origin->y},
                               {velocity->x, velocity->z, velocity->y}};
            void *rotator = (void *)FIELD(avatar, 0x1fc);
            Vector previous = s->marker_ray.origin;
            float dx = incoming.origin.x - previous.x;
            float dy = incoming.origin.y - previous.y;
            float dz = incoming.origin.z - previous.z;
            s->marker_from = rotator && s->marker_rotator == rotator && now - s->marker_tick <= 100u
                && dx*dx + dy*dy + dz*dz < 64.f ? marker_ray_at(s, now) : incoming;
            s->marker_ray = incoming;
            s->marker_tick = now;
            s->marker_dispersion = *extra;
            s->marker_rotator = rotator;
        } else s->marker_rotator = 0;
        if (!s->aim_frame_tick || now - s->aim_frame_tick > 100)
            poll_controls(delta, s, now);
    }
}

__attribute__((section(".late_sandbox"), noinline))
void *THISCALL sandbox_compute_marker(void *rotator, const AimRay *ray, float dispersion) {
    typedef void *(THISCALL *Compute)(void *, const AimRay *, float);
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id() && s->marker_rotator == rotator) {
        uint32_t now = clock_ms(delta);
        if (now - s->marker_tick <= 500u) {
            AimRay rendered = marker_ray_at(s, now);
            /* Show bloom immediately, even while the center is interpolated. */
            return ((Compute)(0x1e111c0u + delta))(rotator, &rendered, s->marker_dispersion);
        }
    }
    return ((Compute)(0x1e111c0u + delta))(rotator, ray, dispersion);
}

__attribute__((section(".late_sandbox"), used, noinline))
static void spread_ring_layout(void *layer, float dx, float dy) {
    float radius = .5f * root(dx*dx + dy*dy);
    if (!__builtin_isfinite(radius)) return;

    uint32_t delta = sandbox_delta();
    typedef void (THISCALL *SetPoint)(void *, const Point *);
    SetPoint set_scale = (SetPoint)(0xcff760u + delta);
    SetPoint set_pivot = (SetPoint)(0xcff410u + delta);
    void **const segments = (void **)FIELD(layer, 0x210);
    const float stroke = 1.25f;
    /* The 32 native controls already rotate around a common center.
       Adjacent tangents overlap slightly to make a continuous outline. */
    Point scale = {(.2f * radius + .25f) / 6.f, stroke / 6.f};
    Point pivot = {3.f, radius / scale.y + 3.f};
    for (uint32_t i = 0; i < 32; ++i) {
        set_scale(segments[i], &scale);
        set_pivot(segments[i], &pivot);
    }
}

__asm__(
    ".pushsection .late_sandbox,\"ax\"\n"
    ".intel_syntax noprefix\n"
    ".globl sandbox_spread_ring\n"
    "sandbox_spread_ring:\n"
    /* The projection just produced the screen-space offset of a full
       world-space diameter in XMM2/XMM1. Keep the native reload styling. */
    "sub esp, 8\n"
    "movss [esp], xmm2\n"
    "movss [esp+4], xmm1\n"
    "push esi\n"
    "call spread_ring_layout\n"
    "add esp, 12\n"
    "jmp 0x1bca15a\n"
    ".att_syntax prefix\n"
    ".popsection\n");

void THISCALL sandbox_move(void *logic, const uint32_t *shooter, const uint32_t *shot,
        const Vector *position, const Vector *aux, const Vector *velocity,
        const float *speed, const uint8_t *effect, const float *extra) {
    typedef void (THISCALL *Move)(void *, const uint32_t *, const uint32_t *, const Vector *,
                                  const Vector *, const Vector *, const float *, const uint8_t *, const float *);
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id() && s->avatar == (uint32_t)logic
            && *shooter == FIELD(s->owner, 0x28)
            && (!s->temporal_end || *shot >= BRANCH_SHOT_BASE)) {
        if (s->shot != *shot) clear_view(s);
        s->shot = *shot;
        s->position = *position;
        s->velocity = *velocity;
        s->missile_tick = clock_ms(delta);
    }
    ((Move)(0x0183bc70u + delta))(logic, shooter, shot, position, aux, velocity, speed, effect, extra);
}

void THISCALL sandbox_stop(void *logic, const uint32_t *shot, const Vector *position, const uint8_t *kind) {
    typedef void (THISCALL *Stop)(void *, const uint32_t *, const Vector *, const uint8_t *);
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id() && s->avatar == (uint32_t)logic && s->shot == *shot) {
        s->shot = 0;
        clear_view(s);
    }
    ((Stop)(0x01845dd0u + delta))(logic, shot, position, kind);
}

static CameraPose aim_missile_camera(Sandbox *s, uint32_t now) {
    Vector axis = normalized(s->velocity);
    float ahead = (float)(now-s->missile_tick)*.001f;
    if (ahead > .12f) ahead = .12f;
    Vector point = {s->position.x+s->velocity.x*ahead-axis.x*1.25f,
                    s->position.y+s->velocity.y*ahead-axis.y*1.25f+.38f,
                    s->position.z+s->velocity.z*ahead-axis.z*1.25f};
    if (!s->view_tick) {
        s->view = point;
    } else {
        float mix = (float)(now-s->view_tick)*.025f;
        if (mix > 1) mix = 1;
        s->view.x += mix*(point.x-s->view.x);
        s->view.y += mix*(point.y-s->view.y);
        s->view.z += mix*(point.z-s->view.z);
    }
    s->view_tick = now ? now : 1;
    Vector view = s->view, heading = s->heading;
    return (CameraPose){{view.x, view.z, view.y},
                        {view.x+heading.x, view.z+heading.z, view.y+heading.y}};
}

static uint32_t battle_camera(uint32_t delta, uint32_t scene) {
    uint32_t camera = FIELD(scene, 0x120);
    if (camera && (FIELD(camera, 0) == 0x0373ad3cu+delta ||
                   FIELD(camera, 0) == 0x0382da70u+delta)) return camera;
    return 0;
}

/* The battle camera keeps the undistorted native angle at +0x32c.
 * Feed the adjusted angle to the native projection setter so world markers,
 * frustum culling and pointer projection all use the same camera. */
__attribute__((section(".late_sandbox"), noinline))
static void set_battle_fov(uint32_t delta, Sandbox *s, uint32_t camera, float stock) {
    typedef void (THISCALL *Set)(void *, float);
    float angle = stock;
    if (s && FIELD(camera, 0x320) != 0 && stock >= 40.f &&
            !__atomic_load_n(&s->active_view, __ATOMIC_RELAXED))
        angle += (float)s->fov - *(const float *)(camera+0x334);
    ((Set)(0x15eaf10u + delta))((void *)camera, angle);
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_battle_fov(void *camera, float stock) {
    uint32_t delta = sandbox_delta();
    set_battle_fov(delta, state(delta, 0), (uint32_t)camera, stock);
}

__attribute__((section(".late_sandbox"), noinline))
static void refresh_battle_fov(uint32_t delta, Sandbox *s, uint32_t camera) {
    if (camera && FIELD(camera, 0) == 0x0382da70u+delta)
        set_battle_fov(delta, s, camera, *(const float *)(camera+0x32c));
}

static CameraPose swap_pose(uint32_t camera, CameraPose pose) {
    CameraPose *current = (CameraPose *)(camera+0x38);
    CameraPose previous = *current;
    *current = pose;
    FIELD(camera, 0x248) |= 1;
    return previous;
}

__attribute__((section(".late_sandbox"), noinline))
static float temporal_project(uint32_t delta, uint32_t camera, Vector p, Point *point, float far) {
    typedef const float *(THISCALL *ViewProj)(uint32_t, uint32_t);
    const float *m = ((ViewProj)(0xa7aee0u + delta))(camera, 0);
    float depth = p.x*m[3]+p.y*m[7]+p.z*m[11]+m[15];
    if (!(depth > .5f && depth < far)) return 0;
    float x = (p.x*m[0]+p.y*m[4]+p.z*m[8]+m[12])/depth;
    float y = (p.x*m[1]+p.y*m[5]+p.z*m[9]+m[13])/depth;
    if (!(x > -1.25f && x < 1.25f && y > -1.35f && y < 1.15f)) return 0;
    *point = (Point){.5f + .5f*x, .5f - .5f*y};
    return depth;
}

/* The clock faces the camera and scales with its distance from the viewer. */
__attribute__((section(".late_sandbox"), noinline))
static void temporal_clock(uint32_t delta, Sandbox *s, uint32_t camera, uint32_t now) {
    if (!s->peer_clock || !s->peer_started) return;
    typedef void (THISCALL *Visible)(void *, uint32_t);
    Visible visible = (Visible)(0xd05020u + delta);
    if (s->peer_merge && now-s->peer_merge >= 420u) {
        visible(s->peer_clock, 0);
        s->peer_started = 0;
        return;
    }
    Point point;
    float depth = temporal_project(delta, camera, s->peer_origin, &point, 260.f);
    if (!depth) {
        visible(s->peer_clock, 0);
        return;
    }
    typedef Rect *(THISCALL *GetRect)(void *, Rect *);
    Rect bounds;
    ((GetRect)(0xcdf810u + delta))(s->peer_root, &bounds);
    point.x *= bounds.width;
    point.y *= bounds.height;
    uint32_t elapsed = now-s->peer_started;
    float entrance = elapsed < 280u ? 1.f-(float)elapsed/280.f : 0.f;
    float reveal = 1.f-entrance*entrance*entrance;
    float cycle = (float)(elapsed % 2600u)/1300.f-1.f;
    float opacity = reveal*(.95f+.05f*cycle*cycle);
    float shrink = 1.f;
    if (s->peer_merge) {
        float remaining = 1.f-(float)(now-s->peer_merge)/420.f;
        opacity *= remaining*remaining;
        shrink = .55f+.45f*remaining;
    }
    float scale = .037f*bounds.width/depth;
    if (scale < .155f) scale = .155f;
    if (scale > .65f) scale = .65f;
    Point scaling = {scale*(.91f+.09f*reveal), scale*shrink*(.48f+.52f*reveal)};
    typedef void (THISCALL *SetPoint)(void *, const Point *);
    typedef void (THISCALL *SetFloat)(void *, float);
    ((SetPoint)(0xcff4a0u + delta))(s->peer_clock, &point);
    ((SetPoint)(0xcff760u + delta))(s->peer_clock, &scaling);
    if (s->peer_opacity) ((SetFloat)(0x102ab30u + delta))(s->peer_opacity, opacity);
    /* The branch can wait for a safe hull placement after the timer ends. */
    float travel = elapsed < s->peer_duration ? (float)elapsed/s->peer_duration : 1.f;
    SetFloat angle = (SetFloat)(0xcfd470u + delta);
    if (s->peer_hand) angle(s->peer_hand, .890118f+6.2831853f*travel);
    if (s->peer_hour) angle(s->peer_hour, -1.012291f+.5235988f*travel);
    visible(s->peer_clock, 1);
}

/* MarkerSystem already hides the control when this predicate is false.
 * The client's world transform uses XY for the ground plane. A parked
 * enemy copy (battle.chameleon_pose) has both coordinates set to -4096. */
__attribute__((section(".late_sandbox"), noinline))
uint8_t THISCALL sandbox_marker_visible(void *marker) {
    if (!*(const uint8_t *)((uintptr_t)marker + 0x1d)) return 0;
    uint32_t entity = FIELD(marker, 8);
    uint32_t transform = entity ? FIELD(entity, 0x3c) : 0;
    if (!transform) return 1;
    const Vector *position = (const Vector *)(transform + 0x38);
    return position->x != -4096.f || position->y != -4096.f;
}

/* MarkerSystem projects the world positions during the scene update, before
 * the draw hook runs. Use the same pose in both passes. */
void THISCALL sandbox_project_markers(void *system, float elapsed) {
    typedef void (THISCALL *Update)(void *, float);
    uint32_t delta = sandbox_delta();
    Update update = (Update)(0x82c280u + delta);
    Sandbox *s = state(delta, 0);
    uint32_t scene = FIELD(system, 0x28);
    uint32_t camera = 0;
    if (s && s->thread == thread_id() && scene && scene == s->scene)
        camera = battle_camera(delta, scene);
    refresh_battle_fov(delta, s, camera);
    if (!camera || !__atomic_load_n(&s->active_view, __ATOMIC_RELAXED)) {
        update(system, elapsed);
        return;
    }
    s->marker_pose = aim_missile_camera(s, clock_ms(delta));
    s->marker_pose_ready = 1;
    CameraPose previous = swap_pose(camera, s->marker_pose);
    update(system, elapsed);
    swap_pose(camera, previous);
}

/* The replay camera toggle hides the vehicle marker layer alongside the HUD.
 * Let the existing marker system keep updating the recorded targets. */
void THISCALL sandbox_keep_replay_markers(void *controller, uint32_t hud_visible, uint32_t replay) {
    uint32_t delta = sandbox_delta();
    typedef void (THISCALL *SetHud)(void *, uint32_t, uint32_t);
    typedef void (THISCALL *SetVisible)(void *, uint32_t);
    ((SetHud)(0x1751400u + delta))(controller, hud_visible, replay);
    if (hud_visible || !replay) return;
    uint32_t context = FIELD(controller, 0x150);
    uint32_t layers = FIELD(context, 0x38);
    void *markers = (void *)FIELD(layers, 0x5c);
    if (markers && FIELD(markers, 0) == 0x382ee60u + delta)
        ((SetVisible)(0xd05020u + delta))(markers, 1);
}

static void sample_camera_blur(Sandbox *s, uint32_t camera, uint32_t now) {
    const Vector position = *(Vector *)(camera+0x38);
    const Vector target = *(Vector *)(camera+0x44);
    Vector direction = normalized((Vector){target.x-position.x, target.y-position.y,
                                           target.z-position.z});
    uint32_t elapsed = now-s->blur_tick;
    s->blur_offset[0] = s->blur_offset[1] = 0;
    if (s->blur_camera == camera && s->blur_tick && elapsed && elapsed < 100) {
        Vector previous = s->blur_direction;
        float horizontal = root(direction.x*direction.x + direction.y*direction.y);
        float agreement = previous.x*direction.x + previous.y*direction.y + previous.z*direction.z;
        if (horizontal > .1f && agreement > .94f) {
            float yaw = (previous.x*direction.y-previous.y*direction.x)/horizontal;
            float pitch = previous.z-direction.z;
            float exposure = 18.f/(float)elapsed;
            s->blur_offset[0] = limit(yaw*exposure*.75f, .045f) * (s->blur_percent*.0005f);
            s->blur_offset[1] = limit(pitch*exposure, .045f) * (s->blur_percent*.0005f);
        }
    }
    s->blur_camera = camera;
    s->blur_direction = direction;
    s->blur_tick = now;
}

__attribute__((section(".late_sandbox"), noinline))
static void sample_temporal_glitch(uint32_t delta, Sandbox *s, uint32_t camera, uint32_t now) {
    s->temporal_glitch[2] = 0;
    uint32_t elapsed = now-s->temporal_transition_tick;
    uint32_t duration = s->temporal_returning ? 230u : 180u;
    if (!s->temporal_transition_tick || !camera || elapsed >= duration) return;
    Point point;
    float depth = temporal_project(delta, camera, s->temporal_transition_position, &point, 260.f);
    if (!depth) return;
    float radius = 2.5f/depth;
    if (radius > .19f) radius = .19f;
    s->temporal_glitch[0] = point.x;
    s->temporal_glitch[1] = point.y;
    s->temporal_glitch[2] = radius;
    float progress = (float)elapsed/duration + .001f;
    s->temporal_glitch[3] = s->temporal_returning ? -progress : progress;
}

/* UISceneSystem selects the material that composites the scene before the HUD. */
__attribute__((section(".late_sandbox"), noinline))
void *sandbox_blur_material(int alpha, int upscale, int grading) {
    typedef void *(*Select)(int, int, int);
    typedef void (THISCALL *Intern)(uint32_t *, const char *);
    typedef void (THISCALL *Property)(void *, const uint32_t *, const float *, int, int, int);
    uint32_t delta = sandbox_delta();
    void *material = ((Select)(0xa84af0u + delta))(alpha, upscale, grading);
    Sandbox *s = state(delta, 0);
    if (material && s && s->thread == thread_id()) {
        float offset[4] = {0};
        uint32_t now = clock_ms(delta);
        if (s->blur_percent && s->blur_scene && s->blur_scene == s->scene &&
                now-s->blur_tick < 100) {
            offset[0] = s->blur_offset[0];
            offset[1] = s->blur_offset[1];
        }
        if (s->temporal_end) {
            float p = (float)(now-s->temporal_started)/260.f;
            if (p > 1.f) p = 1.f;
            offset[2] = .70f * (1.f-(1.f-p)*(1.f-p)*(1.f-p));
            offset[3] = .20f + .48f*(1.f-p)*(1.f-p);
        } else if (s->temporal_merge && now-s->temporal_merge < 420u) {
            float p = 1.f-(float)(now-s->temporal_merge)/420.f;
            offset[2] = .70f*p*p;
            offset[3] = .72f*p*p;
        }
        uint32_t property;
        ((Intern)(0x931830u + delta))(&property, "solbreakerCameraBlur");
        ((Property)(0xca2e30u + delta))(material, &property, offset, 3, 1, 0);
        s->blur_scene = 0;
    }
    return material;
}

/* Project into the sampled UV rectangle, which can use only part of the texture. */
__attribute__((section(".late_sandbox"), noinline))
static void apply_temporal_glitch(void *material, const Rect *uv, uint32_t delta) {
    Sandbox *s = state(delta, 0);
    if (!material || !s || s->thread != thread_id()) return;
    float radius = s->temporal_glitch[2];
    if (radius && !s->temporal_glitch_property) {
        typedef void (THISCALL *Intern)(uint32_t *, const char *);
        ((Intern)(0x931830u + delta))(&s->temporal_glitch_property, "solbreakerTemporalGlitch");
    }
    if (s->temporal_glitch_property) {
        float effect[4] = {
            uv->x + uv->width*s->temporal_glitch[0],
            uv->y + uv->height*s->temporal_glitch[1],
            radius*uv->width, s->temporal_glitch[3],
        };
        typedef void (THISCALL *Property)(void *, const uint32_t *, const float *, int, int, int);
        ((Property)(0xca2e30u + delta))(material, &s->temporal_glitch_property, effect, 3, 1, 0);
        s->temporal_glitch[2] = 0;
    }
}

typedef void (THISCALL *SceneBlit)(void *, void *, void *, const void *, const Rect *, const Rect *);

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_scene_blit(void *draw, void *texture, void *material, const void *color,
                               const Rect *destination, const Rect *uv) {
    uint32_t delta = sandbox_delta();
    apply_temporal_glitch(material, uv, delta);
    ((SceneBlit)(0xa6bc30u + delta))(draw, texture, material, color, destination, uv);
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_scene_graded_blit(void *draw, void *texture, void *material, const void *color,
                                      const Rect *destination, const Rect *uv) {
    uint32_t delta = sandbox_delta();
    apply_temporal_glitch(material, uv, delta);
    ((SceneBlit)(0xa6a150u + delta))(draw, texture, material, color, destination, uv);
}

void THISCALL sandbox_draw(void *scene) {
    typedef void (THISCALL *Draw)(void *);
    uint32_t delta = sandbox_delta();
    Draw draw = (Draw)(0x00d54070u + delta);
    Sandbox *s = state(delta, 0);
    if (s && (s->temporal_end || s->temporal_gain) && s->thread == thread_id()) {
        uint32_t now = clock_ms(delta);
        temporal_audio_update(delta, s, now);
        temporal_update_hud(delta, s, now);
    }
    if (s && s->owner && s->thread == thread_id() && (s->role_state || s->swap_token || s->picker.menu))
        boss_picker_update(delta, s);
    if (!s || s->thread != thread_id() || s->scene != (uint32_t)scene) {
        draw(scene);
        return;
    }
    if (s->peer_hidden_entity) temporal_roster(delta, s, s->peer_hidden_entity);
    uint32_t camera = battle_camera(delta, (uint32_t)scene);
    refresh_battle_fov(delta, s, camera);
    int missile_view = camera && __atomic_load_n(&s->active_view, __ATOMIC_RELAXED);
    CameraPose previous;
    if (missile_view) {
        CameraPose pose = s->marker_pose_ready ? s->marker_pose : aim_missile_camera(s, clock_ms(delta));
        previous = swap_pose(camera, pose);
        s->marker_pose_ready = 0;
    }
    if (camera && s->blur_percent) {
        sample_camera_blur(s, camera, clock_ms(delta));
        s->blur_scene = (uint32_t)scene;
    }
    uint32_t now = clock_ms(delta);
    sample_temporal_glitch(delta, s, camera, now);
    if (camera && s->peer_started) temporal_clock(delta, s, camera, now);
    draw(scene);
    if (missile_view) swap_pose(camera, previous);
}

void sandbox_mouse(uint32_t delta, int32_t x, int32_t y) {
    Sandbox *s = state(delta, 0);
    if (s && __atomic_load_n(&s->active_view, __ATOMIC_RELAXED)) {
        __atomic_fetch_add(&s->mouse_x, x, __ATOMIC_RELAXED);
        __atomic_fetch_add(&s->mouse_y, y, __ATOMIC_RELAXED);
    }
}

__attribute__((section(".boss_exit")))
void THISCALL sandbox_boss_menu_open(void *menu, uint32_t action) {
    typedef void *(THISCALL *Get)(void *);
    typedef void (THISCALL *Open)(void *, uint32_t);
    typedef void (THISCALL *ShowExit)(void *);
    uint32_t delta = sandbox_delta();
    void *context = (void *)FIELD(menu, 0x164);
    void *logic = context ? ((Get)FIELD(FIELD(context, 0), 0xa0))(context) : 0;
    void *avatar = logic && FIELD(logic, 0) == 0x03801fc4u + delta
                   ? ((Get)FIELD(FIELD(logic, 0), 4))(logic) : 0;
    /* Preserve Exit to Hangar in local battles that use stock PvP modes. */
    if (avatar) {
        uint8_t mode = *(const uint8_t *)((uintptr_t)avatar + 0xd9);
        if (mode == 1 || mode == 8 || mode == 23 || mode == 27 || mode == 42)
            ((ShowExit)(0x01df6db0u + delta))(menu);
    }
    boss_respawn_menu(menu, delta);
    ((Open)(0x0194c490u + delta))(menu, action);
}

__asm__(
    ".intel_syntax noprefix\n"
    ".globl sandbox_delta\n"
    "sandbox_delta:\n"
    "call .Lsandbox_pc\n"
    ".Lsandbox_pc: pop eax\n"
    "sub eax, offset .Lsandbox_pc\n"
    "ret\n"
    ".globl sandbox_mouse_hook\n"
    "sandbox_mouse_hook:\n"
    "push ecx\n"
    "push dword ptr [ebp+12]\n"
    "push dword ptr [ebp+8]\n"
    "call sandbox_delta\n"
    "push eax\n"
    "call sandbox_mouse\n"
    "add esp, 12\n"
    "pop ecx\n"
    "jmp 0xaba090\n"
    ".att_syntax prefix\n");

/* Keep the wire options outside the editable description. The view's stock
 * setter receives the canonical room comment before it fills the text field. */
static int room_prefix(const char *value, const char *prefix) {
    while (*prefix && *value == *prefix) { ++value; ++prefix; }
    return !*prefix;
}

static const char *room_text(const NativeString *value) {
    return value->capacity < 16 ? value->text : *(const char * const *)value->text;
}

static void room_parameter(void *view, const char *name, int value) {
    /* The battle settings adapter takes ECX=view, EDX=name, EAX=value,
     * and EBX=the relocated image base. */
    uint32_t base = 0x400000u + sandbox_delta();
    __asm__ volatile ("push %%ebx; mov %[base], %%ebx; call *%[entry]; pop %%ebx"
        : "+c"(view), "+d"(name), "+a"(value)
        : [base]"r"(base), [entry]"S"(base + 0x471df29u)
        : "memory", "cc");
}

typedef struct { int mode, lives, minutes, bots1, bots2, ammo; } RoomOptions;

static const char *room_parse_options(const NativeString *value, RoomOptions *settings) {
    const char *text = setup_read_room_tag(room_text(value), 0);
    int mode = *text ? 1 : -1, lives = 0, minutes = -1, ammo = 0;
    while (room_prefix(text, "[mode ") || room_prefix(text, "[lives ") ||
           room_prefix(text, "[time ") || room_prefix(text, "[ammo ")) {
        if (room_prefix(text, "[mode ")) {
            if (room_prefix(text + 6, "elimination]")) mode = 0;
            if (room_prefix(text + 6, "supremacy]")) mode = 2;
            if (room_prefix(text + 6, "gravity]")) mode = 3;
            if (room_prefix(text + 6, "big_boss]")) mode = 4;
            if (room_prefix(text + 6, "mad_games]")) mode = 5;
            if (room_prefix(text + 6, "uprising]")) mode = 6;
        } else if (room_prefix(text, "[lives ")) {
            lives = room_prefix(text + 7, "unlimited]");
        } else if (room_prefix(text, "[ammo ")) {
            ammo = room_prefix(text + 6, "unlimited]");
        } else {
            const char *digits = text + 6;
            minutes = room_prefix(digits, "default]") ? -1 : 0;
            while (*digits >= '0' && *digits <= '9' && minutes <= 60)
                minutes = minutes * 10 + *digits++ - '0';
        }
        while (*text && *text != ']') ++text;
        if (*text) ++text;
        while (*text == ' ') ++text;
    }
    *settings = (RoomOptions){mode, lives, minutes, 0, 0, ammo};
    if (room_prefix(text, "[bots ") && text[6] >= '0' && text[6] <= '7' &&
        text[7] == ':' && text[8] >= '0' && text[8] <= '7' && text[9] == ']') {
        settings->bots1 = text[6] - '0';
        settings->bots2 = text[8] - '0';
        text += 10;
        while (*text == ' ') ++text;
    }
    if (room_prefix(text, "[notes]")) {
        text += 7;
        if (*text == ' ') ++text;
    }
    return text;
}

void THISCALL room_load_description(void *view, const NativeString *value) {
    typedef void (THISCALL *Set)(void *, const NativeString *);
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Destroy)(NativeString *);
    uint32_t delta = sandbox_delta();
    RoomOptions options;
    const char *text = room_parse_options(value, &options);
    room_parameter(view, "roomInitialMode", options.mode);
    room_parameter(view, "roomInitialLives", options.lives);
    room_parameter(view, "roomInitialAmmo", options.ammo);
    room_parameter(view, "roomInitialMinutes", options.minutes);
    room_parameter(view, "roomInitialBots1", options.bots1);
    room_parameter(view, "roomInitialBots2", options.bots2);
    RoomLineup lineup = {0};
    const char *encoded = room_text(value);
    int has_lineup = setup_read_room_tag(encoded, &lineup) != encoded;
    setup_load_room(delta, view, lineup, has_lineup ? -1 : options.bots1, options.bots2);
    NativeString clean;
    ((String)(0x69a7d0u + delta))(&clean, text);
    ((Set)(0x1e98000u + delta))(view, &clean);
    ((Destroy)(0x69a720u + delta))(&clean);
}

void THISCALL room_header_description(void *view, const NativeString *value) {
    typedef void (THISCALL *Set)(void *, const NativeString *);
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Destroy)(NativeString *);
    uint32_t delta = sandbox_delta();
    RoomOptions options;
    const char *text = room_parse_options(value, &options);
    room_parameter(view, "roomHeaderMode", options.mode);
    room_parameter(view, "roomHeaderMinutes", options.minutes);
    RoomLineup lineup;
    const char *encoded = room_text(value);
    if (setup_read_room_tag(encoded, &lineup) != encoded && lineup.custom) {
        options.bots1 = options.bots2 = 0;
        for (unsigned i = 0; i < 7; ++i) {
            options.bots1 += lineup.slots[i] != UINT32_MAX;
            options.bots2 += lineup.slots[i+7] != UINT32_MAX;
        }
    }
    room_parameter(view, "roomHeaderBots1", options.bots1);
    room_parameter(view, "roomHeaderBots2", options.bots2);
    NativeString clean;
    ((String)(0x69a7d0u + delta))(&clean, text);
    ((Set)(0x1e98010u + delta))(view, &clean);
    ((Destroy)(0x69a720u + delta))(&clean);
}

NativeString *THISCALL room_get_description(void *view, NativeString *result) {
    typedef NativeString *(THISCALL *Get)(void *, NativeString *);
    typedef void *(THISCALL *Find)(void *, const NativeString *);
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Destroy)(NativeString *);
    typedef void (THISCALL *Assign)(NativeString *, const char *, uint32_t);
    uint32_t delta = sandbox_delta();
    ((Get)(0x1e8a240u + delta))(view, result);
    NativeString path;
    ((String)(0x69a7d0u + delta))(&path, "Settings/**/LocalRoomOptions");
    void *field = ((Find)(0xcde480u + delta))((void *)FIELD(view, 4), &path);
    ((Destroy)(0x69a720u + delta))(&path);
    if (result->size > 4096) return result;
    const char *description = room_text(result);
    if (room_prefix(description, "SolbreakerBots/")) {
        setup_save_description(delta, view, result);
        return result;
    }
    if (!field) return result;
    NativeString options;
    ((Get)(0xb7b0f0u + delta))(field, &options);
    const char *tags = room_text(&options);
    /* The same stock view also submits the solo bot recipe. */
    if (options.size > 0 && options.size < 128) {
        static const char marker[] = "[room-form] ";
        char combined[4096 + 128 + sizeof(marker) + 10 + 2*sizeof(RoomLineup)];
        uint32_t length = setup_room_tag(delta, view, combined);
        for (uint32_t i = 0; i < sizeof(marker)-1; ++i) combined[length++] = marker[i];
        for (uint32_t i = 0; i < options.size; ++i) combined[length++] = tags[i];
        for (uint32_t i = 0; i < result->size; ++i) combined[length++] = description[i];
        ((Assign)(0x6a4ef0u + delta))(result, combined, length);
    }
    ((Destroy)(0x69a720u + delta))(&options);
    return result;
}

/* GraphicsOptionsPage's slider uses the native input handler, with a local
 * percentage receipt so changing the render preset cannot overwrite it. */
static void update_blur_label(void *label, uint32_t percent, uint32_t delta) {
    typedef void (THISCALL *Set)(void *, const NativeString *);
    NativeString text = {{0}, 0, 15};
    if (percent == 100) text.text[text.size++] = '1';
    if (percent >= 10) text.text[text.size++] = '0' + (percent / 10) % 10;
    text.text[text.size++] = '0' + percent % 10;
    text.text[text.size++] = '%';
    ((Set)(0xb8e900u + delta))(label, &text);
}

static void *find_control(void *root, const char *name, uint32_t delta) {
    typedef void *(THISCALL *Find)(void *, const NativeString *);
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Destroy)(NativeString *);
    NativeString path;
    ((String)(0x69a7d0u + delta))(&path, name);
    void *control = ((Find)(0xcde480u + delta))(root, &path);
    ((Destroy)(0x69a720u + delta))(&path);
    return control;
}

#define BOSS_UI __attribute__((section(".boss_ui"), noinline))
#define BOSS_DATA __attribute__((section(".boss_rodata")))

static const char boss_picker_path[] BOSS_DATA = "SolBossPicker";
static const char boss_role_path[] BOSS_DATA = "SolRoleValue";
static const char boss_level_path[] BOSS_DATA = "SolRoleLevel";
static const char stock_boss_path[] BOSS_DATA = "AbilityPanel";
static const char respawn_button_path[] BOSS_DATA = "menuDialog/**/SolRespawnButton";
static const char respawn_value_path[] BOSS_DATA = "SolRespawnValue";
static const char swap_button_path[] BOSS_DATA = "menuDialog/**/SolChangeTankButton";
static const char swap_value_path[] BOSS_DATA = "SolChangeTankValue";

BOSS_UI static unsigned picker_value(void *field, uint32_t delta) {
    typedef NativeString *(THISCALL *Get)(void *, NativeString *);
    typedef void (THISCALL *Destroy)(NativeString *);
    NativeString value;
    ((Get)(0xb7b0f0u + delta))(field, &value);
    unsigned number = 0;
    const char *text = room_text(&value);
    if (value.size && value.size <= 5)
        for (unsigned i = 0; i < value.size && text[i] >= '0' && text[i] <= '9'; ++i)
            number = number * 10 + text[i] - '0';
    ((Destroy)(0x69a720u + delta))(&value);
    return number;
}

BOSS_UI static void boss_visible(void *control, uint32_t visible, uint32_t delta) {
    typedef void (THISCALL *Visible)(void *, uint32_t);
    if (control) ((Visible)(0xd05020u + delta))(control, visible);
}

__attribute__((section(".late_sandbox"), noinline))
static void range_ability_message(uint32_t delta, const uint32_t *words) {
    Sandbox *s = state(delta, 0);
    unsigned slot = words[3] & 255u;
    unsigned status = (words[3] >> 8) & 255u;
    unsigned remaining = words[3] >> 16;
    if (!s || s->thread != thread_id() || !words[2] || slot >= 3 || status > 3) return;
    if (s->token && s->token != words[2]) return;
    s->ability_token = words[2];
    AbilitySlot *item = &s->abilities[slot];
    item->state = status;
    item->ends = clock_ms(delta) + 1000u * remaining;
    item->shown = UINT32_MAX;
}

__attribute__((section(".late_sandbox"), noinline))
static void range_ability_hud(uint32_t delta, Sandbox *s, uint32_t now) {
    typedef void (THISCALL *SetText)(void *, const NativeString *);
    SetText set_text = (SetText)(0xb8e900u + delta);
    if (!s->abilities[0].row) return;
    unsigned active = s->token && s->ability_token == s->token;
    for (unsigned slot = 0; slot < 3; slot++) {
        AbilitySlot *item = &s->abilities[slot];
        unsigned status = active ? item->state : 0;
        int32_t duration = item->ends-now;
        unsigned seconds = status > 1 && duration > 0 ? ((unsigned)duration+999u)/1000u : 0;
        unsigned update = (seconds << 8) | status;
        if (item->shown == update) continue;
        item->shown = update;
        boss_visible(item->row, !!status, delta);
        boss_visible(item->dim, status == 3, delta);
        if (item->time) {
            NativeString text = {{0}, 0, 15};
            if (seconds && seconds <= 99) {
                if (seconds >= 10) text.text[text.size++] = '0' + seconds/10;
                text.text[text.size++] = '0' + seconds%10;
            }
            set_text(item->time, &text);
        }
        if (status && item->key) {
            void *layout = keyboard_layout(delta);
            unsigned action = slot ? ABILITY_TWO_ACTION + slot - 1 : ABILITY_ONE_ACTION;
            KeyBinding key = layout ? get_binding(delta, layout, action) : (KeyBinding){0};
            NativeString text = {{0}, 0, 15};
            if (key.device == 0x400 && !key.modifiers && key.key >= 26 && key.key <= 35)
                text.text[text.size++] = '0' + key.key-26u;
            set_text(item->key, &text);
        }
    }
}

__attribute__((section(".late_sandbox"), noinline))
static void send_range_abilities(uint32_t delta, Sandbox *s, uint32_t changes, uint32_t now) {
    s->pending_abilities |= (changes & ABILITY_KEYS) >> ABILITY_SHIFT;
    if (!s->pending_abilities || (s->last_report && now-s->last_report < 35)) return;
    for (unsigned slot = 0; slot < 3; slot++) {
        unsigned mask = 1u << slot;
        if (!(s->pending_abilities & mask)) continue;
        if (s->abilities[slot].state != 1) {
            s->pending_abilities &= ~mask;
            continue;
        }
        if (send_control_value(delta, s, USE_ABILITY, (Vector){0, 0, 0}, slot)) {
            s->pending_abilities &= ~mask;
            s->last_report = now ? now : 1;
        }
        break;
    }
}

BOSS_UI static void target_text(uint32_t delta, void *label, const char *text, int localize) {
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Localize)(void *, NativeString *, const NativeString *);
    typedef void (THISCALL *Set)(void *, const NativeString *);
    typedef void (THISCALL *Destroy)(NativeString *);
    if (!label) return;
    NativeString key, translated;
    ((String)(0x69a7d0u + delta))(&key, text);
    void *localization = (void *)FIELD(0x471eea0u + delta, 0);
    if (localize && localization) {
        ((Localize)(0x9bd130u + delta))(localization, &translated, &key);
        ((Set)(0xb8e900u + delta))(label, &translated);
        ((Destroy)(0x69a720u + delta))(&translated);
    } else {
        ((Set)(0xb8e900u + delta))(label, &key);
    }
    ((Destroy)(0x69a720u + delta))(&key);
}

BOSS_UI static int target_catalog(uint32_t delta, TargetPicker *p) {
    if (!p->catalog) {
        typedef void *(STDCALL *Allocate)(void *, uint32_t, uint32_t, uint32_t);
        p->catalog = (*(Allocate *)(0x036ef224u + delta))(0, sizeof(*p->catalog), 0x3000, 4);
        if (!p->catalog) return 0;
    }
    if (p->catalog->magic != PICKER_MAGIC &&
            !read_setting(delta, "..\\documents\\solbreaker-targets.bin",
                          p->catalog, sizeof(*p->catalog)))
        p->catalog->magic = 0;
    return p->catalog->magic == PICKER_MAGIC && p->catalog->count <= PICKER_RECORDS
        && p->catalog->modules_count <= PICKER_MODULES;
}

__attribute__((section(".late_sandbox"), noinline))
static void bind_stats(uint32_t delta, Sandbox *s, void *menu) {
    TargetPicker *p = &s->picker;
    p->stats = find_control(menu, "SolTankStats", delta);
    boss_visible(p->stats, 0, delta);
    if (!p->stats || !p->hub) return;
    boss_visible(find_control(p->hub, "Middle/VerticalBox/Stats", delta),
                 (s->allowed_keys & TANK_STATS) && !s->temporal_end, delta);
    static const char paths[TANK_STAT_COUNT][2][40] BOSS_DATA = {
        {"Middle/VerticalBox/Boost", "Middle/VerticalBox/BoostAmount"},
        {"Middle/VerticalBox/Reload", "Middle/VerticalBox/ReloadAmount"},
        {"Middle/VerticalBox/Aim", "Middle/VerticalBox/AimAmount"},
        {"Middle/VerticalBox/Accuracy", "Middle/VerticalBox/AccuracyAmount"},
        {"Middle/VerticalBox/Turret", "Middle/VerticalBox/TurretAmount"},
        {"Middle/VerticalBox/Hull", "Middle/VerticalBox/HullAmount"},
        {"Middle/VerticalBox/Damage", "Middle/VerticalBox/DamageAmount"},
        {"Middle/VerticalBox/Health", "Middle/VerticalBox/HealthAmount"},
        {"Middle/VerticalBox/Brake", "Middle/VerticalBox/BrakeAmount"},
        {"Middle/VerticalBox/Reverse", "Middle/VerticalBox/ReverseAmount"},
        {"Middle/VerticalBox/Grip", "Middle/VerticalBox/GripAmount"},
    };
    for (unsigned i = 0; i < TANK_STAT_COUNT; ++i) {
        void *slider = find_control(p->stats, paths[i][0], delta);
        p->stat_slider[i] = slider;
        p->stat_amount[i] = find_control(p->stats, paths[i][1], delta);
        if (!slider) continue;
        *(float *)((uintptr_t)slider + 0x15c) = (float)s->stats[i];
        ((void (THISCALL *)(void *))(0xb85e10u + delta))(slider);
        if (p->stat_amount[i]) update_blur_label(p->stat_amount[i], s->stats[i], delta);
    }
}

__attribute__((noinline))
static int stats_slider(uint32_t delta, Sandbox *s, void *slider, unsigned phase) {
    if (!s || !s->picker.menu) return 0;
    unsigned i = 0;
    while (i < TANK_STAT_COUNT && s->picker.stat_slider[i] != slider) ++i;
    if (i == TANK_STAT_COUNT) return 0;
    float value = *(float *)((uintptr_t)slider + 0x15c);
    if (value >= 0 && value <= 100) {
        unsigned percent = (unsigned)(value+.5f);
        if (phase == 3 && percent != s->stats[i] &&
                send_control_value(delta,s,TANK_STATS,(Vector){0,0,0},(i<<8)|percent))
            s->stats[i] = percent;
        void *label = s->picker.stat_amount[i];
        if (label) update_blur_label(label,phase == 3 ? s->stats[i] : percent,delta);
    }
    return 1;
}

BOSS_UI static int bind_picker_controls(uint32_t delta, TargetPicker *p) {
    if (!p->panel) return 0;
    p->search = find_control(p->panel, "Middle/VerticalBox/Search/Query", delta);
    if (!p->search) return 0;
    p->filter[0] = find_control(p->panel, "Middle/VerticalBox/Nation/Caption", delta);
    p->filter[1] = find_control(p->panel, "Middle/VerticalBox/Tier/Caption", delta);
    p->filter[2] = find_control(p->panel, "Middle/VerticalBox/Class/Caption", delta);
    p->marker[0] = find_control(p->panel, "Middle/VerticalBox/NationActive", delta);
    p->marker[1] = find_control(p->panel, "Middle/VerticalBox/TierActive", delta);
    p->marker[2] = find_control(p->panel, "Middle/VerticalBox/ClassActive", delta);
    p->status = find_control(p->panel, "Middle/VerticalBox/Status", delta);
    p->hint = find_control(p->panel, "Middle/VerticalBox/Search/Hint", delta);
    p->previous = find_control(p->panel, "Middle/VerticalBox/Prev", delta);
    p->next = find_control(p->panel, "Middle/VerticalBox/Next", delta);
    char path[] = "Middle/VerticalBox/Row0";
    for (unsigned index = 0; index < PICKER_ROWS; ++index) {
        path[sizeof(path)-2] = '0' + index;
        p->row[index] = find_control(p->panel, path, delta);
    }
    if (p->modules_panel) {
        void *list = find_control(p->modules_panel, "Middle/VerticalBox", delta);
        p->module_vehicle = find_control(list, "Vehicle", delta);
        char branch[] = "Branch0", name[] = "Card0";
        for (unsigned slot = 0; slot < PICKER_SLOTS; ++slot) {
            branch[6] = '0' + slot;
            void *row = find_control(list, branch, delta);
            for (unsigned column = 0; column < MODULE_COLUMNS; ++column) {
                name[4] = '0' + column;
                p->module_card[slot][column] = find_control(row, name, delta);
            }
        }
    }
    return 1;
}

BOSS_UI static void bind_target_picker(uint32_t delta, Sandbox *s, void *menu) {
    TargetPicker *p = &s->picker;
    void *button = find_control(menu, "menuDialog/**/SolTargetButton", delta);
    p->panel = find_control(menu, "SolTargetPicker", delta);
    boss_visible(p->panel, 0, delta);
    p->modules_panel = find_control(menu, "SolTargetModules", delta);
    boss_visible(p->modules_panel, 0, delta);
    p->hub = find_control(menu, "SolSandboxHub", delta);
    p->ai = find_control(menu, "SolTargetAI", delta);
    p->target_menu = find_control(menu, "SolTargetMenu", delta);
    boss_visible(p->target_menu, 0, delta);
    if (p->target_menu)
        boss_visible(find_control(p->target_menu, "Middle/VerticalBox/Choose", delta),
                     !!(s->allowed_keys & PICK_TARGET), delta);
    bind_stats(delta,s,menu);
    boss_visible(p->hub, 0, delta);
    boss_visible(p->ai, 0, delta);
    p->module_entry = 0;
    p->stock = find_control(menu, "menuDialog", delta);
    boss_visible(p->stock, 1, delta);
    p->menu = 0;
    p->open = 0;
    int available = p->hub && (s->allowed_keys & (PICK_TARGET | TARGET_AI | TOGGLE_MAP_BORDER | TANK_STATS | PRACTICE_MENU));
    boss_visible(button, available, delta);
    if (!available) return;
    boss_visible(find_control(p->hub, "Middle/VerticalBox/Targets", delta), !!(s->allowed_keys & (PICK_TARGET | PRACTICE_MENU)), delta);
    boss_visible(find_control(p->hub, "Middle/VerticalBox/AI", delta), !!(s->allowed_keys & TARGET_AI), delta);
    boss_visible(find_control(p->hub, "Middle/VerticalBox/Borders", delta), !!(s->allowed_keys & TOGGLE_MAP_BORDER), delta);
    p->command = find_control(menu, "SolTargetCommand", delta);
    if (!p->command || !p->stock || !bind_picker_controls(delta, p)) return;
    p->last = picker_value(p->command, delta);
    p->menu = menu;
}

BOSS_UI static int target_query(uint32_t delta, TargetPicker *p) {
    typedef NativeString *(THISCALL *Get)(void *, NativeString *);
    typedef void (THISCALL *Destroy)(NativeString *);
    NativeString value;
    ((Get)(0xb7b0f0u + delta))(p->search, &value);
    const uint8_t *input = (const uint8_t *)room_text(&value);
    char folded[sizeof(p->query)];
    unsigned length = value.size;
    if (length >= sizeof(folded)) length = sizeof(folded)-1;
    for (unsigned i = 0; i < length; ++i) {
        uint8_t ch = input[i];
        if (ch == 0xd0 && i+1 < length) {
            uint8_t next = input[i+1];
            if (next >= 0x90 && next <= 0x9f) {
                folded[i++] = 0xd0;
                ch = next + 0x20;
            } else if (next >= 0xa0 && next <= 0xaf) {
                folded[i++] = 0xd1;
                ch = next - 0x20;
            } else if (next == 0x81) {
                folded[i++] = 0xd1;
                ch = 0x91;
            }
        }
        folded[i] = ch >= 'A' && ch <= 'Z' ? ch + 32 : ch;
    }
    folded[length] = 0;
    int changed = 0;
    for (unsigned i = 0; i <= length; ++i) {
        if (p->query[i] != folded[i]) changed = 1;
        p->query[i] = folded[i];
    }
    ((Destroy)(0x69a720u + delta))(&value);
    return changed;
}

BOSS_UI static int target_matches(const char *text, const char *query) {
    if (!*query) return 1;
    for (; *text; ++text) {
        const char *candidate = text, *match = query;
        while (*match && *candidate == *match) {
            ++candidate;
            ++match;
        }
        if (!*match) return 1;
    }
    return 0;
}

BOSS_UI static void target_collect(TargetPicker *p) {
    p->count = p->page = 0;
    if (!p->catalog || p->catalog->magic != PICKER_MAGIC || p->catalog->count > PICKER_RECORDS)
        return;
    for (unsigned i = 0; i < p->catalog->count; ++i) {
        PickerEntry *row = &p->catalog->entry[i];
        if (row->id && !row->key[sizeof(row->key)-1] && !row->search[sizeof(row->search)-1] &&
                (!p->selected[0] || p->selected[0] == row->nation) &&
                (!p->selected[1] || p->selected[1] == row->tier) &&
                (!p->selected[2] || p->selected[2] == row->kind) &&
                target_matches(row->search, p->query))
            p->matches[p->count++] = i;
    }
}

BOSS_UI static void target_icons(uint32_t delta, void *button, const PickerEntry *entry) {
    char flag[] = "Flag0", type[] = "Type0";
    for (unsigned i = 1; i <= 9; ++i) {
        flag[4] = '0'+i;
        boss_visible(find_control(button, flag, delta), entry->nation == i, delta);
    }
    for (unsigned i = 1; i <= 4; ++i) {
        type[4] = '0'+i;
        boss_visible(find_control(button, type, delta), entry->kind == i, delta);
    }
}

BOSS_UI static void target_render(uint32_t delta, TargetPicker *p) {
    static const char tiers[11][5] BOSS_DATA = {
        "", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    };
    char nation[] = "Local/SbPickNation0", kind[] = "Local/SbPickClass0";
    nation[sizeof(nation)-2] += p->selected[0];
    kind[sizeof(kind)-2] += p->selected[2];
    target_text(delta, p->filter[0], nation, 1);
    target_text(delta, p->filter[1], p->selected[1] ? tiers[p->selected[1]] : "Local/SbPickTier",
                p->selected[1] == 0);
    target_text(delta, p->filter[2], kind, 1);
    for (unsigned i = 0; i < 3; ++i)
        boss_visible(p->marker[i], p->selected[i] != 0, delta);
    boss_visible(p->hint, !p->query[0], delta);
    unsigned start = p->page*PICKER_ROWS;
    for (unsigned i = 0; i < PICKER_ROWS; ++i) {
        void *button = p->row[i];
        if (!button) continue;
        boss_visible(button, start+i < p->count, delta);
        if (start+i < p->count) {
            const PickerEntry *entry = &p->catalog->entry[p->matches[start+i]];
            target_text(delta, find_control(button, "Caption", delta), entry->key, entry->key[0] == '#');
            target_text(delta, find_control(button, "Rank", delta),
                        entry->tier <= 10 ? tiers[entry->tier] : "", 0);
            target_icons(delta, button, entry);
        }
    }
    boss_visible(p->previous, p->page != 0, delta);
    boss_visible(p->next, start+PICKER_ROWS < p->count, delta);
    const char *empty = (p->catalog && p->catalog->magic == PICKER_MAGIC)
        ? "Local/SbPickEmpty" : "Local/SbPickUnavailable";
    target_text(delta, p->status, p->count ? "Local/SbPickHow" : empty, 1);
}

BOSS_UI static unsigned target_modules(const TargetPicker *p, unsigned slot,
                                       const PickerModule **options) {
    const PickerEntry *entry = p->module_entry;
    unsigned count = 0, end = entry->modules_start + entry->modules_count;
    if (end > p->catalog->modules_count || end > PICKER_MODULES) return 0;
    for (unsigned i = entry->modules_start; i < end && count < MODULE_COLUMNS; ++i) {
        const PickerModule *option = p->catalog->module + i;
        if (option->slot != slot || (slot == 2 && option->turret != p->module_selected[1]))
            continue;
        if (options) options[count] = option;
        ++count;
    }
    return count;
}

BOSS_UI static void target_render_modules(uint32_t delta, const TargetPicker *p) {
    for (unsigned slot = 0; slot < PICKER_SLOTS; ++slot) {
        const PickerModule *options[MODULE_COLUMNS];
        unsigned count = target_modules(p, slot, options);
        for (unsigned column = 0; column < MODULE_COLUMNS; ++column) {
            void *card = p->module_card[slot][column];
            if (!card) continue;
            int shown = column < count || column == 0;
            boss_visible(card, shown, delta);
            if (!shown) continue;
            const PickerModule *option = column < count ? options[column] : 0;
            if (option && option->key[sizeof(option->key)-1]) option = 0;
            const char *key = option ? option->key : "Local/SbPickStock";
            target_text(delta, find_control(card, "Caption", delta), key, !option || key[0] == '#');
            int selected = column == p->module_selected[slot];
            boss_visible(find_control(card, "Selected", delta), selected, delta);
            if (slot == 1) boss_visible(find_control(card, "GunLink", delta), selected, delta);
        }
    }
}

BOSS_UI static int target_confirm(uint32_t delta, Sandbox *s, unsigned flags, unsigned selection) {
    uint32_t now = clock_ms(delta);
    if ((s->last_report && now - s->last_report < 55) ||
            !send_control_value(delta, s, flags, (Vector){0, 0, 0}, selection))
        return 0;
    s->last_report = now ? now : 1;
    TargetPicker *p = &s->picker;
    void *menu = p->menu;
    boss_visible(p->panel, 0, delta);
    boss_visible(p->modules_panel, 0, delta);
    boss_visible(p->ai, 0, delta);
    boss_visible(p->target_menu, 0, delta);
    boss_visible(p->stock, 1, delta);
    p->menu = 0;
    p->module_entry = 0;
    s->respawn_menu = s->respawn_value = s->swap_value = 0;
    typedef void (THISCALL *Close)(void *);
    ((Close)(0x19449c0u + delta))(menu);
    return 1;
}

BOSS_UI static void update_target_picker(uint32_t delta, Sandbox *s) {
    TargetPicker *p = &s->picker;
    if (!p->menu) return;
    if (battle_input_active(delta)) {
        p->menu = 0;
        return;
    }
    unsigned value = picker_value(p->command, delta);
    unsigned action = value != p->last ? value % 64 : 0;
    if (p->module_entry) {
        if (action == 16) {
            p->module_entry = 0;
            boss_visible(p->modules_panel, 0, delta);
            boss_visible(p->panel, 1, delta);
        } else if (action == 17) {
            unsigned selection = p->module_entry->id;
            for (unsigned slot = 0; slot < PICKER_SLOTS; ++slot)
                selection |= p->module_selected[slot] << (16 + 4 * slot);
            if (!target_confirm(delta, s, PICK_TARGET, selection)) return;
        } else if (action >= 18 && action < 18+MODULE_COLUMNS*PICKER_SLOTS) {
            unsigned slot = (action-18)/MODULE_COLUMNS, column = (action-18)%MODULE_COLUMNS;
            if (column != p->module_selected[slot] && column < target_modules(p, slot, 0)) {
                p->module_selected[slot] = column;
                if (slot == 1) p->module_selected[2] = 0;
                target_render_modules(delta, p);
            }
        }
        p->last = value;
        return;
    }
    if (action == 52 && !(s->allowed_keys & PRACTICE_MENU)) action = p->open ? 2 : 1;
    if (action == 1) {
        p->open = 1;
        boss_visible(p->target_menu, 0, delta);
        boss_visible(p->hub, 0, delta);
        boss_visible(p->panel, 1, delta);
        target_catalog(delta, p);
    } else if (action == 2) {
        p->open = 0;
        boss_visible(p->panel, 0, delta);
        boss_visible(p->ai, 0, delta);
        boss_visible(p->stats, 0, delta);
        boss_visible(p->target_menu, 0, delta);
        boss_visible(p->hub, 1, delta);
    } else if (action == 39) {
        boss_visible(p->hub, 0, delta);
        boss_visible(p->stock, 1, delta);
    } else if (action == 40) {
        boss_visible(p->stock, 0, delta);
        boss_visible(p->hub, 1, delta);
    } else if (action == 41 || action == 51) {
        boss_visible(p->hub, 0, delta);
        boss_visible(action == 41 ? p->ai : p->stats, 1, delta);
    } else if (action >= 42 && action < 50) {
        if (!target_confirm(delta, s, TARGET_AI, action - 42)) return;
    } else if (action == 50) {
        if (!target_confirm(delta, s, TOGGLE_MAP_BORDER, 0)) return;
    } else if (action == 52) {
        p->open = 0;
        boss_visible(p->panel, 0, delta);
        boss_visible(p->hub, 0, delta);
        boss_visible(p->target_menu, 1, delta);
    } else if (action >= 53 && action <= 58 && (s->allowed_keys & PRACTICE_MENU)) {
        if (!target_confirm(delta, s, PRACTICE_MENU, action - 53)) return;
    }
    if (p->open) {
        int changed = target_query(delta, p);
        static const unsigned sizes[3] BOSS_DATA = {10, 11, 5};
        if (action >= 5 && action <= 7) {
            unsigned filter = action-5;
            p->selected[filter] = (p->selected[filter]+1) % sizes[filter];
            changed = 1;
        }
        if (changed || action == 1) target_collect(p);
        if (action == 3 && p->page) --p->page;
        if (action == 4 && (p->page+1)*PICKER_ROWS < p->count) ++p->page;
        if (changed || (action && action <= 7)) target_render(delta, p);
        if (action >= 8 && action < 8+PICKER_ROWS) {
            unsigned index = p->page*PICKER_ROWS + action-8;
            if (index < p->count) {
                const PickerEntry *entry = &p->catalog->entry[p->matches[index]];
                if (p->modules_panel) {
                    p->module_entry = entry;
                    __builtin_memset(p->module_selected, 0, sizeof(p->module_selected));
                    boss_visible(p->panel, 0, delta);
                    boss_visible(p->modules_panel, 1, delta);
                    target_text(delta, p->module_vehicle, entry->key, entry->key[0] == '#');
                    target_render_modules(delta, p);
                } else if (!target_confirm(delta, s, PICK_TARGET, entry->id)) {
                    return;
                }
            }
        }
    }
    p->last = value;
}

/* Both team controls own an ordered map of vehicle IDs to row controls.
 * Resolve the current row on each use. A native update may replace a row. */
BOSS_UI static uint32_t team_row_map(uint32_t delta, void *control) {
    if (!control) return 0;
    uint32_t vtable = FIELD(control, 0)-delta;
    if (vtable == 0x3848eb4u) return FIELD(control, 0x168);  /* UITeamPanel */
    if (vtable == 0x384c5e0u) return FIELD(control, 0x148);  /* ShortTeamPanel */
    return 0;
}

BOSS_UI static void *temporal_team_row(uint32_t delta, void *control, uint32_t entity) {
    uint32_t head = team_row_map(delta, control);
    for (uint32_t node = head ? FIELD(head, 4) : 0; node && node != head;) {
        uint32_t id = FIELD(node, 0x10);
        if (id == entity) return (void *)FIELD(node, 0x14);
        node = FIELD(node, entity < id ? 0 : 8);
    }
    return 0;
}

BOSS_UI static void bind_team_panels(uint32_t delta, Sandbox *s) {
    void *root = s->battle_controls_root;
    if (!root) return;
    void *hud = find_control(root, "BattleHUD/HUDLayer", delta);
    if (!hud) return;
    s->team_panels[0].control = find_control(hud, "AllyTeamPanelContainer/AllyTeamPanel", delta);
    s->team_panels[1].control = find_control(hud, "EnemyTeamPanelContainer/EnemyTeamPanel", delta);
    void *score = find_control(hud,
        "PanelsContainerLayoutProtector/PanelsContainer/Top/Content/BattleStatusControl/ScoreBg", delta);
    if (score) {
        s->team_panels[2].control = find_control(score, "left/allyPanel", delta);
        s->team_panels[3].control = find_control(score, "right/enemyPanel", delta);
    }
}

/* Polygon is a solo mode. Its enemy panels contain only practice targets. */
BOSS_UI static void hide_replaced_rows(uint32_t delta, uint32_t head, uint32_t node,
                                      const uint32_t *ids, const uint32_t *end, unsigned depth) {
    if (!node || node == head || !depth) return;
    const uint32_t *id = ids;
    while (id != end && *id != FIELD(node, 0x10)) ++id;
    void *row = (void *)FIELD(node, 0x14);
    if (id == end && row && (*(uint8_t *)((uintptr_t)row + 0x4c) & 2u))
        boss_visible(row, 0, delta);
    hide_replaced_rows(delta, head, FIELD(node, 0), ids, end, depth-1);
    hide_replaced_rows(delta, head, FIELD(node, 8), ids, end, depth-1);
}

BOSS_UI static void practice_roster(uint32_t delta, Sandbox *s, const uint32_t *ids, const uint32_t *end) {
    if (!s->team_panels[1].control || !s->team_panels[3].control)
        bind_team_panels(delta, s);
    for (unsigned i = 1; i < 4; i += 2) {
        uint32_t head = team_row_map(delta, s->team_panels[i].control);
        if (head) hide_replaced_rows(delta, head, FIELD(head, 4), ids, end, 32);
    }
}

BOSS_UI static void temporal_roster(uint32_t delta, Sandbox *s, uint32_t entity) {
    uint32_t previous = s->peer_hidden_entity;
    s->peer_hidden_entity = entity;
    if (entity && (!s->team_panels[0].control || !s->team_panels[1].control
            || !s->team_panels[2].control || !s->team_panels[3].control))
        bind_team_panels(delta, s);
    for (unsigned i = 0; i < sizeof(s->team_panels)/sizeof(*s->team_panels); ++i) {
        TeamPanel *panel = &s->team_panels[i];
        if (previous != entity && panel->hidden_row) {
            void *old_row = temporal_team_row(delta, panel->control, previous);
            if (old_row == panel->hidden_row) boss_visible(old_row, 1, delta);
            panel->hidden_row = 0;
        }
        void *row = entity ? temporal_team_row(delta, panel->control, entity) : 0;
        if (row != panel->hidden_row) panel->hidden_row = 0;
        if (row && (*(uint8_t *)((uintptr_t)row + 0x4c) & 2u)) {
            boss_visible(row, 0, delta);
            panel->hidden_row = row;
        }
    }
}

BOSS_UI static void bind_battle_controls(uint32_t delta, Sandbox *s, void *root) {
    if (s->battle_controls_root == root) return;
    s->battle_controls_root = root;
    void *abilities = find_control(root, "SolRangeAbilities", delta);
    for (unsigned slot = 0; slot < 3; slot++) {
        char name[] = "Slot0";
        name[4] += slot;
        AbilitySlot *item = &s->abilities[slot];
        item->row = abilities ? find_control(abilities, name, delta) : 0;
        item->time = item->row ? find_control(item->row, "Time", delta) : 0;
        item->dim = item->row ? find_control(item->row, "Dim", delta) : 0;
        item->key = item->row ? find_control(item->row, "Key", delta) : 0;
        item->shown = UINT32_MAX;
    }
    s->aim_time_label = find_control(root, "SolAimTime", delta);
    s->aim_tenths = 0;
    s->temporal_panel = find_control(root, "SolTimeHUD", delta);
    s->temporal_label = s->temporal_panel ? find_control(s->temporal_panel, "SolTimeValue", delta) : 0;
    s->temporal_hud = 0;
    boss_visible(s->temporal_panel, 0, delta);
    s->recovery_panel = find_control(root, "SolAutoRecovery", delta);
    s->recovery_label = s->recovery_panel ? find_control(s->recovery_panel, "Time", delta) : 0;
    for (unsigned i = 0; i < 3; ++i) {
        char name[] = "Ring1";
        name[4] += i;
        s->recovery_rings[i] = s->recovery_panel ? find_control(s->recovery_panel, name, delta) : 0;
    }
    update_recovery_warning(delta, s);
    s->peer_root = root;
    s->peer_clock = find_control(root, "SolTimeClock", delta);
    s->peer_hand = s->peer_hour = s->peer_opacity = 0;
    if (s->peer_clock) {
        s->peer_hand = find_control(s->peer_clock, "Hand", delta);
        s->peer_hour = find_control(s->peer_clock, "Hour", delta);
        const char *type = "UIOpacityComponent";
        NativeString component_name = {{0}, 18, 18};
        __builtin_memcpy(component_name.text, &type, sizeof(type));
        typedef void *(THISCALL *Component)(void *, const NativeString *, uint32_t);
        void *opacity = ((Component)(0xce0530u + delta))(s->peer_clock, &component_name, 0);
        if (opacity && FIELD(opacity, 0) == 0x3781ee4u+delta) s->peer_opacity = opacity;
        boss_visible(s->peer_clock, 0, delta);
    }
    __builtin_memset(s->team_panels, 0, sizeof(s->team_panels));
    if (s->peer_hidden_entity) temporal_roster(delta, s, s->peer_hidden_entity);
    s->role_picker = find_control(root, boss_picker_path, delta);
    s->role_value = s->role_picker ? find_control(s->role_picker, boss_role_path, delta) : 0;
    s->role_sent = s->role_value ? picker_value(s->role_value, delta) : 0;
    boss_visible(s->role_picker, s->role_state == 2, delta);
}

/* Bind early for the role picker during battle loading. */
BOSS_UI void *THISCALL boss_screen_root(void *package, void *result, const NativeString *name) {
    typedef void *(THISCALL *Get)(void *, void *, const NativeString *);
    uint32_t delta = sandbox_delta();
    void *returned = ((Get)(0xcddf80u + delta))(package, result, name);
    Sandbox *s = state(delta, 1);
    void *root = (void *)FIELD(result, 0);
    if (s && root) bind_battle_controls(delta, s, root);
    return returned;
}

/* Track the native read-only cards to give the interactive cards their own
 * panel during training selection. The regular countdown can use them again. */
BOSS_UI void *THISCALL boss_stock_panel(void *view, void *result, const NativeString *name) {
    typedef void *(THISCALL *Get)(void *, void *, const NativeString *);
    uint32_t delta = sandbox_delta();
    void *returned = ((Get)(0x1c61300u + delta))(view, result, name);
    Sandbox *s = state(delta, 1);
    void *root = (void *)FIELD(result, 0);
    if (s && root && room_prefix(room_text(name), stock_boss_path)) {
        s->stock_boss_panel = root;
        if (s->role_state == 2) boss_visible(root, 0, delta);
    }
    return returned;
}

BOSS_UI static void boss_picker_message(uint32_t delta, const uint32_t *message) {
    Sandbox *s = state(delta, 1);
    if (!s || (s->thread && s->thread != thread_id()) || message[3] > 2) return;
    if (!s->thread) s->thread = thread_id();
    // Countdown can arrive before the local gun controller binds the Avatar.
    uint32_t token = message[3] ? message[2] : 0;
    if (s->role_token != token || s->role_state != message[3]) {
        s->role_token = token;
        s->role_state = message[3];
        s->role_keys = 0x3e; /* Require a fresh press after the picker opens. */
        s->role_sent = s->role_value ? picker_value(s->role_value, delta) : 0;
    }
    boss_visible(s->role_picker, s->role_state == 2, delta);
    boss_visible(s->stock_boss_panel, s->role_state != 2, delta);
}

BOSS_UI static void boss_respawn_menu(void *menu, uint32_t delta) {
    Sandbox *s = state(delta, 0);
    if (!s) return;
    void *button = find_control(menu, respawn_button_path, delta);
    if (!button) return;
    s->respawn_menu = menu;
    s->respawn_value = find_control(menu, respawn_value_path, delta);
    s->respawn_sent = s->respawn_value ? picker_value(s->respawn_value, delta) : 0;
    void *swap_button = find_control(menu, swap_button_path, delta);
    s->swap_value = swap_button && s->swap_token ? find_control(menu, swap_value_path, delta) : 0;
    s->swap_sent = s->swap_value ? picker_value(s->swap_value, delta) : 0;
    boss_visible(swap_button, s->swap_value != 0, delta);
    boss_visible(button, s->role_state == 1, delta);
    bind_target_picker(delta, s, menu);
}

BOSS_UI static void boss_picker_update(uint32_t delta, Sandbox *s) {
    typedef int16_t (STDCALL *Key)(int);
    update_target_picker(delta, s);
    if (s->swap_value && picker_value(s->swap_value, delta) != s->swap_sent &&
            send_control_value(delta, s, CHANGE_TANK, (Vector){0, 0, 0}, 0)) {
        typedef void (THISCALL *Close)(void *);
        ((Close)(0x19449c0u + delta))(s->respawn_menu);
        s->swap_value = s->respawn_value = s->respawn_menu = 0;
        return;
    }
    if (s->role_state == 2 && foreground(delta, s)) {
        uint32_t value = s->role_value ? picker_value(s->role_value, delta) : 0;
        uint32_t choice = value != s->role_sent ? value % 10 : 0;
        uint32_t keys = 0;
        Key key = (Key)s->key_state;
        for (uint32_t number = 1; key && number <= 5; ++number) {
            uint32_t bit = 1u << number;
            if (key('0' + number) < 0) {
                keys |= bit;
                if (!choice && !(s->role_keys & bit)) choice = number;
            }
        }
        uint32_t encoded = choice;
        if (choice > 1 && s->role_picker) {
            void *field = find_control(s->role_picker, boss_level_path, delta);
            uint32_t level = field ? picker_value(field, delta) : 1;
            if (level >= 1 && level <= 5) encoded += level * 5;
        }
        if (!choice || (choice <= 5 &&
                send_control_value(delta, s, BOSS_PICK_ROLE, (Vector){0, 0, 0}, encoded))) {
            s->role_keys = keys;
            s->role_sent = value;
        }
    } else if (s->role_state == 1 && s->respawn_value) {
        uint32_t value = picker_value(s->respawn_value, delta);
        if (value != s->respawn_sent &&
                send_control_value(delta, s, BOSS_RESPAWN, (Vector){0, 0, 0}, 0)) {
            s->respawn_sent = value;
            typedef void (THISCALL *Close)(void *);
            ((Close)(0x19449c0u + delta))(s->respawn_menu);
            s->swap_value = s->respawn_menu = s->respawn_value = 0;
        }
    }
}

__attribute__((section(".late_sandbox"), noinline))
static void update_fov_label(void *label, uint8_t fov, uint32_t delta) {
    typedef void (THISCALL *Set)(void *, const NativeString *);
    NativeString text = {{0}, 0, 15};
    text.text[text.size++] = ' ';
    if (fov == 100) text.text[text.size++] = '1';
    text.text[text.size++] = '0' + (fov / 10) % 10;
    text.text[text.size++] = '0' + fov % 10;
    text.text[text.size++] = (char)0xc2;
    text.text[text.size++] = (char)0xb0;
    ((Set)(0xb8e900u + delta))(label, &text);
}

void *THISCALL sandbox_graphics_page(void *package, const NativeString *name) {
    typedef void *(THISCALL *Get)(void *, const NativeString *);
    typedef void (THISCALL *Refresh)(void *);
    uint32_t delta = sandbox_delta();
    void *page = ((Get)(0xce3490u + delta))(package, name);
    if (!page) return page;
    void *slider = find_control(page,
        "ScrollView/scrollContainerControl/SolbreakerMotionBlur/Background/Editor/SolbreakerBlurSlider", delta);
    void *label = slider ? find_control(slider, "SolbreakerBlurPercent", delta) : 0;
    Sandbox *s = state(delta, 1);
    if (slider && label && s) {
        s->settings_thread = thread_id();
        *(float *)((uintptr_t)slider + 0x15c) = (float)s->blur_percent;
        ((Refresh)(0xb85e10u + delta))(slider);
        update_blur_label(label, s->blur_percent, delta);
    }
    return page;
}

__attribute__((section(".late_sandbox"), noinline))
void *THISCALL sandbox_controls_page(void *package, const NativeString *name) {
    typedef void *(THISCALL *Get)(void *, const NativeString *);
    typedef void (THISCALL *Refresh)(void *);
    uint32_t delta = sandbox_delta();
    void *page = ((Get)(0xce3490u + delta))(package, name);
    if (!page) return page;
    void *row = find_control(page, "ScrollView/scrollContainerControl/SolbreakerFieldOfView", delta);
    void *slider = row ? find_control(row, "Slider", delta) : 0;
    void *label = row ? find_control(row, "ValueAndCaption/Value", delta) : 0;
    Sandbox *s = state(delta, 1);
    if (slider && label && s) {
        s->settings_thread = thread_id();
        *(float *)((uintptr_t)slider + 0x15c) = (float)s->fov;
        ((Refresh)(0xb85e10u + delta))(slider);
        update_fov_label(label, s->fov, delta);
    }
    return page;
}

__attribute__((section(".late_sandbox"), noinline))
static int fov_input(uint32_t delta, Sandbox *s, void *slider, unsigned phase) {
    if (!find_control(slider, "SolbreakerFovValue", delta)) return 0;
    void *row = (void *)FIELD(slider, 0x38);
    void *label = row ? find_control(row, "ValueAndCaption/Value", delta) : 0;
    if (!label) return 1;
    float value = *(float *)((uintptr_t)slider + 0x15c);
    if (!(value >= 40 && value <= 100)) return 1;
    uint8_t fov = (uint8_t)(value+.5f);
    if (phase == 3) {
        if (write_setting(delta, fov_path, fov_temp, &fov, sizeof(fov))) s->saved_fov = fov;
        else fov = s->saved_fov;
    }
    s->fov = fov;
    update_fov_label(label, fov, delta);
    return 1;
}

static void save_mouse_sensitivity(uint32_t delta, void *slider);

void THISCALL sandbox_blur_slider(void *slider, void *event) {
    typedef void (THISCALL *Input)(void *, void *);
    uint32_t delta = sandbox_delta();
    ((Input)(0xb7dd20u + delta))(slider, event);
    if (!event || (FIELD(event, 0x38) != 1 && FIELD(event, 0x38) != 2 &&
                   FIELD(event, 0x38) != 3)) return;
    if (FIELD(event, 0x38) == 3) save_mouse_sensitivity(delta, slider);
    if (setup_slider(delta, slider, FIELD(event, 0x38))) return;
    Sandbox *s = state(delta, 0);
    if (stats_slider(delta,s,slider,FIELD(event, 0x38))) return;
    if (!s || s->settings_thread != thread_id() || fov_input(delta, s, slider, FIELD(event, 0x38))) return;
    void *label = find_control(slider, "SolbreakerBlurPercent", delta);
    if (!label) return;
    float value = *(float *)((uintptr_t)slider + 0x15c);
    if (!(value >= 0 && value <= 100)) return;
    uint8_t percent = (uint8_t)(value+.5f);
    if (FIELD(event, 0x38) == 3 &&
        !write_setting(delta, blur_path, blur_temp, &percent, sizeof(percent)))
        percent = (uint8_t)s->blur_percent;
    if (FIELD(event, 0x38) == 3)
        s->blur_percent = percent;
    update_blur_label(label, percent, delta);
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_save_description(uint32_t delta, void *view, NativeString *description) {
    Sandbox *s = state(delta, 0);
    if (!s || !s->setup.identity || description->size > 250) return;
    void *identity = find_control((void *)FIELD(view, 4), "**/SolSetupIdentity", delta);
    if (identity != s->setup.identity) return;
    static const char marker[] BOSS_DATA = "/custom1/";
    static const char hex[] BOSS_DATA = "0123456789abcdef";
    const char *original = room_text(description);
    char output[250+sizeof(marker)+2*sizeof(SetupProfile)];
    uint32_t length = 0;
    for (; length < description->size; ++length) output[length] = original[length];
    for (unsigned i = 0; i < sizeof(marker)-1; ++i) output[length++] = marker[i];
    const uint8_t *bytes = (const uint8_t *)&s->setup.profile;
    for (unsigned i = 0; i < sizeof(SetupProfile); ++i) {
        output[length++] = hex[bytes[i] >> 4];
        output[length++] = hex[bytes[i] & 15];
    }
    typedef void (THISCALL *Assign)(NativeString *, const char *, uint32_t);
    ((Assign)(0x6a4ef0u + delta))(description, output, length);

}

static const char room_lineup_marker[] BOSS_DATA = "[lineup ";
static const char setup_hex_digits[] BOSS_DATA = "0123456789abcdef";

__attribute__((section(".late_sandbox"), noinline))
static const char *setup_read_room_tag(const char *text, RoomLineup *lineup) {
    if (!room_prefix(text, room_lineup_marker)) return text;
    const char *cursor = text + sizeof(room_lineup_marker)-1;
    RoomLineup decoded;
    uint8_t *bytes = (uint8_t *)&decoded;
    for (unsigned i = 0; i < sizeof(decoded); ++i) {
        unsigned value = 0;
        for (unsigned digit = 0; digit < 2; ++digit) {
            unsigned j = 0;
            while (j < 16 && setup_hex_digits[j] != *cursor) ++j;
            if (j == 16) return text;
            value = 16*value + j;
            ++cursor;
        }
        bytes[i] = value;
    }
    if (decoded.custom > 1 || *cursor != ']') return text;
    if (lineup) *lineup = decoded;
    ++cursor;
    return *cursor == ' ' ? cursor+1 : cursor;
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_load_room(uint32_t delta, void *view, RoomLineup lineup, int bots1, int bots2) {
    void *form = (void *)FIELD(view, 4);
    void *identity = form ? find_control(form, "**/SolSetupIdentity", delta) : 0;
    if (!identity) return;
    Sandbox *s = state(delta, 1);
    if (!s) return;
    SetupEditor *e = &s->setup;
    e->room_identity = identity;
    if (bots1 >= 0) {
        lineup.custom = 0;
        for (unsigned i = 0; i < 14; ++i) {
            unsigned count = i < 7 ? (unsigned)bots1 : (unsigned)bots2;
            lineup.slots[i] = i%7 < count ? 0 : UINT32_MAX;
        }
    }
    e->lineup = lineup;
}

__attribute__((section(".late_sandbox"), noinline))
static unsigned setup_room_tag(uint32_t delta, void *view, char *output) {
    Sandbox *s = state(delta, 0);
    void *form = (void *)FIELD(view, 4);
    if (!s || !s->setup.room_identity || !form ||
        find_control(form, "**/SolSetupIdentity", delta) != s->setup.room_identity) return 0;
    unsigned length = 0;
    for (; length < sizeof(room_lineup_marker)-1; ++length)
        output[length] = room_lineup_marker[length];
    const uint8_t *bytes = (const uint8_t *)&s->setup.lineup;
    for (unsigned i = 0; i < sizeof(RoomLineup); ++i) {
        output[length++] = setup_hex_digits[bytes[i] >> 4];
        output[length++] = setup_hex_digits[bytes[i] & 15];
    }
    output[length++] = ']';
    output[length++] = ' ';
    return length;
}

static unsigned setup_slot_index(const SetupEditor *e, unsigned index) {
    return !e->room ? index : index == 13 ? 0 : index+1;
}

static uint32_t setup_get_slot(const SetupEditor *e, unsigned index) {
    return e->room ? e->lineup.slots[index] : e->profile.slots[index];
}

static void setup_set_slot(SetupEditor *e, unsigned index, uint32_t value) {
    if (e->room) e->lineup.slots[index] = value;
    else e->profile.slots[index] = value;
}

__attribute__((section(".late_sandbox"), noinline))
static void *setup_body(uint32_t delta, void *form, const char *name) {
    void *panel = find_control(form, name, delta);
    return panel ? find_control(panel, "Middle/VerticalBox", delta) : 0;
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_panel(uint32_t delta, void *form, unsigned which) {
    static const char names[][20] BOSS_DATA = {"SolSetupRoster", "SolSetupSlot", "SolSetupPresets",
                                              "SolTargetPicker", "SolTargetModules"};
    for (unsigned i = 0; i < sizeof(names)/sizeof(*names); ++i)
        boss_visible(find_control(form, names[i], delta), i+1 == which, delta);
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_roster_labels(uint32_t delta, void *form, SetupEditor *editor) {
    void *body = setup_body(delta, form, "SolSetupRoster");
    if (!body) return;
    boss_visible(find_control(body, "AlliesSelected", delta), !editor->team, delta);
    boss_visible(find_control(body, "EnemiesSelected", delta), editor->team, delta);
    boss_visible(find_control(body, "YourTank", delta), !editor->team && !editor->room, delta);
    boss_visible(find_control(body, "FillAllies", delta), !editor->team, delta);
    boss_visible(find_control(body, "FillEnemies", delta), editor->team, delta);
    target_text(delta, find_control(body, "Allies/Caption", delta),
                editor->room ? "Local/SbSetupTeam1" : "Local/SbSetupAllies", 1);
    target_text(delta, find_control(body, "Enemies/Caption", delta),
                editor->room ? "Local/SbSetupTeam2" : "Local/SbSetupEnemies", 1);
    unsigned custom = editor->room ? editor->lineup.custom : editor->profile.custom;
    target_text(delta, find_control(body, "Mode/Caption", delta),
                custom ? "Local/SbSetupCustom" : "Local/SbSetupAuto", 1);
    TargetPicker *p = &editor->picker;
    int loaded = target_catalog(delta, p);
    char path[] = "Slot00";
    for (unsigned i = 0; i < 14; ++i) {
        path[4] = '0' + i/10;
        path[5] = '0' + i%10;
        void *button = find_control(body, path, delta);
        int show = ((i >= 6 && i < 13) == editor->team) && (i != 13 || editor->room);
        boss_visible(button, show, delta);
        if (!show || !button) continue;
        uint32_t value = setup_get_slot(editor, setup_slot_index(editor, i));
        const char *key = value == UINT32_MAX ? "Local/SbSetupEmpty" :
            value == 0 ? "Local/SbSetupAutomatic" : "Local/SbSetupRandom";
        if (loaded && (value & 15) == 1) {
            for (unsigned j = 0; j < p->catalog->count; ++j) {
                const PickerEntry *row = &p->catalog->entry[j];
                if (row->id == (value & 65535) && !row->key[sizeof(row->key)-1]) {
                    key = row->key;
                    break;
                }
            }
        }
        target_text(delta, find_control(button, "Caption", delta), key, 1);
    }
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_slot_labels(uint32_t delta, void *form, SetupEditor *editor) {
    void *body = setup_body(delta, form, "SolSetupSlot");
    if (!body) return;
    char kind[] = "Local/SbSetupClass0";
    kind[sizeof(kind)-2] += editor->kind;
    target_text(delta, find_control(body, "Class/Caption", delta), kind, 1);
    char number[] = "10";
    const unsigned value = editor->tier;
    if (value < 10) {
        number[0] = '0' + value;
        number[1] = 0;
    }
    target_text(delta, find_control(body, "Tier/Caption", delta),
                value ? number : "Local/SbSetupTierAny", !value);
}

static const char setup_stat_names[TANK_STAT_COUNT][10] BOSS_DATA = {
    "Boost", "Reload", "Aim", "Accuracy", "Turret", "Hull", "Damage", "Health", "Brake", "Reverse", "Grip"
};

__attribute__((section(".late_sandbox"), noinline))
static void *setup_stat_control(uint32_t delta, void *body, unsigned index, const char *suffix) {
    char name[16];
    unsigned n = 0;
    while (setup_stat_names[index][n]) {
        name[n] = setup_stat_names[index][n];
        ++n;
    }
    unsigned j = 0;
    do { name[n+j] = suffix[j]; } while (suffix[j++]);
    return find_control(body, name, delta);
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_preset_labels(uint32_t delta, void *form, SetupEditor *editor) {
    void *body = setup_body(delta, form, "SolSetupPresets");
    if (!body) return;
    unsigned selected = editor->preset;
    unsigned on = editor->profile.active == selected+1;
    target_text(delta, find_control(body, "State", delta),
                on ? "Local/SbSetupPresetOn" : "Local/SbSetupPresetOff", 1);
    target_text(delta, find_control(body, "Activate/Caption", delta),
                on ? "Local/SbSetupDeactivate" : "Local/SbSetupActivate", 1);
    target_text(delta, find_control(body, "Bots/Caption", delta),
                editor->profile.values[selected][TANK_STAT_COUNT] ?
                "Local/SbSetupBotsOn" : "Local/SbSetupBotsOff", 1);
    char path[] = "Preset0/Caption";
    for (unsigned i = 0; i < 4; ++i) {
        path[6] = '0'+i;
        char number[] = {'1'+i, 0, 0, 0};
        if (selected == i) {
            number[0] = '[';
            number[1] = '1'+i;
            number[2] = ']';
        }
        target_text(delta, find_control(body, path, delta), number, 0);
    }
    char group[] = "StatsGroup0";
    for (unsigned i = 0; i < 3; ++i) {
        group[sizeof(group)-2] = '0'+i;
        boss_visible(find_control(body, group, delta), i == editor->stat_group, delta);
    }
    static const uint8_t groups[TANK_STAT_COUNT] BOSS_DATA = {1, 0, 0, 0, 0, 1, 0, 2, 1, 1, 1};
    for (unsigned i = 0; i < TANK_STAT_COUNT; ++i) {
        int show = groups[i] == editor->stat_group;
        unsigned value = editor->profile.values[selected][i];
        void *slider = find_control(body, setup_stat_names[i], delta);
        boss_visible(slider, show, delta);
        if (show && slider) {
            *(float *)((uintptr_t)slider + 0x15c) = (float)value;
            ((void (THISCALL *)(void *))(0xb85e10u + delta))(slider);
        }
        void *label = setup_stat_control(delta, body, i, "Value");
        boss_visible(setup_stat_control(delta, body, i, "Label"), show, delta);
        boss_visible(label, show, delta);
        if (show && label) update_blur_label(label, value, delta);
    }
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_assign(uint32_t delta, void *form, SetupEditor *e, uint32_t value) {
    if (e->room) e->lineup.custom = 1;
    else e->profile.custom = 1;
    setup_set_slot(e, e->slot, value);
    setup_roster_labels(delta, form, e);
    setup_panel(delta, form, 1);
}

__attribute__((section(".late_sandbox"), noinline))
static void setup_catalog_event(uint32_t delta, void *form, SetupEditor *e, unsigned action) {
    TargetPicker *p = &e->picker;
    if (action == 52) {
        setup_panel(delta, form, 2);
        return;
    }
    if (p->module_entry && action >= 16 && action < 38) {
        if (action == 16) {
            p->module_entry = 0;
            setup_panel(delta, form, 4);
        } else if (action == 17) {
            uint32_t value = p->module_entry->id;
            for (unsigned slot = 0; slot < PICKER_SLOTS; ++slot)
                value |= p->module_selected[slot] << (16+4*slot);
            p->module_entry = 0;
            setup_assign(delta, form, e, value);
        } else {
            unsigned slot = (action-18)/MODULE_COLUMNS, column = (action-18)%MODULE_COLUMNS;
            if (column < target_modules(p, slot, 0)) {
                p->module_selected[slot] = column;
                if (slot == 1) p->module_selected[2] = 0;
                target_render_modules(delta, p);
            }
        }
        return;
    }
    if (!p->search || !target_catalog(delta, p)) return;
    int changed = target_query(delta, p);
    if (action >= 5 && action <= 7) {
        static const unsigned sizes[] BOSS_DATA = {10, 11, 5};
        unsigned i = action-5;
        p->selected[i] = (p->selected[i]+1) % sizes[i];
        changed = 1;
    }
    if (changed) target_collect(p);
    if (action == 3 && p->page) --p->page;
    if (action == 4 && (p->page+1)*PICKER_ROWS < p->count) ++p->page;
    if (action >= 8 && action < 16 && !changed) {
        unsigned index = p->page*PICKER_ROWS + action-8;
        if (index < p->count) {
            p->module_entry = &p->catalog->entry[p->matches[index]];
            __builtin_memset(p->module_selected, 0, sizeof(p->module_selected));
            target_text(delta, p->module_vehicle, p->module_entry->key, 1);
            target_render_modules(delta, p);
            setup_panel(delta, form, 5);
            return;
        }
    }
    target_render(delta, p);
}

__attribute__((section(".late_sandbox"), noinline))
static int setup_slider(uint32_t delta, void *slider, unsigned phase) {
    void *marker = find_control(slider, "SolSetupAction", delta);
    if (!marker) return 0;
    void *form = slider, *identity = 0;
    for (unsigned depth = 0; form && depth < 18 && !identity; ++depth) {
        form = (void *)FIELD(form, 0x38);
        if (form) identity = find_control(form, "SolSetupIdentity", delta);
    }
    if (!identity) return 1;
    Sandbox *s = state(delta, 1);
    if (!s) return 1;
    SetupEditor *e = &s->setup;
    unsigned action = picker_value(marker, delta);
    if (phase == 3 && (action == 101 || action == 103 || action == 106)) {
        e->room = action == 106;
        e->picker.module_entry = 0;
        if (e->room && e->room_identity != identity) {
            e->room_identity = identity;
            e->lineup.custom = 0;
            for (unsigned i = 0; i < 14; ++i) e->lineup.slots[i] = UINT32_MAX;
        }
    }
    if (!e->room && e->identity != identity) {
        e->identity = identity;
        __builtin_memset(&e->profile, 0, sizeof(e->profile));
        e->preset = e->profile.active ? e->profile.active-1 : 0;
        e->picker.module_entry = 0;
    }
    if (action >= 180 && action < 180+TANK_STAT_COUNT) {
        const unsigned index = action-180;
        float raw = *(float *)((uintptr_t)slider + 0x15c);
        if (raw >= 0 && raw <= 100) {
            unsigned value = (unsigned)(raw+.5f);
            if (phase == 3) e->profile.values[e->preset][index] = value;
            void *body = setup_body(delta, form, "SolSetupPresets");
            void *label = body ? setup_stat_control(delta, body, index, "Value") : 0;
            if (label) update_blur_label(label, value, delta);
        }
        return 1;
    }
    if (phase != 3) return 1;
    if (action == 101 || action == 106) {
        setup_roster_labels(delta, form, e);
        setup_panel(delta, form, 1);
    } else if (action == 103) {
        setup_preset_labels(delta, form, e);
        setup_panel(delta, form, 3);
    } else if (action == 102) {
        setup_panel(delta, form, 0);
    } else if (action == 100) {
        if (e->room) e->lineup.custom ^= 1;
        else e->profile.custom ^= 1;
        setup_roster_labels(delta, form, e);
    } else if (action == 104 || action == 105) {
        unsigned split = e->room ? 7 : 6;
        unsigned start = action == 104 ? 0 : split;
        unsigned end = action == 104 ? split : split+7;
        for (unsigned i = start; i < end; ++i) setup_set_slot(e, i, 2);
        if (e->room) e->lineup.custom = 1;
        else e->profile.custom = 1;
        setup_roster_labels(delta, form, e);
    } else if (action == 115 || action == 116) {
        e->team = action-115;
        setup_roster_labels(delta, form, e);
    } else if (action >= 120 && action <= 132+(e->room != 0)) {
        e->slot = setup_slot_index(e, action-120);
        uint32_t value = setup_get_slot(e, e->slot);
        unsigned random = (value & 15) == 2;
        e->tier = random ? (value>>4)&15 : 0;
        e->kind = random ? (value>>8)&15 : 0;
        if (e->tier > 10) e->tier = 0;
        if (e->kind > 4) e->kind = 0;
        setup_slot_labels(delta, form, e);
        setup_panel(delta, form, 2);
    } else if (action == 109) {
        TargetPicker *p = &e->picker;
        p->panel = find_control(form, "SolTargetPicker", delta);
        p->modules_panel = find_control(form, "SolTargetModules", delta);
        if (p->panel && p->modules_panel && bind_picker_controls(delta,p)) {
            p->module_entry = 0;
            target_catalog(delta, p);
            target_query(delta,p);
            target_collect(p);
            target_render(delta,p);
            setup_panel(delta, form, 4);
        }
    } else if (action == 110 || action == 111) {
        if (action == 110) e->tier = (e->tier+1) % 11;
        else e->kind = (e->kind+1) % 5;
        setup_slot_labels(delta, form, e);
    } else if (action == 108 || action == 112 || action == 113) {
        uint32_t value = action == 108 ? 0 : action == 113 ? UINT32_MAX :
                         2 | e->tier<<4 | e->kind<<8;
        setup_assign(delta, form, e, value);
    } else if (action == 114) {
        setup_panel(delta, form, 1);
    } else if (action >= 140 && action <= 147) {
        if (action < 144) e->preset = action-140;
        else if (action == 145) e->profile.active = e->profile.active == e->preset+1 ? 0 : e->preset+1;
        else if (action == 146) e->profile.values[e->preset][TANK_STAT_COUNT] ^= 1;
        else if (action == 147)
            __builtin_memset(e->profile.values[e->preset], 0, sizeof(e->profile.values[e->preset]));
        setup_preset_labels(delta, form, e);
    } else if (action >= 150 && action <= 152) {
        e->stat_group = action-150;
        setup_preset_labels(delta, form, e);
    } else {
        setup_catalog_event(delta, form, e, action);
    }
    return 1;
}

__attribute__((section(".late_sandbox"), used, noinline))
static void setup_receive(void *view, uint32_t index, uint32_t word) {
    uint32_t delta = sandbox_delta();
    if (!view) return;
    void *form = (void *)FIELD(view, 4);
    void *identity = form ? find_control(form, "**/SolSetupIdentity", delta) : 0;
    if (!identity) return;
    Sandbox *s = state(delta, 1);
    if (!s) return;
    SetupEditor *e = &s->setup;
    if (!index) {
        __builtin_memset(&e->profile, 0, sizeof(e->profile));
        e->identity = identity;
        e->received = 0;
        e->picker.module_entry = 0;
    }
    if (e->identity != identity || index != e->received) return;
    unsigned offset = index*4;
    uint8_t *data = (uint8_t *)&e->profile;
    for (unsigned i = 0; i < 4 && offset+i < sizeof(SetupProfile); ++i)
        data[offset+i] = word >> (8*i);
    ++e->received;
    if (e->received == (sizeof(SetupProfile)+3)/4) {
        if (e->profile.active > 4 || e->profile.custom > 1)
            __builtin_memset(&e->profile, 0, sizeof(e->profile));
        e->preset = e->profile.active ? e->profile.active-1 : 0;
    }
}

/* Original frame at the pinned bot-settings response trampoline. */
__asm__(
    ".section .late_sandbox,\"ax\"\n"
    ".globl sandbox_setup_reply\n"
    "sandbox_setup_reply:\n"
    "cmpl $130, 12(%ebp)\n"
    "jb 1f\n"
    "cmpl $155, 12(%ebp)\n"
    "ja 1f\n"
    "pushal\n"
    "movl 12(%ebp), %eax\n"
    "subl $130, %eax\n"
    "pushl 8(%ebp)\n"
    "pushl %eax\n"
    "pushl 0x178(%esi)\n"
    "call setup_receive\n"
    "addl $12, %esp\n"
    "popal\n"
    "jmp 0x1e92bcc\n"
    "1: cmpl $127, 12(%ebp)\n"
    "je 0x4b1de82\n"
    "jmp 0x4b1de6a\n"
    ".text\n");


/* Keep the stock PC controls local even if the account options are rebuilt. */
#define CONTROL_TOGGLES 4
#define CONTROL_SLIDERS 2
#define CONTROLS_MAGIC 0x31434253u /* SBC1 */
typedef struct {
    uint32_t magic, valid, toggles;
    float sensitivity[CONTROL_SLIDERS];
} SavedControls;

static const char controls_path[] BOSS_DATA = "..\\documents\\solbreaker-controls.bin";
static const char controls_temp[] BOSS_DATA = "..\\documents\\solbreaker-controls.tmp";
static const char zoom_path[] BOSS_DATA = "..\\documents\\solbreaker-sniper-zoom.bin";
static const char control_names[][30] BOSS_DATA = {
    "SmartZoom", "InvertedZoom", "InvertedMouse", "BackwardZoom",
    "MouseSensitivity", "MouseSensitivityInSnipperMode",
};

__attribute__((section(".late_sandbox"), noinline))
static int control_index(void *control) {
    for (unsigned depth = 0; control && depth < 5; depth++, control = (void *)FIELD(control, 0x38)) {
        const char *name = (const char *)FIELD(control, 0x30);
        if (!name) continue;
        for (unsigned index = 0; index < CONTROL_TOGGLES+CONTROL_SLIDERS; index++) {
            const char *expected = control_names[index];
            unsigned i = 0;
            while (expected[i] && expected[i] == name[i]) i++;
            if (!expected[i] && !name[i]) return (int)index;
        }
    }
    return -1;
}

__attribute__((section(".late_sandbox"), noinline))
static void load_controls(uint32_t delta, SavedControls *settings) {
    if (read_setting(delta, controls_path, settings, sizeof(*settings)) &&
            settings->magic == CONTROLS_MAGIC) return;
    *settings = (SavedControls){.magic = CONTROLS_MAGIC};
    uint8_t value;
    if (read_setting(delta, zoom_path, &value, sizeof(value)) && value <= 1) {
        settings->valid = 1;
        settings->toggles = value;
    }
}

__attribute__((section(".late_sandbox"), noinline))
static void save_mouse_sensitivity(uint32_t delta, void *slider) {
    int index = control_index(slider);
    if (index < CONTROL_TOGGLES) return;
    float value = *(const float *)((uintptr_t)slider + 0x15c);
    if (!(value >= .049f && value <= 1.501f)) return;
    SavedControls settings;
    load_controls(delta, &settings);
    settings.valid |= 1u << index;
    settings.sensitivity[index-CONTROL_TOGGLES] = value;
    write_setting(delta, controls_path, controls_temp, &settings, sizeof(settings));
}

__attribute__((section(".late_sandbox"), noinline))
void THISCALL sandbox_zoom_changed(void *toggle, unsigned event, void *input) {
    uint32_t delta = sandbox_delta();
    typedef void (THISCALL *SendEvent)(void *, unsigned, void *);
    int index = control_index(toggle);
    uint8_t value = *(const uint8_t *)((uintptr_t)toggle + 0x14c);
    if (index >= 0 && index < CONTROL_TOGGLES && value <= 1) {
        SavedControls settings;
        load_controls(delta, &settings);
        uint32_t bit = 1u << index;
        settings.valid |= bit;
        /* Native switch position is the inverse of its boolean option. */
        settings.toggles = (settings.toggles & ~bit) | (value ? 0 : bit);
        write_setting(delta, controls_path, controls_temp, &settings, sizeof(settings));
    }
    ((SendEvent)(0xb84750u + delta))(toggle, event, input);
}

__attribute__((section(".late_sandbox"), used, noinline))
static int zoom_migrate(void *options) {
    uint32_t delta = sandbox_delta();
    SavedControls settings;
    load_controls(delta, &settings);
    typedef void (THISCALL *SetBool)(void *, const NativeString *, unsigned);
    typedef void (THISCALL *SetFloat)(void *, const NativeString *, float);
    typedef void (THISCALL *String)(NativeString *, const char *);
    typedef void (THISCALL *Destroy)(NativeString *);
    for (unsigned i = 0; i < CONTROL_TOGGLES+CONTROL_SLIDERS; i++) {
        if (!(settings.valid & (1u << i))) continue;
        NativeString key;
        ((String)(0x69a7d0u + delta))(&key, control_names[i]);
        if (i < CONTROL_TOGGLES) {
            ((SetBool)(0x123cbb0u + delta))(options, &key, (settings.toggles >> i) & 1);
        } else {
            float value = settings.sensitivity[i-CONTROL_TOGGLES];
            if (value >= .05f && value <= 1.5f)
                ((SetFloat)(0x123d780u + delta))(options, &key, value);
        }
        ((Destroy)(0x69a720u + delta))(&key);
    }
    return settings.valid & 1;
}

__asm__(
    ".section .late_sandbox,\"ax\"\n"
    ".globl sandbox_zoom_migration\n"
    "sandbox_zoom_migration:\n"
    "pushal\n"
    "pushl %edi\n"
    "call zoom_migrate\n"
    "addl $4, %esp\n"
    "testl %eax, %eax\n"
    "jz 1f\n"
    "movb $0, -0x5d(%ebp)\n"
    "1: popal\n"
    "cmpb $0, -0x5d(%ebp)\n"
    "movl $0, -0x18(%ebp)\n"
    "jmp 0x1237313\n"
    ".text\n"
);

/* The HUD passes a fixed 600 m world size to MinimapObjectsViewImpl.
 * At this call site the parent frame supplies AvatarContextBattle through
 * its +0x120 field. Substitute the larger extent only for the local range. */
__asm__(
    ".section .late_sandbox,\"ax\"\n"
    ".globl sandbox_minimap_extent\n"
    "sandbox_minimap_extent:\n"
    "pushfl\n"
    "pushal\n"
    "call sandbox_delta\n"
    "leal 0x174eab5(%eax), %ecx\n"
    "cmpl %ecx, 4(%ebp)\n"
    "jne 2f\n"
    "movl (%ebp), %edx\n"
    "movl -0x50(%edx), %edx\n"
    "testl %edx, %edx\n"
    "jz 2f\n"
    "movl 0x120(%edx), %edx\n"
    "testl %edx, %edx\n"
    "jz 2f\n"
    "leal 0x37f86d0(%eax), %ecx\n"
    "cmpl %ecx, (%edx)\n"
    "jne 2f\n"
    "movl 0x2c(%edx), %edx\n"
    "testl %edx, %edx\n"
    "jz 2f\n"
    "leal 0x3844a04(%eax), %ecx\n"
    "cmpl %ecx, (%edx)\n"
    "jne 2f\n"
    "cmpw $107, 0x14c(%edx)\n"
    "jne 2f\n"
    "call 1f\n"
    "1: popl %edx\n"
    "addl $(3f-1b), %edx\n"
    "movl %edx, 52(%esp)\n"
    "2: popal\n"
    "popfl\n"
    "jmp 0x1da4350\n"
    ".balign 4\n"
    "3: .float 1024.0, 1024.0\n"
    ".text\n"
);
