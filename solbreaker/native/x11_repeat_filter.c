/* Per-process X11 input workarounds for Blitz 11.21 under Proton.
 * No XKB settings, grabs, or input synthesis.
 */
#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct repeat_state {
    unsigned char down[256];
    Display *display;
    uint64_t last_warp;
};
static __thread struct repeat_state state;
static pthread_once_t symbols_once = PTHREAD_ONCE_INIT;
static pthread_once_t keysyms_once = PTHREAD_ONCE_INIT;
static Bool (*real_filter)(XEvent *, Window);
static int (*real_warp)(Display *, Window, Window, int, int, unsigned int, unsigned int, int, int);
static int (*real_lookup)(XKeyEvent *, char *, int, KeySym *, XComposeStatus *);
static int (*real_mb_lookup)(XIC, XKeyPressedEvent *, char *, int, KeySym *, Status *);
static uint32_t (*keysym_to_utf32)(uint32_t);
static __thread int target_confirmed;
static __thread unsigned suppressed;

static void resolve_symbols(void) {
    real_filter = dlsym(RTLD_NEXT, "XFilterEvent");
    real_warp = dlsym(RTLD_NEXT, "XWarpPointer");
    real_lookup = dlsym(RTLD_NEXT, "XLookupString");
    real_mb_lookup = dlsym(RTLD_NEXT, "XmbLookupString");
}
static void resolve_keysyms(void) {
    void *library = dlopen("libxkbcommon.so.0", RTLD_LAZY | RTLD_LOCAL);
    if (library) keysym_to_utf32 = dlsym(library, "xkb_keysym_to_utf32");
}
static uint64_t now_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return 0;
    return (uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
/* Wine changes argv[0] to the Windows executable. Test lazily, because the
 * ELF constructor runs before that change. Other inherited processes pass through. */
static int is_target(void) {
    char command[4096];
    const char *target;
    ssize_t n;
    int fd;
    if (target_confirmed) return 1;
    target = getenv("SOLBREAKER_X11_REPEAT_TARGET");
    if (!target || !*target) return 0;
    fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    n = read(fd, command, sizeof(command) - 1);
    close(fd);
    if (n <= 0) return 0;
    command[n] = 0;
    const char *path = command;
    if ((command[0] == 'Z' || command[0] == 'z') && command[1] == ':') {
        for (char *p = command + 2; *p; ++p) if (*p == '\\') *p = '/';
        path += 2;
    }
    if (strcmp(path, target)) return 0;
    target_confirmed = 1;
    return 1;
}
static void recover_cyrillic(const XKeyEvent *event, int count, KeySym *keysym) {
    if (count || !keysym || *keysym < 0x06a1 || *keysym > 0x06ff ||
        event->type != KeyPress || (event->state & (ControlMask | Mod1Mask)) || !is_target())
        return;
    pthread_once(&keysyms_once, resolve_keysyms);
    if (!keysym_to_utf32) return;
    uint32_t codepoint = keysym_to_utf32(*keysym);
    /* Proton's X11 ToUnicodeEx accepts a direct Unicode keysym when
     * Xlib supplied no text. Preserve the normal lookup and IME result. */
    if (codepoint) *keysym = 0x01000000 | codepoint;
}
int XLookupString(XKeyEvent *event, char *buffer, int size, KeySym *keysym, XComposeStatus *compose) {
    pthread_once(&symbols_once, resolve_symbols);
    if (!real_lookup) return 0;
    int count = real_lookup(event, buffer, size, keysym, compose);
    recover_cyrillic(event, count, keysym);
    return count;
}
int XmbLookupString(XIC ic, XKeyPressedEvent *event, char *buffer, int size, KeySym *keysym, Status *status) {
    pthread_once(&symbols_once, resolve_symbols);
    if (!real_mb_lookup) return 0;
    int count = real_mb_lookup(ic, event, buffer, size, keysym, status);
    if (status && *status == XLookupKeySym) recover_cyrillic(event, count, keysym);
    return count;
}
static void select_display(Display *display) {
    if (state.display != display) {
        memset(&state, 0, sizeof(state));
        state.display = display;
    }
}
static int suppress_press(struct repeat_state *s, unsigned key, uint64_t now) {
    int repeated = s->down[key];
    s->down[key] = 1;
    return repeated && s->last_warp && now >= s->last_warp && now - s->last_warp < 150;
}
Bool XFilterEvent(XEvent *event, Window window) {
    pthread_once(&symbols_once, resolve_symbols);
    /* XIM may consume a physical press and later forward it. Only presses
     * actually delivered past XIM count as down. Always let XIM see repeats
     * too, otherwise its asynchronous key-event bookkeeping can desynchronize. */
    if (event->type == FocusOut && is_target()) {
        memset(state.down, 0, sizeof(state.down));
        state.last_warp = 0;
    }
    Bool consumed = real_filter ? real_filter(event, window) : False;
    if (consumed) return True;
    if (is_target()) {
        select_display(event->xany.display);
        if (event->type == FocusOut) {
            memset(state.down, 0, sizeof(state.down));
            state.last_warp = 0;
        } else if (event->type == KeyRelease && event->xkey.keycode < 256) {
            state.down[event->xkey.keycode] = 0;
        } else if (event->type == KeyPress && event->xkey.keycode < 256 && !event->xkey.send_event) {
            if (suppress_press(&state, event->xkey.keycode, now_ms())) {
                if (getenv("SOLBREAKER_X11_REPEAT_TRACE") && !(suppressed++ % 100))
                    fprintf(stderr, "solbreaker_input_filter suppressed=%u key=%u\n", suppressed, event->xkey.keycode);
                return True;
            }
        }
    }
    return False;
}
int XWarpPointer(Display *display, Window src, Window dest, int sx, int sy,
                 unsigned int width, unsigned int height, int dx, int dy) {
    int result;
    pthread_once(&symbols_once, resolve_symbols);
    if (!real_warp) return 0;
    result = real_warp(display, src, dest, sx, sy, width, height, dx, dy);
    if (is_target() && dest != None) {
        select_display(display);
        state.last_warp = now_ms();
    }
    return result;
}
