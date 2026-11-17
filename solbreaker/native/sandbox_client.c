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
#define PRACTICE_ACTIONS (NEXT_TARGET | TOGGLE_TARGET_FIRE | CLEAR_TARGETS)
#define ONE_SHOT_ACTIONS (SPAWN_TARGET | GRAVITY_JUMP | TELEPORT | PRACTICE_ACTIONS | RESTORE_HP)
/* The pinned keyboard registry uses 0 through 0x32 for stock actions. */
#define FIRST_SANDBOX_ACTION 0x33u
#define SANDBOX_ACTION_COUNT 12u
#define VISIBLE_SANDBOX_ACTIONS 11u
#define TELEPORT_KEY (1u << 5)
#define RESTORE_HP_KEY (1u << 9)
#define GOD_MODE_KEY (1u << 10)
#define JUMP_KEY (1u << VISIBLE_SANDBOX_ACTIONS)
#define SANDBOX_KEYS (JUMP_KEY - 1u)
#define ABILITY_ONE_ACTION 7u /* Native Consumables4 / ABILITY 1. */

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
static const char blur_temp[] = "..\\documents\\solbreaker-motion-blur.tmp";
static const char bindings_temp[] = "..\\documents\\solbreaker-keybinds.tmp";
__attribute__((section(".boss_rodata")))
static const char keyboard_path[] = "..\\documents\\solbreaker-keyboard.bin";
__attribute__((section(".boss_rodata")))
static const char keyboard_temp[] = "..\\documents\\solbreaker-keyboard.tmp";

static const NativeString binding_names[] = {
    {"SolBoost", 8, 15}, {"SolFlight", 9, 15}, {"SolMissile", 10, 15},
    {"SolTarget", 9, 15}, {"SolJump", 7, 15}, {"SolReload", 9, 15},
    {"SolTeleport", 11, 15},
    {"SolNextTank", 11, 15}, {"SolTargetFire", 13, 15}, {"SolClear", 8, 15},
    {"SolHeal", 7, 15}, {"SolGod", 6, 15},
};
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
};
static const uint8_t visible_bindings[] = {0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11};

