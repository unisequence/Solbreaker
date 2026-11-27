//! Borrow the arena's DIRECT client through the public Bullet 3.2.7 plugin ABI.
//! The SDK destroys plugins before deleting their command processor. A call
//! holds a read lease throughout its use of the handle, including result reads.
use crate::bullet_api::Handle;
use std::collections::BTreeMap;
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, LazyLock, Mutex, RwLock, RwLockReadGuard};

const SHARED_MEMORY_MAGIC_NUMBER: i32 = 202010061;

#[repr(C)]
pub struct PluginContext {
    client: Handle,
    user: *mut c_void,
    // Only the first two fields of b3PluginContext are needed.
}

pub struct Session {
    // This opaque C handle is only used with a read lease and the arena owner.
    client: usize,
    live: RwLock<bool>,
    issued: AtomicBool,
}

impl Session {
    pub fn client(&self) -> Handle {
        self.client as Handle
    }

    pub fn guard(&self) -> Result<RwLockReadGuard<'_, bool>, &'static str> {
        let live = self.live.read().unwrap_or_else(|e| e.into_inner());
        if *live { Ok(live) } else { Err("Bullet arena is disconnected") }
    }
}

static SESSIONS: LazyLock<Mutex<BTreeMap<i32, Arc<Session>>>> =
    LazyLock::new(|| Mutex::new(BTreeMap::new()));
static NEXT_TOKEN: AtomicI32 = AtomicI32::new(1);

pub fn session(token: i32) -> Option<Arc<Session>> {
    SESSIONS.lock().unwrap_or_else(|e| e.into_inner()).get(&token).cloned()
}

#[no_mangle]
pub extern "C" fn solbreaker_physics_session_live(token: i32) -> bool {
    session(token).is_some()
}

#[no_mangle]
pub unsafe extern "C" fn initPlugin(ctx: *mut PluginContext) -> i32 {
    if ctx.is_null() || (*ctx).client.is_null() {
        return -1;
    }
    let Ok(token) = NEXT_TOKEN.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |v| v.checked_add(1))
    else { return -1 };
    let session = Arc::new(Session {
        client: (*ctx).client as usize,
        live: RwLock::new(true),
        issued: AtomicBool::new(false),
    });
    SESSIONS.lock().unwrap_or_else(|e| e.into_inner()).insert(token, session);
    (*ctx).user = Box::into_raw(Box::new(token)).cast();
    SHARED_MEMORY_MAGIC_NUMBER
}

#[no_mangle]
pub unsafe extern "C" fn executePluginCommand(ctx: *mut PluginContext, _: *const c_void) -> i32 {
    if ctx.is_null() || (*ctx).user.is_null() {
        return -1;
    }
    let token = *(*ctx).user.cast::<i32>();
    match session(token) {
        Some(value) if !value.issued.swap(true, Ordering::Relaxed) => token,
        _ => -1,
    }
}

#[no_mangle]
pub unsafe extern "C" fn exitPlugin(ctx: *mut PluginContext) {
    if ctx.is_null() || (*ctx).user.is_null() {
        return;
    }
    let token = Box::from_raw((*ctx).user.cast::<i32>());
    (*ctx).user = std::ptr::null_mut();
    if let Some(value) = SESSIONS.lock().unwrap_or_else(|e| e.into_inner()).remove(&*token) {
        *value.live.write().unwrap_or_else(|e| e.into_inner()) = false;
    }
}
