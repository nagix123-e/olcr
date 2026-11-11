//! macOS owns delivery after scheduling, including while OLCR is closed.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock, atomic::{AtomicU64, Ordering}};

static REVISION: AtomicU64 = AtomicU64::new(0);
static DESIRED: OnceLock<Mutex<BTreeMap<String, (u64, bool)>>> = OnceLock::new();
static OS_CHANGES: Mutex<()> = Mutex::new(());
fn is_current(id: &str, revision: u64, active: bool) -> bool {
    DESIRED.get_or_init(Mutex::default).lock().map(|state| state.get(id) == Some(&(revision, active))).unwrap_or(false)
}

#[derive(Deserialize)]
pub struct Timer {
    id: String,
    name: String,
    deadline: f64,
    #[serde(default)]
    kind: String,
}

#[derive(Serialize)]
pub struct Receipt {
    status: &'static str,
    scheduled_ids: Vec<String>,
    failed_ids: Vec<String>,
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancellation_supersedes_a_snapshot_waiting_for_permission() {
        let id = "revision-test";
        DESIRED.get_or_init(Mutex::default).lock().unwrap().insert(id.into(), (10, true));
        assert!(is_current(id, 10, true));
        DESIRED.get_or_init(Mutex::default).lock().unwrap().insert(id.into(), (11, false));
        assert!(!is_current(id, 10, true));
        assert!(is_current(id, 11, false));
        DESIRED.get_or_init(Mutex::default).lock().unwrap().insert(id.into(), (12, true));
        assert!(!is_current(id, 11, false));
        assert!(is_current(id, 12, true));
        DESIRED.get_or_init(Mutex::default).lock().unwrap().remove(id);
    }
    #[test]
    fn notification_identifiers_cannot_escape_the_timer_namespace() {
        assert!(valid_id("123-abc_def"));
        for id in ["", "../timer", "timer\nother", "timer with spaces"] { assert!(!valid_id(id)); }
    }
}

#[tauri::command]
pub async fn assistant_sync_notifications(timers: Vec<Timer>, cancelled_ids: Vec<String>, language: String) -> Result<Receipt, String> {
    if timers.len() > 100 || cancelled_ids.len() > 200 || timers.iter().any(|t| !valid_id(&t.id) || t.name.len() > 400 || !t.deadline.is_finite()) || cancelled_ids.iter().any(|id| !valid_id(id)) {
        return Err("ASSISTANT_NOTIFICATION_INPUT_INVALID".into());
    }
    let revision = REVISION.fetch_add(1, Ordering::SeqCst) + 1;
    {
        let mut state = DESIRED.get_or_init(Mutex::default).lock().map_err(|_| "ASSISTANT_NOTIFICATION_STATE_FAILED".to_owned())?;
        for timer in &timers { state.insert(timer.id.clone(), (revision, true)); }
        for id in &cancelled_ids { state.insert(id.clone(), (revision, false)); }
    }
    tauri::async_runtime::spawn_blocking(move || {
        let result = sync(timers, cancelled_ids, language, revision);
        if let Ok(mut state) = DESIRED.get_or_init(Mutex::default).lock() { state.retain(|_, (version, _)| *version != revision); }
        result
    })
        .await.map_err(|_| "ASSISTANT_NOTIFICATION_WORKER_FAILED".to_owned())?
}

