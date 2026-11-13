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
/* The pinned keyboard registry uses 0 through 0x32 for stock actions. */
#define FIRST_SANDBOX_ACTION 0x33u
#define SANDBOX_ACTION_COUNT 5u
#define VISIBLE_SANDBOX_ACTIONS 4u
#define ABILITY_ONE_ACTION 7u /* Native Consumables4 / ABILITY 1. */

typedef struct { char text[16]; uint32_t size, capacity; } NativeString;
typedef struct { uint32_t device, key, modifiers, phase; } KeyBinding;
typedef struct { const uint32_t *begin, *end, *capacity; } ActionVector;
typedef int (THISCALL *NativeBind)(void *, uint32_t, NativeString, const KeyBinding *);
typedef struct { uint32_t key, modifiers; } SavedKey;
typedef struct { uint32_t magic; SavedKey keys[SANDBOX_ACTION_COUNT]; } SavedBindings;
#define BINDINGS_MAGIC 0x314b4253u  /* SBK1 */

/* The launcher runs the executable from the prepared client's client/ directory. */
static const char bindings_path[] = "..\\documents\\solbreaker-keybinds.bin";
static const char bindings_temp[] = "..\\documents\\solbreaker-keybinds.tmp";

static const NativeString binding_names[] = {
    {"SolBoost", 8, 15}, {"SolFlight", 9, 15}, {"SolMissile", 10, 15},
    {"SolTarget", 9, 15}, {"SolJump", 7, 15},
};
static const NativeString binding_captions[][2] = {
    {{"Local/SbBoost", 13, 15}, {"", 0, 15}},
    {{"Local/SbFlight", 14, 15}, {"", 0, 15}},
    {{"Local/SbMissile", 15, 15}, {"", 0, 15}},
    {{"Local/SbTarget", 14, 15}, {"", 0, 15}},
    {{"Local/SbJump", 12, 15}, {"", 0, 15}},
};

