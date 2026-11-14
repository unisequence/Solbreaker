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
#define PRACTICE_ACTIONS (NEXT_TARGET | TOGGLE_TARGET_FIRE | CLEAR_TARGETS)
#define ONE_SHOT_ACTIONS (SPAWN_TARGET | GRAVITY_JUMP | TELEPORT | PRACTICE_ACTIONS | RESTORE_HP)
/* The pinned keyboard registry uses 0 through 0x32 for stock actions. */
#define FIRST_SANDBOX_ACTION 0x33u
#define SANDBOX_ACTION_COUNT 11u
#define VISIBLE_SANDBOX_ACTIONS 10u
#define TELEPORT_KEY (1u << 5)
#define RESTORE_HP_KEY (1u << 9)
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

static const NativeString binding_names[] = {
    {"SolBoost", 8, 15}, {"SolFlight", 9, 15}, {"SolMissile", 10, 15},
    {"SolTarget", 9, 15}, {"SolJump", 7, 15}, {"SolReload", 9, 15},
    {"SolTeleport", 11, 15},
    {"SolNextTank", 11, 15}, {"SolTargetFire", 13, 15}, {"SolClear", 8, 15},
    {"SolHeal", 7, 15},
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
};
static const uint8_t visible_bindings[] = {0, 1, 2, 3, 5, 6, 7, 8, 9, 10};

typedef struct { float x, y, z; } Vector;
typedef struct { Vector position, target; } CameraPose;
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
    static const uint8_t saved_counts[] = {SANDBOX_ACTION_COUNT, 10, 7, 6, 5};
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
            if (have_saved && saved.keys[binding].key && saved.keys[binding].key < 0x7f
                    && saved.keys[binding].modifiers < 16) {
                key.key = saved.keys[binding].key;
                key.modifiers = saved.keys[binding].modifiers;
            }
            bind(layout, action, binding_names[binding], &key);
        }
    }
}

void THISCALL sandbox_reset_bindings(void *layout) {
    typedef void (THISCALL *Reset)(void *);
    uint32_t delta = sandbox_delta();
    ((Reset)(0x01232ab0u + delta))(layout);
    if (layout == keyboard_layout(delta)) {
        seed_bindings(delta, layout, 0);
        save_bindings(delta, layout);
    }
}

