//! Explicitly enabled human webcam input. Does not invoke the agent action router.
use rand::{distributions::Alphanumeric, Rng};
use serde::Deserialize;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Deserialize)]
pub struct HumanEvent {
    kind: String,
    x: Option<f64>,
    y: Option<f64>,
    delta: Option<i32>,
}

struct Session {
    token: String,
    sequence: u64,
    updated: Instant,
    point: [f64; 2],
    bounds: [f64; 4],
    down: bool,
}

#[derive(Default)]
pub struct HumanInputManager(Mutex<Option<Session>>);

impl HumanInputManager {
    fn stop(&self, token: &str) {
        let mut state = self.0.lock().unwrap();
        if state.as_ref().is_some_and(|s| s.token == token) {
            if let Some(s) = state.as_mut() {
                release(s);
            }
            *state = None;
        }
    }
}

impl Drop for HumanInputManager {
    fn drop(&mut self) {
        if let Ok(state) = self.0.get_mut() {
            if let Some(s) = state.as_mut() {
                release(s);
            }
        }
    }
}

#[tauri::command]
pub fn begin_human_input(
    state: tauri::State<'_, Arc<HumanInputManager>>,
) -> Result<String, String> {
    if !crate::accessibility::is_trusted()
        || !crate::accessibility::pointer_event_permission_status()
    {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    let (point, bounds) = display_geometry()?;
    let token: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    let mut inner = state.0.lock().unwrap();
    if let Some(old) = inner.as_mut() {
        release(old);
    }
    *inner = Some(Session {
        token: token.clone(),
        sequence: 0,
        updated: Instant::now(),
        point,
        bounds,
        down: false,
    });
    drop(inner);
    let manager = Arc::clone(state.inner());
    let lease = token.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(100));
        let mut inner = manager.0.lock().unwrap();
        let Some(s) = inner.as_mut() else { break };
        if s.token != lease {
            break;
        }
        if s.updated.elapsed() > Duration::from_millis(1200) {
            release(s);
            *inner = None;
            break;
        }
    });
    Ok(token)
}

#[tauri::command]
pub fn end_human_input(state: tauri::State<'_, Arc<HumanInputManager>>, token: String) {
    state.stop(&token);
}

fn valid_frame(sequence: u64, last: u64, captured_at: f64, now: f64) -> bool {
    sequence > last && captured_at.is_finite() && now >= captured_at && now - captured_at < 800.
}

#[tauri::command]
pub fn apply_human_input(
    state: tauri::State<'_, Arc<HumanInputManager>>,
    token: String,
    sequence: u64,
    captured_at: f64,
    events: Vec<HumanEvent>,
) -> Result<(), String> {
    let mut inner = state.0.lock().unwrap();
    let s = inner.as_mut().ok_or("HANDSFREE_INPUT_OFF")?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "HANDSFREE_CLOCK_INVALID")?
        .as_secs_f64()
        * 1000.;
    if s.token != token || !valid_frame(sequence, s.sequence, captured_at, now) || events.len() > 5
    {
        release(s);
        return Err("HANDSFREE_INPUT_STALE".into());
    }
    if !crate::accessibility::pointer_event_permission_status() {
        release(s);
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    // Validate the entire batch before posting any events.
    for e in &events {
        if !matches!(e.kind.as_str(), "move" | "click" | "down" | "up" | "scroll")
            || (e.kind == "move"
                && !e.x.zip(e.y).is_some_and(|(x, y)| {
                    x.is_finite()
                        && y.is_finite()
                        && (0.0..=1.0).contains(&x)
                        && (0.0..=1.0).contains(&y)
                }))
            || (e.kind == "scroll" && !e.delta.is_some_and(|n| (-12..=12).contains(&n)))
        {
            release(s);
            return Err("HANDSFREE_EVENT_INVALID".into());
        }
    }
    s.sequence = sequence;
    s.updated = Instant::now();
    for e in events {
        match e.kind.as_str() {
            "move" => {
                s.point = [
                    s.bounds[0] + e.x.unwrap() * (s.bounds[2] - 1.),
                    s.bounds[1] + e.y.unwrap() * (s.bounds[3] - 1.),
                ];
                post(if s.down { 6 } else { 5 }, s.point, 0)?;
            }
            "down" => {
                if !s.down {
                    post(1, s.point, 0)?;
                    s.down = true;
                }
            }
            "up" => release(s),
            "click" => {
                if !s.down {
                    post(1, s.point, 0)?;
                    post(2, s.point, 0)?;
                }
            }
            "scroll" => post(22, s.point, e.delta.unwrap())?,
            _ => unreachable!(),
        }
    }
    Ok(())
}