#[cfg(target_os = "macos")]
fn sync(timers: Vec<Timer>, cancelled_ids: Vec<String>, language: String, revision: u64) -> Result<Receipt, String> {
    use block2::RcBlock;
    use objc2::rc::Retained;
    use objc2::runtime::{Bool, ProtocolObject};
    use objc2::{define_class, msg_send, AnyThread};
    use objc2_foundation::{NSArray, NSError, NSObject, NSObjectProtocol, NSString};
    use objc2_user_notifications::{UNAuthorizationOptions, UNMutableNotificationContent, UNNotification,
        UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationSound,
        UNTimeIntervalNotificationTrigger, UNUserNotificationCenter, UNUserNotificationCenterDelegate};
    use std::sync::{mpsc, Once};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    define_class!(
        #[unsafe(super(NSObject))]
        #[name = "OLCRTimerNotificationDelegate"]
        struct Delegate;
        unsafe impl NSObjectProtocol for Delegate {}
        unsafe impl UNUserNotificationCenterDelegate for Delegate {
            #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
            fn present(&self, _center: &UNUserNotificationCenter, _notification: &UNNotification,
                       completion: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>) {
                completion.call((UNNotificationPresentationOptions::Banner | UNNotificationPresentationOptions::Sound | UNNotificationPresentationOptions::List,));
            }
        }
    );
    static DELEGATE: Once = Once::new();
    let center = UNUserNotificationCenter::currentNotificationCenter();
    DELEGATE.call_once(|| {
        let delegate: Retained<Delegate> = unsafe { msg_send![Delegate::alloc(), init] };
        center.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        // UNUserNotificationCenter keeps a weak delegate. Retain it for the app lifetime.
        let _ = Retained::into_raw(delegate);
    });
    {
        let _change = OS_CHANGES.lock().map_err(|_| "ASSISTANT_NOTIFICATION_STATE_FAILED".to_owned())?;
        let cancellation: Vec<_> = cancelled_ids.iter().filter(|id| is_current(id, revision, false)).map(|id| NSString::from_str(&format!("olcr.timer.{id}"))).collect();
        if !cancellation.is_empty() {
            let ids = NSArray::from_retained_slice(&cancellation);
            center.removePendingNotificationRequestsWithIdentifiers(&ids);
            center.removeDeliveredNotificationsWithIdentifiers(&ids);
        }
    }
    if timers.is_empty() {
        return Ok(Receipt { status: "scheduled", scheduled_ids: vec![], failed_ids: vec![] });
    }
    let (sender, receiver) = mpsc::channel();
    let completion = RcBlock::new(move |granted: Bool, error: *mut NSError| {
        let _ = sender.send(granted.as_bool() && error.is_null());
    });
    center.requestAuthorizationWithOptions_completionHandler(UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound, &completion);
    let granted = receiver.recv_timeout(Duration::from_secs(120)).map_err(|_| "ASSISTANT_NOTIFICATION_PERMISSION_TIMEOUT".to_owned())?;
    if !granted {
        return Ok(Receipt { status: "denied", scheduled_ids: vec![], failed_ids: timers.into_iter().map(|t| t.id).collect() });
    }
    let mut scheduled_ids = vec![];
    let mut failed_ids = vec![];
    for timer in timers {
        let _change = OS_CHANGES.lock().map_err(|_| "ASSISTANT_NOTIFICATION_STATE_FAILED".to_owned())?;
        // A cancellation/resume received while macOS was asking for permission
        // supersedes this snapshot and must not be overwritten by a late worker.
        if !is_current(&timer.id, revision, true) { continue; }
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "ASSISTANT_NOTIFICATION_CLOCK_INVALID".to_owned())?.as_secs_f64();
        let remaining = timer.deadline - now;
        if remaining > 604801.0 || remaining < -30.0 {
            failed_ids.push(timer.id);
            continue;
        }
        let content = UNMutableNotificationContent::new();
        content.setTitle(&NSString::from_str(if timer.name.is_empty() { "OLCR" } else { &timer.name }));
        content.setBody(&NSString::from_str(if timer.kind == "alarm" { if language == "ja" { "設定したアラームの時刻です。" } else { "It is time for your alarm." } } else if language == "ja" { "タイマーの時間です。" } else { "Your timer has finished." }));
        content.setSound(Some(&UNNotificationSound::defaultSound()));
        let trigger = UNTimeIntervalNotificationTrigger::triggerWithTimeInterval_repeats(remaining.max(0.1), false);
        let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
            &NSString::from_str(&format!("olcr.timer.{}", timer.id)), &content, Some(&trigger));
        let (sender, receiver) = mpsc::channel();
        let completion = RcBlock::new(move |error: *mut NSError| { let _ = sender.send(error.is_null()); });
        center.addNotificationRequest_withCompletionHandler(&request, Some(&completion));
        if receiver.recv_timeout(Duration::from_secs(10)).unwrap_or(false) {
            scheduled_ids.push(timer.id);
        } else {
            failed_ids.push(timer.id);
        }
    }
    Ok(Receipt { status: if failed_ids.is_empty() { "scheduled" } else { "failed" }, scheduled_ids, failed_ids })
}

#[cfg(not(target_os = "macos"))]
fn sync(timers: Vec<Timer>, _cancelled_ids: Vec<String>, _language: String, _revision: u64) -> Result<Receipt, String> {
    Ok(Receipt { status: "unavailable", scheduled_ids: vec![], failed_ids: timers.into_iter().map(|t| t.id).collect() })
}