typedef struct { float x, y, z; } Vector;
typedef struct {
    uint32_t thread, avatar, owner, scene, token, sequence, previous_keys;
    uint32_t toggles, last_report, missile_tick, shot, active_view, view_tick;
    uint32_t sent_flags, key_state, window_process;
    int32_t mouse_x, mouse_y;
    Vector position, velocity, view, heading;
    uint32_t pending_actions, allowed_keys;
    void *bindings_layout;
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

static int read_bindings(uint32_t delta, SavedBindings *data) {
    typedef int (STDCALL *Read)(void *, void *, uint32_t, uint32_t *, void *);
    typedef int (STDCALL *Close)(void *);
    void *file = open_binding_file(delta, bindings_path, 0x80000000u, 3);
    if (file == (void *)UINT32_MAX) return 0;
    uint32_t count = 0;
    int ok = (*(Read *)(0x036ef194u + delta))(file, data, sizeof(*data), &count, 0);
    (*(Close *)(0x036ef320u + delta))(file);
    return ok && count == sizeof(*data) && data->magic == BINDINGS_MAGIC;
}

static int save_bindings(uint32_t delta, void *layout) {
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
    void *file = open_binding_file(delta, bindings_temp, 0x40000000u, 2);
    if (file == (void *)UINT32_MAX) return 0;
    SavedBindings data = {.magic = BINDINGS_MAGIC};
    for (unsigned index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++) {
        KeyBinding key = get_binding(delta, layout, FIRST_SANDBOX_ACTION + index);
        if (key.device == 0x400) data.keys[index] = (SavedKey){key.key, key.modifiers};
    }
    uint32_t count = 0;
    int ok = (*(Write *)(0x036ef0d4u + delta))(file, &data, sizeof(data), &count, 0)
             && count == sizeof(data) && (*(Flush *)(0x036ef080u + delta))(file);
    (*(Close *)(0x036ef320u + delta))(file);
    if (ok && move(bindings_temp, bindings_path, 9)) return 1;
    (*(Delete *)(0x036ef068u + delta))(bindings_temp);
    return 0;
}

static void seed_bindings(uint32_t delta, void *layout, int restore) {
    NativeBind bind = (NativeBind)(0x0123c830u + delta);
    SavedBindings saved;
    int have_saved = restore && read_bindings(delta, &saved);
    for (uint32_t index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++) {
        uint32_t action = FIRST_SANDBOX_ACTION + index;
        if (!get_binding(delta, layout, action).device) {
            /* Native keyboard device, Numpad 1 through 4, key-down phase. */
            KeyBinding key = {0x400, 0x4a + index, 0, 9};
            if (have_saved && saved.keys[index].key && saved.keys[index].key < 0x7f
                    && saved.keys[index].modifiers < 16) {
                key.key = saved.keys[index].key;
                key.modifiers = saved.keys[index].modifiers;
            }
            bind(layout, action, binding_names[index], &key);
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
    if (action-FIRST_SANDBOX_ACTION >= VISIBLE_SANDBOX_ACTIONS)
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
        uint32_t action = index == VISIBLE_SANDBOX_ACTIONS ? ABILITY_ONE_ACTION : FIRST_SANDBOX_ACTION + index;
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
    if (device != 0x400 || columns != 3 || count > 58) {
        original(controller, page, actions, device, columns);
        return;
    }
    void *layout = keyboard_layout(delta);
    if (layout) seed_bindings(delta, layout, 1);
    for (unsigned index = 0; index < VISIBLE_SANDBOX_ACTIONS; index++) ids[index] = FIRST_SANDBOX_ACTION + index;
    ids[4] = ids[5] = UINT32_MAX;
    for (unsigned index = 0; index < count; index++) ids[6+index] = actions->begin[index];
    ActionVector combined = {ids, ids+6+count, ids+64};
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
            value->thread = thread_id();
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
    __atomic_exchange_n(&s->mouse_x, 0, __ATOMIC_RELAXED);
    __atomic_exchange_n(&s->mouse_y, 0, __ATOMIC_RELAXED);
}

static void reset(Sandbox *s) {
    clear_view(s);
    s->owner = s->avatar = s->scene = s->token = s->sequence = 0;
    s->previous_keys = s->toggles = s->last_report = s->sent_flags = s->shot = 0;
    s->pending_actions = s->allowed_keys = 0;
    s->bindings_layout = 0;
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

int sandbox_message(uint32_t delta, const uint32_t *first, const uint32_t *end,
                    const uint32_t *second, const uint32_t *second_end) {
    typedef int (*Original)(uint32_t, const uint32_t *, const uint32_t *,
                            const uint32_t *, const uint32_t *);
    if (first && end >= first+2 && first[0] == 0x31534654u && first[1] == 0x584f4253u) {
        Sandbox *s = state(delta, 0);
        if (s && s->thread == thread_id() && s->owner && end == first+3
                && (second == second_end || (second && second_end == second+1)) && first[2]) {
            s->token = first[2];
            s->allowed_keys = second == second_end ? 15 : second[0] & 31;
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
    if (s && s->thread == thread_id()) reset(s);
}

void sandbox_frame(uint32_t delta, uint32_t *rotator) {
    typedef void (*Original)(uint32_t, uint32_t *);
    ((Original)(0x04b1d6cdu + delta))(delta, rotator);
    if (!rotator) return;
    uint32_t avatar = FIELD(rotator, 0x10);
    if (!avatar || FIELD(avatar, 0) != 0x03801fc4u + delta) return;
    uint32_t owner = FIELD(avatar, 4);
    if (!owner || FIELD(owner, 0) != 0x037f7058u + delta || !FIELD(owner, 0x1c)) return;
    Sandbox *s = state(delta, 1);
    if (!s || s->thread != thread_id()) return;
    if (s->owner != owner || s->avatar != avatar) {
        reset(s);
        s->owner = owner;
        s->avatar = avatar;
    }
    uint32_t consumer = FIELD(rotator, 0x13c);
    s->scene = 0;
    if (consumer && FIELD(consumer, 0) == 0x0382efd8u + delta) {
        uint32_t link = FIELD(consumer-0x160, 0x144);
        if (link) s->scene = FIELD(link, 0xc);
    }
    if (!foreground(delta, s) || !s->key_state || !battle_input_active(delta)) {
        s->previous_keys = 31;
        s->pending_actions = 0;
        clear_view(s);
        return;
    }
    uint32_t now = clock_ms(delta);
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
    if (changes & 1) s->toggles ^= BOOST;
    if (changes & 2) s->toggles ^= FLIGHT;
    if (changes & 4) s->toggles ^= MISSILE_VIEW;
    if (changes & 8) s->pending_actions |= SPAWN_TARGET;
    if (changes & 16) s->pending_actions |= GRAVITY_JUMP;
    uint32_t flags = s->toggles & (BOOST | FLIGHT);
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
    uint32_t interval = (flags & (MISSILE_VIEW | SPAWN_TARGET | GRAVITY_JUMP)) ? 55 : 170;
    if ((flags || s->sent_flags) && (!s->last_report || now-s->last_report >= interval)) {
        s->last_report = now ? now : 1;
        if (send_control(delta, s, flags, direction)) {
            s->sent_flags = flags & ~(SPAWN_TARGET | GRAVITY_JUMP);
            s->pending_actions = 0;
        }
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
        s->toggles &= ~MISSILE_VIEW;
        clear_view(s);
    }
    ((Stop)(0x01845dd0u + delta))(logic, shot, position, kind);
}

static void aim_missile_camera(Sandbox *s, uint32_t now, Vector *position, Vector *target) {
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
    *position = (Vector){view.x, view.z, view.y};
    *target = (Vector){view.x+heading.x, view.z+heading.z, view.y+heading.y};
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
    uint32_t camera = 0;
    if (__atomic_load_n(&s->active_view, __ATOMIC_RELAXED)) {
        camera = FIELD(scene, 0x120);
        if (camera && FIELD(camera, 0) != 0x0373ad3cu+delta
                   && FIELD(camera, 0) != 0x0382da70u+delta) camera = 0;
    }
    Vector old_position, old_target;
    if (camera) {
        Vector *position = (Vector *)(camera+0x38), *target = (Vector *)(camera+0x44);
        old_position = *position;
        old_target = *target;
        aim_missile_camera(s, clock_ms(delta), position, target);
        FIELD(camera, 0x248) |= 1;
    }
    draw(scene);
    if (camera) {
        *(Vector *)(camera+0x38) = old_position;
        *(Vector *)(camera+0x44) = old_target;
        FIELD(camera, 0x248) |= 1;
    }
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