typedef struct { float x, y, z; } Vector;
typedef struct { Vector origin, velocity; } AimRay;
typedef struct { float x, y; } Point;
typedef struct { float x, y, width, height; } Rect;
typedef struct { Vector position, target; } CameraPose;
typedef struct { void *control, *hidden_row; } TeamPanel;
typedef struct { uint32_t effect, entity, renderer; } ParticleIdentity;
typedef struct { void **begin, **end; } ParticleVector;
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
    uint32_t graphics_thread;
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
    static const uint8_t saved_counts[] = {SANDBOX_ACTION_COUNT, 11, 10, 7, 6, 5};
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
    for (unsigned index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++) {
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
    for (uint32_t index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++) {
        uint32_t binding = visible_bindings[index];
        uint32_t action = FIRST_SANDBOX_ACTION + binding;
        if (!get_binding(delta, layout, action).device) {
            /* Native keyboard device, Numpad 1 through 9 then 0. */
            KeyBinding key = {0x400, binding == 10 ? 0x49 : 0x4a + index, 0, 9};
            if (binding == 11) key = (KeyBinding){0x400, 0x53, 0, 9};
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
        if (!(s->allowed_keys & (1u << index))) continue;
        uint32_t action = index == VISIBLE_SANDBOX_ACTIONS ? ABILITY_ONE_ACTION
                          : FIRST_SANDBOX_ACTION + visible_bindings[index];
        KeyBinding key = get_binding(delta, layout, action);
        if (key.device == 0x400 && key.modifiers == modifiers && key.key
                && (uint8_t)*down(keyboard, &value, key.key)) pressed |= 1u << index;
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
    if (device != 0x400 || columns != 3 || count > 64-VISIBLE_SANDBOX_ACTIONS) {
        original(controller, page, actions, device, columns);
        return;
    }
    void *layout = keyboard_layout(delta);
    if (layout) seed_bindings(delta, layout, 1);
    for (unsigned index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++)
        ids[index] = FIRST_SANDBOX_ACTION + visible_bindings[index];
    for (unsigned index = 0; index < count; index++) ids[VISIBLE_SANDBOX_ACTIONS+index] = actions->begin[index];
    ActionVector combined = {ids, ids+VISIBLE_SANDBOX_ACTIONS+count, ids+64};
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
static void *find_control(void *root, const char *name, uint32_t delta);
static void bind_battle_controls(uint32_t delta, Sandbox *s, void *root);
static void temporal_audio_gain(uint32_t delta, Sandbox *s, uint32_t percent);
static void temporal_roster(uint32_t delta, Sandbox *s, uint32_t entity);

static void reset(uint32_t delta, Sandbox *s) {
    temporal_audio_gain(delta, s, 100u);
    s->temporal_release_gain = 0;
    clear_view(s);
    s->owner = s->avatar = s->scene = s->token = s->sequence = 0;
    s->previous_keys = s->toggles = s->last_report = s->sent_flags = s->shot = 0;
    s->pending_actions = s->allowed_keys = 0;
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

static void sandbox_notice(uint32_t delta, Sandbox *s, uint32_t index) {
    static const NativeString keys[] = {
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
        uint32_t notice = index == 21 ? 0x116 : 0x100 + index;
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
int sandbox_message(uint32_t delta, const uint32_t *first, const uint32_t *end,
                    const uint32_t *second, const uint32_t *second_end) {
    typedef int (*Original)(uint32_t, const uint32_t *, const uint32_t *,
                            const uint32_t *, const uint32_t *);
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
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id()) {
            uint32_t status = first[2];
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
            if (capabilities & (64 | 256 | 1024 | 0x8000)) {
                if (s->token == first[2]) {
                    if ((capabilities & 64) && (s->allowed_keys & 8))
                        sandbox_notice(delta, s, 8);
                    if ((capabilities & 256) && (s->allowed_keys & TELEPORT_KEY))
                        sandbox_notice(delta, s, 9);
                    if ((capabilities & 1024) && (s->allowed_keys & 0x1c0))
                        sandbox_notice(delta, s, 10 + ((capabilities >> 11) & 7));
                    if ((capabilities & 0x8000) && (s->allowed_keys & RESTORE_HP_KEY))
                        sandbox_notice(delta, s, 18);
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
                              | ((capabilities & 0x10000) ? GOD_MODE_KEY : 0);
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
        s->temporal_panel = s->temporal_label = 0;
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
    if (!foreground(delta, s) || !s->key_state || !battle_input_active(delta)) {
        s->previous_keys = SANDBOX_KEYS | JUMP_KEY;
        s->pending_actions = 0;
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
    uint32_t pressed = binding_keys(delta, s) & s->allowed_keys;
    uint32_t changes = pressed & ~s->previous_keys;
    s->previous_keys = pressed;
    if (changes & 1) {
        s->toggles ^= BOOST;
        sandbox_notice(delta, s, !!(s->toggles & BOOST));
    }
    if (changes & 2) {
        s->toggles ^= FLIGHT;
        sandbox_notice(delta, s, 2 + !!(s->toggles & FLIGHT));
    }
    if (changes & 4) {
        s->toggles ^= MISSILE_VIEW;
        sandbox_notice(delta, s, 4 + !!(s->toggles & MISSILE_VIEW));
    }
    if (changes & 8) s->pending_actions |= SPAWN_TARGET;
    if (changes & 16) {
        s->toggles ^= FAST_RELOAD;
        sandbox_notice(delta, s, 6 + !!(s->toggles & FAST_RELOAD));
    }
    if (changes & JUMP_KEY) s->pending_actions |= GRAVITY_JUMP;
    if (changes & TELEPORT_KEY) s->pending_actions |= TELEPORT;
    if (changes & GOD_MODE_KEY) {
        s->toggles ^= GOD_MODE;
        sandbox_notice(delta, s, 19 + !!(s->toggles & GOD_MODE));
    }
    /* The four Numpad 7–0 action bits follow the order of their binding bits. */
    s->pending_actions |= (changes << 3) & (PRACTICE_ACTIONS | RESTORE_HP);
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

static CameraPose swap_pose(uint32_t camera, CameraPose pose) {
    CameraPose *current = (CameraPose *)(camera+0x38);
    CameraPose previous = *current;
    *current = pose;
    FIELD(camera, 0x248) |= 1;
    return previous;
}

__attribute__((section(".late_sandbox"), noinline))
static float temporal_project(uint32_t delta, uint32_t camera, Vector p, Point *point) {
    typedef const float *(THISCALL *ViewProj)(uint32_t, uint32_t);
    const float *m = ((ViewProj)(0xa7aee0u + delta))(camera, 0);
    float depth = p.x*m[3]+p.y*m[7]+p.z*m[11]+m[15];
    if (!(depth > .5f && depth < 260.f)) return 0;
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
    float depth = temporal_project(delta, camera, s->peer_origin, &point);
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

/* MarkerSystem projects the world positions during the scene update, before
 * the draw hook runs. Use the same pose in both passes. */
void THISCALL sandbox_project_markers(void *system, float elapsed) {
    typedef void (THISCALL *Update)(void *, float);
    uint32_t delta = sandbox_delta();
    Update update = (Update)(0x82c280u + delta);
    Sandbox *s = state(delta, 0);
    uint32_t scene = FIELD(system, 0x28);
    uint32_t camera = 0;
    if (s && s->thread == thread_id() && scene && scene == s->scene &&
            __atomic_load_n(&s->active_view, __ATOMIC_RELAXED))
        camera = battle_camera(delta, scene);
    if (!camera) {
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
    float depth = temporal_project(delta, camera, s->temporal_transition_position, &point);
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
    if (s && s->owner && s->thread == thread_id() && (s->role_state || s->swap_token))
        boss_picker_update(delta, s);
    if (!s || s->thread != thread_id() || s->scene != (uint32_t)scene) {
        draw(scene);
        return;
    }
    if (s->peer_hidden_entity) temporal_roster(delta, s, s->peer_hidden_entity);
    uint32_t camera = battle_camera(delta, (uint32_t)scene);
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
    /* These stock event modes hide Exit to Hangar during the battle. */
    if (avatar) {
        uint8_t mode = *(const uint8_t *)((uintptr_t)avatar + 0xd9);
        if (mode == 8 || mode == 23 || mode == 27)
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
    const char *text = room_text(value);
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
    if (!field || result->size > 4096) return result;
    NativeString options;
    ((Get)(0xb7b0f0u + delta))(field, &options);
    const char *tags = room_text(&options);
    const char *description = room_text(result);
    /* The same stock view also submits the solo bot recipe. */
    if (!room_prefix(description, "SolbreakerBots/") && options.size > 0 && options.size < 128) {
        static const char marker[] = "[room-form] ";
        char combined[4096 + 128 + sizeof(marker)];
        uint32_t length = 0;
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

/* Both team controls own an ordered map of vehicle IDs to row controls.
 * Resolve the current row on each use. A native update may replace a row. */
BOSS_UI static void *temporal_team_row(uint32_t delta, void *control, uint32_t entity) {
    if (!control) return 0;
    uint32_t vtable = FIELD(control, 0)-delta;
    unsigned offset;
    if (vtable == 0x3848eb4u) offset = 0x168;       /* UITeamPanel */
    else if (vtable == 0x384c5e0u) offset = 0x148;  /* ShortTeamPanel */
    else return 0;
    uint32_t head = FIELD(control, offset);
    for (uint32_t node = head ? FIELD(head, 4) : 0; node && node != head;) {
        uint32_t id = FIELD(node, 0x10);
        if (id == entity) return (void *)FIELD(node, 0x14);
        node = FIELD(node, entity < id ? 0 : 8);
    }
    return 0;
}

BOSS_UI static void bind_team_panels(uint32_t delta, Sandbox *s) {
    void *root = s->battle_controls_root;
    if (!root || !s->peer_clock) return;
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
    s->temporal_panel = find_control(root, "SolTimeHUD", delta);
    s->temporal_label = s->temporal_panel ? find_control(s->temporal_panel, "SolTimeValue", delta) : 0;
    s->temporal_hud = 0;
    boss_visible(s->temporal_panel, 0, delta);
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
}

BOSS_UI static void boss_picker_update(uint32_t delta, Sandbox *s) {
    typedef int16_t (STDCALL *Key)(int);
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
        s->graphics_thread = thread_id();
        *(float *)((uintptr_t)slider + 0x15c) = (float)s->blur_percent;
        ((Refresh)(0xb85e10u + delta))(slider);
        update_blur_label(label, s->blur_percent, delta);
    }
    return page;
}

void THISCALL sandbox_blur_slider(void *slider, void *event) {
    typedef void (THISCALL *Input)(void *, void *);
    uint32_t delta = sandbox_delta();
    ((Input)(0xb7dd20u + delta))(slider, event);
    if (!event || (FIELD(event, 0x38) != 1 && FIELD(event, 0x38) != 2 &&
                   FIELD(event, 0x38) != 3)) return;
    void *label = find_control(slider, "SolbreakerBlurPercent", delta);
    Sandbox *s = state(delta, 0);
    if (!label || !s || s->graphics_thread != thread_id()) return;
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