int THISCALL sandbox_set_binding(void *layout, uint32_t action, NativeString name, const KeyBinding *key) {
    uint32_t delta = sandbox_delta();
    NativeBind bind = (NativeBind)(0x0123c830u + delta);
    uint32_t index = action - FIRST_SANDBOX_ACTION;
    if (index >= SANDBOX_ACTION_COUNT || index == 4)
        return bind(layout, action, name, key);
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

static void reset(Sandbox *s) {
    clear_view(s);
    s->owner = s->avatar = s->scene = s->token = s->sequence = 0;
    s->previous_keys = s->toggles = s->last_report = s->sent_flags = s->shot = 0;
    s->pending_actions = s->allowed_keys = 0;
    s->bindings_layout = 0;
    s->aim_frame_tick = 0;
    s->blur_tick = s->blur_camera = s->blur_scene = 0;
    s->blur_offset[0] = s->blur_offset[1] = 0;
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

static int send_control(uint32_t delta, Sandbox *s, uint32_t flags, Vector direction) {
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
    FIELD(body, 9) = s->token;
    FIELD(body, 13) = ++s->sequence;
    FIELD(body, 17) = flags;
    FIELD(body, 21) = flags & MISSILE_VIEW ? s->shot : 0;
    *(Vector *)(body + 25) = direction;
    body[37] = 1;
    return 1;
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
        {"Local/SbHealed", 14, 15},
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
    if (text.size)
        ((Show)(0x1de19b0u + delta))(controller, &text, 0x100 + index);
    ((Destroy)(0x69a720u + delta))(&text);
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

int sandbox_message(uint32_t delta, const uint32_t *first, const uint32_t *end,
                    const uint32_t *second, const uint32_t *second_end) {
    typedef int (*Original)(uint32_t, const uint32_t *, const uint32_t *,
                            const uint32_t *, const uint32_t *);
    if (first && end == first+3 && first[0] == 0x31534654u && first[1] == 0x43494f56u) {
        /* VOICE_ENEMY_KILLED and the stock TrackHealth voice. */
        if (first[2] == 0x22 || first[2] == 0x4f)
            crew_voice(delta, state(delta, 0), first[2]);
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
            s->allowed_keys = (capabilities & 15) | ((capabilities & 16) ? JUMP_KEY : 0)
                              | ((capabilities & 32) ? 16 : 0)
                              | ((capabilities & 128) ? TELEPORT_KEY : 0)
                              | ((capabilities & 512) ? 0x1c0 : 0)
                              | ((capabilities & 0x4000) ? RESTORE_HP_KEY : 0);
            s->last_report = 0;
        }
        return 1;
    }
    return ((Original)(0x04b1cc42u + delta))(delta, first, end, second, second_end);
}

void sandbox_reset(uint32_t delta, uint32_t *layer) {
    typedef void (*Original)(uint32_t, uint32_t *);
    ((Original)(0x04b1ce1du + delta))(delta, layer);
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id()) {
        s->score_active = 0;
        s->score_panel = 0;
        reset(s);
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
        reset(s);
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
    /* The four Numpad 7–0 action bits follow the order of their binding bits. */
    s->pending_actions |= (changes << 3) & (PRACTICE_ACTIONS | RESTORE_HP);
    uint32_t flags = s->toggles & (BOOST | FLIGHT | FAST_RELOAD);
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
        uint32_t link = FIELD(consumer-0x160, 0x144);
        if (link) s->scene = FIELD(link, 0xc);
    }
    poll_controls(delta, s, now);
}

/* Some gun controllers bypass the local aim hook. The periodic server
 * gun-marker update still runs on the local AvatarGameLogic. */
void THISCALL sandbox_gun_marker(void *avatar, const Vector *point,
                                const Vector *direction, const float *extra) {
    typedef void (THISCALL *Update)(void *, const Vector *, const Vector *, const float *);
    uint32_t delta = sandbox_delta();
    ((Update)(0x1846390u + delta))(avatar, point, direction, extra);
    Sandbox *s = battle_state(delta, (uint32_t)avatar);
    if (s) {
        uint32_t now = clock_ms(delta);
        if (!s->aim_frame_tick || now - s->aim_frame_tick > 100)
            poll_controls(delta, s, now);
    }
}

void THISCALL sandbox_move(void *logic, const uint32_t *shooter, const uint32_t *shot,
        const Vector *position, const Vector *aux, const Vector *velocity,
        const float *speed, const uint8_t *effect, const float *extra) {
    typedef void (THISCALL *Move)(void *, const uint32_t *, const uint32_t *, const Vector *,
                                  const Vector *, const Vector *, const float *, const uint8_t *, const float *);
    uint32_t delta = sandbox_delta();
    Sandbox *s = state(delta, 0);
    if (s && s->thread == thread_id() && s->avatar == (uint32_t)logic
            && *shooter == FIELD(s->owner, 0x28)) {
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

/* UISceneSystem selects the material that composites the scene before the HUD. */
void *sandbox_blur_material(int alpha, int upscale, int grading) {
    typedef void *(*Select)(int, int, int);
    typedef void (THISCALL *Intern)(uint32_t *, const char *);
    typedef void (THISCALL *Property)(void *, const uint32_t *, const float *, int, int, int);
    uint32_t delta = sandbox_delta();
    void *material = ((Select)(0xa84af0u + delta))(alpha, upscale, grading);
    Sandbox *s = state(delta, 0);
    if (material && s && s->thread == thread_id()) {
        const float zero[4] = {0};
        const float *offset = zero;
        if (s->blur_percent && s->blur_scene && s->blur_scene == s->scene &&
                clock_ms(delta)-s->blur_tick < 100)
            offset = s->blur_offset;
        uint32_t property;
        ((Intern)(0x931830u + delta))(&property, "solbreakerCameraBlur");
        ((Property)(0xca2e30u + delta))(material, &property, offset, 3, 1, 0);
        s->blur_scene = 0;
    }
    return material;
}

void THISCALL sandbox_draw(void *scene) {
    typedef void (THISCALL *Draw)(void *);
    uint32_t delta = sandbox_delta();
    Draw draw = (Draw)(0x00d54070u + delta);
    Sandbox *s = state(delta, 0);
    if (!s || s->thread != thread_id() || s->scene != (uint32_t)scene) {
        draw(scene);
        return;
    }
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
