"""Regression coverage for the Proton input workaround."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from solbreaker import client_x11_input


@unittest.skipUnless(shutil.which('gcc') and Path('/usr/include/X11/Xlib.h').exists(),
                     'The repeat filter needs GCC and X11 headers')
class NativeRepeatFilterTests(unittest.TestCase):
    def test_key_lifecycle_and_ui_passthrough(self):
        source = r'''
#include "FILTER_SOURCE"
#include <assert.h>
static int forwarded, xim_consumes;
static Bool next_filter(XEvent *e, Window w) { (void)e; (void)w; ++forwarded; return xim_consumes; }
static int next_warp(Display *d, Window s, Window t, int x, int y, unsigned w, unsigned h, int a, int b) {
    (void)d;(void)s;(void)t;(void)x;(void)y;(void)w;(void)h;(void)a;(void)b;return 42;
}
int main(int argc, char **argv) {
    (void)argc;
    pthread_once(&symbols_once, resolve_symbols);
    real_filter=next_filter;real_warp=next_warp;
    XEvent e={0};e.xkey.type=KeyPress;e.xkey.display=(Display*)1;e.xkey.keycode=38;
    setenv("SOLBREAKER_X11_REPEAT_TARGET", "/another-client/wotblitz.exe", 1);
    XWarpPointer(e.xkey.display,0,1,0,0,0,0,100,100);
    assert(!XFilterEvent(&e,0));assert(!XFilterEvent(&e,0));assert(!state.last_warp);
    setenv("SOLBREAKER_X11_REPEAT_TARGET", argv[0], 1);
    assert(!XFilterEvent(&e,0)); /* first press */
    assert(!XFilterEvent(&e,0)); /* text autorepeat without mouse capture */
    assert(XWarpPointer(e.xkey.display,0,1,0,0,0,0,100,100)==42);
    int n=forwarded;
    assert(XFilterEvent(&e,0));assert(forwarded==n+1); /* XIM sees repeat; Wine doesn't */
    e.type=KeyRelease;assert(!XFilterEvent(&e,0)); /* never swallow keyup */
    e.type=KeyPress;assert(!XFilterEvent(&e,0));assert(XFilterEvent(&e,0));
    e.type=FocusOut;assert(!XFilterEvent(&e,0));assert(!state.last_warp);
    e.type=KeyPress;assert(!XFilterEvent(&e,0));assert(!XFilterEvent(&e,0));
    XWarpPointer(e.xkey.display,0,1,0,0,0,0,100,100);
    state.last_warp=now_ms()-200;assert(!XFilterEvent(&e,0)); /* chat/menu resumes */
    state.last_warp=now_ms()+100;assert(!XFilterEvent(&e,0)); /* clock guard */
    state.last_warp=now_ms();e.xkey.send_event=True;assert(!XFilterEvent(&e,0));
    e.xkey.send_event=False;e.xkey.keycode=256;assert(!XFilterEvent(&e,0));
    e.xkey.keycode=38;e.xkey.display=(Display*)2;assert(!XFilterEvent(&e,0));
    assert(!state.last_warp); /* separate X connection has no inherited key state */
    e.type=KeyRelease;assert(!XFilterEvent(&e,0));
    XWarpPointer(e.xkey.display,0,1,0,0,0,0,100,100);
    /* An input method consumes the first physical press, then forwards it.
     * That forwarded press must reach Wine immediately, not become a repeat. */
    e.type=KeyPress;xim_consumes=1;
    assert(XFilterEvent(&e,0));assert(!state.down[38]);
    xim_consumes=0;
    assert(!XFilterEvent(&e,0));assert(state.down[38]);
    assert(XFilterEvent(&e,0));
    /* A consumed release isn't yet delivered; its forwarded copy clears down. */
    e.type=KeyRelease;xim_consumes=1;assert(XFilterEvent(&e,0));assert(state.down[38]);
    xim_consumes=0;assert(!XFilterEvent(&e,0));assert(!state.down[38]);
    e.type=KeyPress;assert(!XFilterEvent(&e,0));
    e.type=FocusOut;xim_consumes=1;assert(XFilterEvent(&e,0));
    assert(!state.down[38]);assert(!state.last_warp);
    return 0;
}
'''.replace('FILTER_SOURCE', str(client_x11_input.SOURCE))
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp, 'test.c'); c.write_text(source)
            exe = Path(tmp, 'test')
            subprocess.run(['gcc', '-Wall', '-Wextra', '-Werror', str(c), '-ldl', '-pthread',
                            '-o', str(exe)], check=True, capture_output=True)
            subprocess.run([str(exe)], check=True, timeout=5)


class RepeatFilterLaunchTests(unittest.TestCase):
    @patch.object(client_x11_input.platform, 'machine', return_value='x86_64')
    @patch.object(client_x11_input.sys, 'platform', 'linux')
    def test_launch_preload_only_applies_to_the_prepared_client(self, _machine):
        with tempfile.TemporaryDirectory(prefix='solbreaker client ') as temporary:
            lab = Path(temporary)
            executable = lab / 'client/wotblitz.exe'
            original = {'DISPLAY': ':97', 'LD_PRELOAD': '/existing.so'}
            with patch.object(client_x11_input, '_libraries') as build:
                with client_x11_input.preload(original, lab, executable) as env:
                    head, tail = env['LD_PRELOAD'].split(':')
                    alias = Path(head).parent.parent
                    self.assertTrue(alias.is_symlink())
                    self.assertEqual(tail, '/existing.so')
                    self.assertEqual(os.readlink(alias), str(build.call_args.args[0]))
                    self.assertEqual(env['SOLBREAKER_X11_REPEAT_TARGET'], str(executable))
                self.assertFalse(alias.is_symlink())
                self.assertEqual(original['LD_PRELOAD'], '/existing.so')
                with client_x11_input.preload(dict(original, SOLBREAKER_X11_REPEAT_FILTER='0'),
                                             lab, executable) as env:
                    self.assertEqual(env['LD_PRELOAD'], '/existing.so')
                build.assert_called_once()