fn release(s: &mut Session) {
    if s.down {
        let _ = post(2, s.point, 0);
        s.down = false;
    }
}

#[cfg(target_os = "macos")]
mod quartz {
    use std::ffi::c_void;
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct Point {
        pub x: f64,
        pub y: f64,
    }
    #[repr(C)]
    pub struct Size {
        pub width: f64,
        pub height: f64,
    }
    #[repr(C)]
    pub struct Rect {
        pub origin: Point,
        pub size: Size,
    }
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        pub fn CGEventCreate(source: *const c_void) -> *mut c_void;
        pub fn CGEventGetLocation(event: *mut c_void) -> Point;
        pub fn CGEventCreateMouseEvent(
            source: *const c_void,
            kind: u32,
            point: Point,
            button: u32,
        ) -> *mut c_void;
        pub fn CGEventCreateScrollWheelEvent(
            source: *const c_void,
            units: u32,
            count: u32,
            wheel1: i32,
            wheel2: i32,
            wheel3: i32,
        ) -> *mut c_void;
        pub fn CGEventSetLocation(event: *mut c_void, point: Point);
        pub fn CGEventPost(tap: u32, event: *mut c_void);
        pub fn CGDisplayBounds(id: u32) -> Rect;
        pub fn CGMainDisplayID() -> u32;
        pub fn CGGetDisplaysWithPoint(
            point: Point,
            max: u32,
            ids: *mut u32,
            count: *mut u32,
        ) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        pub fn CFRelease(value: *const c_void);
    }
}

fn display_geometry() -> Result<([f64; 2], [f64; 4]), String> {
    #[cfg(target_os = "macos")]
    unsafe {
        use quartz::*;
        let event = CGEventCreate(std::ptr::null());
        if event.is_null() {
            return Err("HANDSFREE_POINTER_UNAVAILABLE".into());
        }
        let p = CGEventGetLocation(event);
        CFRelease(event);
        let mut id = CGMainDisplayID();
        let mut count = 0;
        CGGetDisplaysWithPoint(p, 1, &mut id, &mut count);
        let r = CGDisplayBounds(id);
        return Ok((
            [p.x, p.y],
            [r.origin.x, r.origin.y, r.size.width, r.size.height],
        ));
    }
    #[cfg(not(target_os = "macos"))]
    Err("HANDSFREE_INPUT_UNSUPPORTED_PLATFORM".into())
}

fn post(kind: u32, point: [f64; 2], delta: i32) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    unsafe {
        use quartz::*;
        let p = Point {
            x: point[0],
            y: point[1],
        };
        let event = if kind == 22 {
            CGEventCreateScrollWheelEvent(std::ptr::null(), 1, 1, delta, 0, 0)
        } else {
            CGEventCreateMouseEvent(std::ptr::null(), kind, p, 0)
        };
        if event.is_null() {
            return Err("HANDSFREE_EVENT_CREATE_FAILED".into());
        }
        if kind == 22 {
            CGEventSetLocation(event, p);
        }
        CGEventPost(0, event);
        CFRelease(event);
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (kind, point, delta);
        Err("HANDSFREE_INPUT_UNSUPPORTED_PLATFORM".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_replayed_future_and_stale_camera_frames() {
        assert!(valid_frame(2, 1, 1000., 1200.));
        assert!(!valid_frame(1, 1, 1000., 1200.));
        assert!(!valid_frame(2, 1, 1300., 1200.));
        assert!(!valid_frame(2, 1, 1000., 1800.));
        assert!(!valid_frame(2, 1, f64::NAN, 1200.));
    }
}
