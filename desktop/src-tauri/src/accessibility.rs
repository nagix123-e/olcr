//! Bounded, read-only snapshots of explicitly selected macOS Accessibility trees.
//!
//! Permission prompts happen only after the Settings control explicitly calls
//! `request_permission`; ordinary status checks never show an OS prompt.

use serde::Serialize;
use std::collections::VecDeque;
use std::ffi::c_void;
#[cfg(target_os = "macos")]
use std::sync::OnceLock;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc, Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

const MAX_DEPTH: usize = 8;
const MAX_ELEMENTS: usize = 120;
// Leave room for action-request metadata and Python JSON whitespace under the
// backend's 24 KB observation limit. Window controls precede menu entries.
const MAX_OBSERVATION_BYTES: usize = 18_000;
const MAX_VISITED_NODES: usize = 160;
const MAX_CHILDREN_PER_ELEMENT: usize = 32;
const MAX_LABEL_CHARS: usize = 180;
const SNAPSHOT_BUDGET: Duration = Duration::from_secs(3);
const ACTION_STATE_MAX_AGE: Duration = Duration::from_secs(30);

fn is_perception_notification(notification: &str) -> bool {
    matches!(
        notification,
        "AXFocusedUIElementChanged"
            | "AXFocusedWindowChanged"
            | "AXMainWindowChanged"
            | "AXWindowCreated"
            | "AXWindowMoved"
            | "AXWindowResized"
            | "AXValueChanged"
            | "AXSelectedChildrenChanged"
            | "AXSelectedTextChanged"
    )
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityElement {
    pub id: String,
    pub role: String,
    pub subrole: Option<String>,
    pub label: String,
    pub frame: Option<AccessibilityRect>,
    pub visible: Option<bool>,
    pub enabled: Option<bool>,
    pub focused: Option<bool>,
    pub selected: Option<bool>,
    pub expanded: Option<bool>,
    /// Numeric AXValue is exposed only for AXScrollBar, never for text controls.
    pub scroll_value: Option<f64>,
    pub supported_actions: Vec<String>,
    pub keyboard_shortcut: Option<KeyboardShortcut>,
    pub depth: usize,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KeyboardShortcut {
    pub key_code: u16,
    pub modifiers: u32,
    pub key: String,
    pub provenance: &'static str,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityActionResult {
    pub status: &'static str,
    pub verified: bool,
    pub execution_attempted: bool,
    pub action: &'static str,
    pub observation: AccessibilityObservation,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityShortcutResult {
    pub status: &'static str,
    pub verified: bool,
    pub execution_attempted: bool,
    pub shortcut: KeyboardShortcut,
    pub observation: AccessibilityObservation,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityTextEntryResult {
    pub status: &'static str,
    pub verified: bool,
    pub execution_attempted: bool,
    pub value_matched: bool,
    pub observation: AccessibilityObservation,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityKeyResult {
    pub status: &'static str,
    pub verified: bool,
    pub execution_attempted: bool,
    pub key: &'static str,
    pub observation: AccessibilityObservation,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityScrollResult {
    pub status: &'static str,
    pub execution_attempted: bool,
    pub direction: &'static str,
    pub observation: AccessibilityObservation,
}

fn text_entry_role_allowed(role: &str, subrole: Option<&str>) -> bool {
    matches!(role, "AXTextField" | "AXTextArea") && subrole != Some("AXSecureTextField")
}

fn tab_focus_role_allowed(role: &str) -> bool {
    matches!(
        role,
        "AXButton"
            | "AXTextField"
            | "AXTextArea"
            | "AXCheckBox"
            | "AXRadioButton"
            | "AXPopUpButton"
            | "AXLink"
            | "AXComboBox"
            | "AXSlider"
            | "AXIncrementor"
            | "AXTab"
    )
}

fn bounded_chat_key(key: &str) -> Option<(&'static str, u16)> {
    match key {
        "Enter" => Some(("Enter", 36)),
        "Escape" => Some(("Escape", 53)),
        "Space" => Some(("Space", 49)),
        "ArrowLeft" => Some(("ArrowLeft", 123)),
        "ArrowRight" => Some(("ArrowRight", 124)),
        "ArrowDown" => Some(("ArrowDown", 125)),
        "ArrowUp" => Some(("ArrowUp", 126)),
        _ => None,
    }
}

fn keypress_focus_role_allowed(role: &str, subrole: Option<&str>) -> bool {
    subrole != Some("AXSecureTextField")
        && (tab_focus_role_allowed(role)
            || matches!(role, "AXGroup" | "AXWindow" | "AXWebArea" | "AXCanvas"))
}

fn keypress_target_enabled(role: &str, enabled: Option<bool>) -> bool {
    enabled == Some(true)
        || (matches!(role, "AXGroup" | "AXWindow" | "AXWebArea" | "AXCanvas")
            && enabled != Some(false))
}

#[cfg(target_os = "macos")]
struct CachedAXElement {
    id: String,
    role: String,
    subrole: Option<String>,
    label: String,
    visible: Option<bool>,
    enabled: Option<bool>,
    focused: Option<bool>,
    expanded: Option<bool>,
    scroll_value: Option<f64>,
    depth: usize,
    frame: Option<(f64, f64, f64, f64)>,
    supported_actions: Vec<String>,
    keyboard_shortcut: Option<KeyboardShortcut>,
    element: axuielement::AXUIElement,
}

#[cfg(target_os = "macos")]
struct CachedAXSnapshot {
    observation_id: String,
    session_id: String,
    application_pid: i32,
    application_bundle_id: Option<String>,
    observed_at: f64,
    window_frame: Option<(f64, f64, f64, f64)>,
    elements: Vec<CachedAXElement>,
}

#[cfg(target_os = "macos")]
static ACTION_SNAPSHOTS: OnceLock<Mutex<VecDeque<CachedAXSnapshot>>> = OnceLock::new();

pub fn press_fresh_disclosure(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<AccessibilityActionResult, String> {
    #[cfg(target_os = "macos")]
    {
        press_fresh_disclosure_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Dispatch one freshly observed, explicitly labelled AXButton press.
/// Effect verification remains in the existing backend SemanticActionRuntime.
pub fn press_fresh_button(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<AccessibilityActionResult, String> {
    #[cfg(target_os = "macos")]
    {
        press_fresh_button_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Replace the value of one uniquely identified, already-focused, non-secure
/// editable control. Input text is never returned in the result or observation.
pub fn enter_fresh_text(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    input_text: String,
) -> Result<AccessibilityTextEntryResult, String> {
    if input_text.is_empty()
        || input_text.chars().count() > 2_048
        || input_text
            .chars()
            .any(|character| character < ' ' && character != '\n')
    {
        return Err("ACCESSIBILITY_TEXT_INPUT_INVALID".into());
    }
    #[cfg(target_os = "macos")]
    {
        enter_fresh_text_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
            &input_text,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            input_text,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn press_fresh_tab(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    current_focus_element_id: String,
    current_focus_label: String,
    target_element_id: String,
    target_label: String,
) -> Result<AccessibilityKeyResult, String> {
    #[cfg(target_os = "macos")]
    {
        press_fresh_tab_macos(
            &observation_id,
            &session_id,
            application_pid,
            &current_focus_element_id,
            &current_focus_label,
            &target_element_id,
            &target_label,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            current_focus_element_id,
            current_focus_label,
            target_element_id,
            target_label,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn press_fresh_key(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    key: String,
) -> Result<AccessibilityKeyResult, String> {
    #[cfg(target_os = "macos")]
    {
        press_fresh_key_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
            &key,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            key,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn scroll_fresh_area(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    area_element_id: String,
    area_label: String,
    scrollbar_element_id: String,
    before_scroll_value: f64,
    direction: String,
) -> Result<AccessibilityScrollResult, String> {
    if !matches!(direction.as_str(), "up" | "down")
        || !before_scroll_value.is_finite()
        || !(0.0..=1.0).contains(&before_scroll_value)
    {
        return Err("ACCESSIBILITY_SCROLL_INPUT_INVALID".into());
    }
    #[cfg(target_os = "macos")]
    {
        scroll_fresh_area_macos(
            &observation_id,
            &session_id,
            application_pid,
            &area_element_id,
            &area_label,
            &scrollbar_element_id,
            before_scroll_value,
            &direction,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            area_element_id,
            area_label,
            scrollbar_element_id,
            before_scroll_value,
            direction,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Use the fresh AX box only when the disclosure does not expose AXPress.
/// This is a fallback for the same reversible, closed-disclosure action.
pub fn click_fresh_disclosure(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<AccessibilityActionResult, String> {
    #[cfg(target_os = "macos")]
    {
        click_fresh_disclosure_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
            None,
            false,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Click a UI-Venus/LK-grounded point only for the same closed AX disclosure
/// selected from the fresh observation. The caller must independently validate
/// the point against the active display and target window immediately beforehand.
pub fn click_vision_grounded_disclosure(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    global_point: [f64; 2],
) -> Result<AccessibilityActionResult, String> {
    #[cfg(target_os = "macos")]
    {
        click_fresh_disclosure_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
            Some((global_point[0], global_point[1])),
            false,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            global_point,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Click one fresh AXButton only when AXPress is unavailable and the caller
/// supplies a point freshly aligned to the active target application's window.
pub fn click_vision_grounded_button(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    global_point: [f64; 2],
) -> Result<AccessibilityActionResult, String> {
    #[cfg(target_os = "macos")]
    {
        click_fresh_disclosure_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
            Some((global_point[0], global_point[1])),
            true,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            global_point,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn activate_find_shortcut(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<AccessibilityShortcutResult, String> {
    #[cfg(target_os = "macos")]
    {
        activate_find_shortcut_macos(
            &observation_id,
            &session_id,
            application_pid,
            &element_id,
            &label,
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

#[derive(Debug, PartialEq, Eq)]
struct KeyEventSpec {
    key_code: u16,
    key_down: bool,
    flags: u64,
}

fn find_shortcut_events(key_code: u16, modifiers: u32) -> Result<Vec<KeyEventSpec>, String> {
    if key_code > 127 || modifiers > 15 || (modifiers & 8 != 0 && modifiers & 7 != 0) {
        return Err("SHORTCUT_METADATA_INVALID".into());
    }
    const SHIFT: u64 = 1 << 17;
    const CONTROL: u64 = 1 << 18;
    const OPTION: u64 = 1 << 19;
    const COMMAND: u64 = 1 << 20;
    let mut held = Vec::new();
    if modifiers & 4 != 0 {
        held.push((59, CONTROL));
    }
    if modifiers & 2 != 0 {
        held.push((58, OPTION));
    }
    if modifiers & 1 != 0 {
        held.push((56, SHIFT));
    }
    if modifiers & 8 == 0 {
        held.push((55, COMMAND));
    }
    let mut flags = 0;
    let mut events = Vec::with_capacity(held.len() * 2 + 2);
    for (key, flag) in &held {
        flags |= flag;
        events.push(KeyEventSpec {
            key_code: *key,
            key_down: true,
            flags,
        });
    }
    events.push(KeyEventSpec {
        key_code,
        key_down: true,
        flags,
    });
    events.push(KeyEventSpec {
        key_code,
        key_down: false,
        flags,
    });
    for (key, flag) in held.iter().rev() {
        flags &= !flag;
        events.push(KeyEventSpec {
            key_code: *key,
            key_down: false,
            flags,
        });
    }
    Ok(events)
}

fn safe_find_menu_label(label: &str) -> bool {
    let normalized = label
        .trim()
        .trim_end_matches(|character| character == '…' || character == '.')
        .trim()
        .to_lowercase();
    matches!(normalized.as_str(), "find" | "search" | "検索")
}

fn disclosure_press_is_allowed(
    role: &str,
    enabled: Option<bool>,
    expanded: Option<bool>,
    has_frame: bool,
    actions: &[String],
) -> bool {
    role == "AXDisclosureTriangle"
        && has_frame
        && enabled == Some(true)
        && expanded == Some(false)
        && actions.iter().any(|action| action == "AXPress")
}

fn disclosure_pointer_is_allowed(
    role: &str,
    enabled: Option<bool>,
    expanded: Option<bool>,
    frame: Option<(f64, f64, f64, f64)>,
    actions: &[String],
) -> bool {
    role == "AXDisclosureTriangle"
        && enabled == Some(true)
        && expanded == Some(false)
        && !actions.iter().any(|action| action == "AXPress")
        && frame.is_some_and(|frame| valid_pointer_frame(&frame))
}

fn disclosure_vision_pointer_is_allowed(
    role: &str,
    enabled: Option<bool>,
    expanded: Option<bool>,
    actions: &[String],
    point: (f64, f64),
) -> bool {
    role == "AXDisclosureTriangle"
        && enabled == Some(true)
        && expanded == Some(false)
        && !actions.iter().any(|action| action == "AXPress")
        && point.0.is_finite()
        && point.1.is_finite()
        && point.0.abs() <= 100_000.0
        && point.1.abs() <= 100_000.0
}

fn button_vision_pointer_is_allowed(
    role: &str,
    enabled: Option<bool>,
    actions: &[String],
    point: (f64, f64),
) -> bool {
    role == "AXButton"
        && enabled == Some(true)
        && !actions.iter().any(|action| action == "AXPress")
        && point.0.is_finite()
        && point.1.is_finite()
        && point.0.abs() <= 100_000.0
        && point.1.abs() <= 100_000.0
}

fn unique_fresh_vision_button_id(
    elements: &[AccessibilityElement],
    label: &str,
) -> Result<String, String> {
    let matches = elements
        .iter()
        .filter(|element| {
            element.role == "AXButton"
                && element.label == label
                && element.visible == Some(true)
                && element.enabled == Some(true)
        })
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return Err("VISION_POINTER_FRESH_TARGET_NOT_UNIQUE".into());
    }
    if matches[0]
        .supported_actions
        .iter()
        .any(|action| action == "AXPress")
    {
        return Err("ACCESSIBILITY_AXPRESS_AVAILABLE_USE_SEMANTIC_ACTION".into());
    }
    Ok(matches[0].id.clone())
}

fn valid_pointer_frame(frame: &(f64, f64, f64, f64)) -> bool {
    let (x, y, width, height) = *frame;
    x.is_finite()
        && y.is_finite()
        && width.is_finite()
        && height.is_finite()
        && x.abs() <= 100_000.0
        && y.abs() <= 100_000.0
        && width > 0.0
        && height > 0.0
        && width <= 32_768.0
        && height <= 32_768.0
        && (x + width / 2.0).is_finite()
        && (y + height / 2.0).is_finite()
}

fn usable_pointer_frame(frame: Option<(f64, f64, f64, f64)>) -> Option<(f64, f64, f64, f64)> {
    frame.filter(valid_pointer_frame)
}

fn tracked_point_is_inside_live_button_frame(
    point: (f64, f64),
    frame: (f64, f64, f64, f64),
) -> bool {
    valid_pointer_frame(&frame)
        && point_is_inside_display_bounds(point, (frame.0, frame.1), (frame.2, frame.3))
}

fn pointer_frame_center(frame: (f64, f64, f64, f64)) -> Result<(f64, f64), &'static str> {
    if !valid_pointer_frame(&frame) {
        return Err("ACCESSIBILITY_TARGET_FRAME_INVALID");
    }
    Ok((frame.0 + frame.2 / 2.0, frame.1 + frame.3 / 2.0))
}

fn point_is_inside_display_bounds(point: (f64, f64), origin: (f64, f64), size: (f64, f64)) -> bool {
    point.0.is_finite()
        && point.1.is_finite()
        && origin.0.is_finite()
        && origin.1.is_finite()
        && size.0.is_finite()
        && size.1.is_finite()
        && size.0 > 0.0
        && size.1 > 0.0
        && point.0 >= origin.0
        && point.0 < origin.0 + size.0
        && point.1 >= origin.1
        && point.1 < origin.1 + size.1
}

fn point_is_inside_accessibility_rect(point: (f64, f64), rect: &AccessibilityRect) -> bool {
    valid_pointer_frame(&(rect.x, rect.y, rect.width, rect.height))
        && point_is_inside_display_bounds(point, (rect.x, rect.y), (rect.width, rect.height))
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityObservation {
    pub source: &'static str,
    pub permission_granted: bool,
    pub observation_id: String,
    pub observed_at: f64,
    pub session_id: String,
    pub coordinate_space: &'static str,
    pub width: Option<usize>,
    pub height: Option<usize>,
    pub confidence: f64,
    pub text: Vec<String>,
    pub application_pid: Option<i32>,
    pub application_bundle_id: Option<String>,
    pub window_title: Option<String>,
    pub window_frame: Option<AccessibilityRect>,
    pub elements: Vec<AccessibilityElement>,
    pub truncated: bool,
    pub uncertainty: Vec<String>,
}

fn observed_element_visibility(
    hidden: Option<bool>,
    role: &str,
    frame: Option<&AccessibilityRect>,
    window: Option<&AccessibilityRect>,
    window_minimized: Option<bool>,
) -> Option<bool> {
    if let Some(hidden) = hidden {
        return Some(!hidden);
    }
    // Controls often omit AXHidden. Infer visibility only for known interactive
    // controls fully inside a known non-minimized content window, never for
    // menu tails or text outside the target window.
    if !matches!(
        role,
        "AXButton" | "AXDisclosureTriangle" | "AXTextField" | "AXTextArea"
    ) || window_minimized != Some(false)
    {
        return None;
    }
    let (frame, window) = frame.zip(window)?;
    if !valid_pointer_frame(&(frame.x, frame.y, frame.width, frame.height))
        || !valid_pointer_frame(&(window.x, window.y, window.width, window.height))
    {
        return None;
    }
    (frame.x >= window.x
        && frame.y >= window.y
        && frame.x + frame.width <= window.x + window.width
        && frame.y + frame.height <= window.y + window.height)
        .then_some(true)
}

fn bound_observation_bytes(observation: &mut AccessibilityObservation) {
    // Preserve the prefix observed from the target window and prune only the
    // menu tail. Native cached handles are trimmed to the identical prefix.
    while serde_json::to_vec(observation).map_or(true, |bytes| bytes.len() > MAX_OBSERVATION_BYTES)
    {
        if observation.elements.pop().is_none() {
            break;
        }
        observation.text.pop();
        observation.truncated = true;
        if !observation
            .uncertainty
            .iter()
            .any(|item| item == "AX_SNAPSHOT_BYTE_BUDGET_REACHED")
        {
            observation
                .uncertainty
                .push("AX_SNAPSHOT_BYTE_BUDGET_REACHED".into());
        }
    }
}

pub fn is_trusted() -> bool {
    #[cfg(target_os = "macos")]
    {
        axuielement::is_process_trusted()
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

pub fn request_permission() -> bool {
    #[cfg(target_os = "macos")]
    {
        axuielement::is_process_trusted_with_prompt()
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

pub fn pointer_event_permission_status() -> bool {
    #[cfg(target_os = "macos")]
    {
        // SAFETY: This is the non-prompting Core Graphics permission preflight.
        unsafe { CGPreflightPostEventAccess() }
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

pub fn request_pointer_event_permission() -> bool {
    #[cfg(target_os = "macos")]
    {
        // Called only from the explicit Settings control, never during planning/action.
        unsafe { CGRequestPostEventAccess() }
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

pub fn observe_focused() -> Result<AccessibilityObservation, String> {
    #[cfg(target_os = "macos")]
    {
        observe_macos(None)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RunningAccessibilityApplication {
    pub application_pid: i32,
    pub application_name: String,
    pub application_bundle_id: String,
    pub application_path: Option<String>,
}

pub fn list_running_applications() -> Result<Vec<RunningAccessibilityApplication>, String> {
    #[cfg(target_os = "macos")]
    {
        // Bundle IDs and localized app names come from NSWorkspace's process
        // inventory; they do not require reading an Accessibility tree. Keep
        // this inventory available so Settings can define Computer Use scope
        // before the user grants AX permission for observation/actions.
        use objc2_app_kit::{NSApplicationActivationPolicy, NSWorkspace};
        let applications = NSWorkspace::sharedWorkspace().runningApplications();
        let mut output = Vec::new();
        for index in 0..applications.count() {
            let application = applications.objectAtIndex(index);
            if application.isTerminated() {
                continue;
            }
            if application.activationPolicy() != NSApplicationActivationPolicy::Regular {
                continue;
            }
            let application_pid = application.processIdentifier();
            if application_pid <= 0 || application_pid == std::process::id() as i32 {
                continue;
            }
            let Some(bundle_id) = application
                .bundleIdentifier()
                .map(|value| value.to_string())
            else {
                continue;
            };
            if bundle_id == "local.olcr.desktop" {
                continue;
            }
            let Some(name) = application.localizedName().map(|value| value.to_string()) else {
                continue;
            };
            output.push(RunningAccessibilityApplication {
                application_pid,
                application_name: name,
                application_bundle_id: bundle_id,
                application_path: application
                    .bundleURL()
                    .and_then(|url| url.path())
                    .map(|path| path.to_string())
                    .map(|path| {
                        std::path::Path::new(&path)
                            .canonicalize()
                            .map(|canonical| canonical.to_string_lossy().into_owned())
                            .unwrap_or(path)
                    }),
            });
        }
        output.sort_by(|left, right| {
            left.application_name
                .to_lowercase()
                .cmp(&right.application_name.to_lowercase())
        });
        Ok(output)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Read Launch Services metadata for scoped applications without launching them.
pub fn list_installed_applications(
    bundle_ids: Vec<String>,
) -> Result<Vec<RunningAccessibilityApplication>, String> {
    if bundle_ids.len() > 128 {
        return Err("COMPUTER_USE_APP_SCOPE_TOO_LARGE".into());
    }
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::NSWorkspace;
        use objc2_foundation::NSString;
        let workspace = NSWorkspace::sharedWorkspace();
        let mut applications = Vec::new();
        for id in bundle_ids {
            if id.is_empty()
                || id.len() > 256
                || id.contains(['\r', '\n'])
                || id == "local.olcr.desktop"
            {
                continue;
            }
            let Some(url) =
                workspace.URLForApplicationWithBundleIdentifier(&NSString::from_str(&id))
            else {
                continue;
            };
            let Some(path) = url.path().map(|p| p.to_string()) else {
                continue;
            };
            let path = validated_application_bundle_path(&id, &path)?;
            let plist = path.join("Contents/Info.plist");
            let name = ["CFBundleDisplayName", "CFBundleName"]
                .into_iter()
                .find_map(|key| {
                    let output = std::process::Command::new("/usr/libexec/PlistBuddy")
                        .args(["-c", &format!("Print :{key}")])
                        .arg(&plist)
                        .output()
                        .ok()?;
                    if !output.status.success() {
                        return None;
                    }
                    let value = String::from_utf8(output.stdout).ok()?.trim().to_string();
                    (!value.is_empty()).then_some(value)
                })
                .unwrap_or_else(|| {
                    path.file_stem()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned()
                });
            applications.push(RunningAccessibilityApplication {
                application_pid: 0,
                application_name: name,
                application_bundle_id: id,
                application_path: Some(path.to_string_lossy().into_owned()),
            });
        }
        Ok(applications)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = bundle_ids;
        Err("COMPUTER_USE_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn launch_application_by_bundle_id(application_bundle_id: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::NSWorkspace;
        use objc2_foundation::NSString;

        let bundle_id = application_bundle_id.trim();
        if bundle_id.is_empty()
            || bundle_id.len() > 256
            || bundle_id.contains('\r')
            || bundle_id.contains('\n')
        {
            return Err("COMPUTER_USE_APPLICATION_BUNDLE_ID_INVALID".into());
        }
        if bundle_id == "local.olcr.desktop" {
            return Err("COMPUTER_USE_CANNOT_TARGET_OLCR".into());
        }
        let workspace = NSWorkspace::sharedWorkspace();
        let bundle_id = NSString::from_str(bundle_id);
        let application_url = workspace
            .URLForApplicationWithBundleIdentifier(&bundle_id)
            .ok_or_else(|| "COMPUTER_USE_APPLICATION_NOT_INSTALLED".to_string())?;
        if !workspace.openURL(&application_url) {
            return Err("COMPUTER_USE_APPLICATION_LAUNCH_FAILED".into());
        }
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = application_bundle_id;
        Err("COMPUTER_USE_UNSUPPORTED_PLATFORM".into())
    }
}

#[cfg(target_os = "macos")]
fn validated_application_bundle_path(
    application_bundle_id: &str,
    application_path: &str,
) -> Result<std::path::PathBuf, String> {
    use std::path::Path;
    use std::process::Command;

    let bundle_id = application_bundle_id.trim();
    let raw_path = application_path.trim();
    if bundle_id.is_empty()
        || bundle_id.len() > 256
        || bundle_id.contains(['\r', '\n'])
        || bundle_id == "local.olcr.desktop"
        || raw_path.is_empty()
        || raw_path.len() > 4096
        || raw_path.contains(['\r', '\n'])
    {
        return Err("COMPUTER_USE_APPLICATION_BUNDLE_INVALID".into());
    }
    let bundle_path = Path::new(raw_path)
        .canonicalize()
        .map_err(|_| "COMPUTER_USE_APP_BUNDLE_INVALID".to_string())?;
    if !bundle_path.is_dir() || bundle_path.extension().is_none_or(|value| value != "app") {
        return Err("COMPUTER_USE_APP_BUNDLE_INVALID".into());
    }
    let info_plist = bundle_path.join("Contents/Info.plist");
    let output = Command::new("/usr/bin/plutil")
        .args(["-extract", "CFBundleIdentifier", "raw", "-o", "-"])
        .arg(&info_plist)
        .output()
        .map_err(|_| "COMPUTER_USE_APP_BUNDLE_INVALID".to_string())?;
    let resolved_bundle_id = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if !output.status.success() || resolved_bundle_id != bundle_id {
        return Err("COMPUTER_USE_APP_BUNDLE_ID_MISMATCH".into());
    }
    let package_type = Command::new("/usr/bin/plutil")
        .args(["-extract", "CFBundlePackageType", "raw", "-o", "-"])
        .arg(&info_plist)
        .output()
        .map_err(|_| "COMPUTER_USE_APP_BUNDLE_INVALID".to_string())?;
    if !package_type.status.success()
        || String::from_utf8_lossy(&package_type.stdout).trim() != "APPL"
    {
        return Err("COMPUTER_USE_APP_BUNDLE_INVALID".into());
    }
    Ok(bundle_path)
}

#[cfg(target_os = "macos")]
fn open_application_command_at_path(path: &std::path::Path) -> std::process::Command {
    let mut command = std::process::Command::new("/usr/bin/open");
    command.arg("-n").arg(path);
    command
}

pub fn launch_application_at_path(
    application_bundle_id: &str,
    application_path: &str,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let bundle_path =
            validated_application_bundle_path(application_bundle_id, application_path)?;
        // This path is already authorized by the backend against the selected
        // project's workspace and Computer Use allowlist. Send that exact
        // bundle to Launch Services instead of asking NSWorkspace to discover
        // an installation by bundle identity; project-built bundles may not
        // have a Launch Services registration yet.
        let output = open_application_command_at_path(&bundle_path)
            .output()
            .map_err(|_| "COMPUTER_USE_APPLICATION_LAUNCH_FAILED".to_string())?;
        if !output.status.success() {
            return Err("COMPUTER_USE_APPLICATION_LAUNCH_FAILED".into());
        }
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (application_bundle_id, application_path);
        Err("COMPUTER_USE_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn observe_application(
    application_pid: i32,
    include_menu_bar: bool,
) -> Result<AccessibilityObservation, String> {
    #[cfg(target_os = "macos")]
    {
        if application_pid <= 0 || application_pid == std::process::id() as i32 {
            return Err("ACCESSIBILITY_TARGET_APPLICATION_INVALID".into());
        }
        observe_macos_with_menu_scope(Some(application_pid), include_menu_bar)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (application_pid, include_menu_bar);
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Read-only preflight for a future vision-grounded pointer action. It verifies
/// that the mapped point belongs to the identity-bound AX window and that
/// ScreenCaptureKit's display geometry still matches Quartz. The target need
/// not be frontmost while the chat shows its captured window; the eventual
/// click activates it and rechecks frontmost identity immediately before input.
/// This function never activates the app, requests permissions, or posts input.
pub fn validate_grounded_pointer_target(
    application_pid: i32,
    application_bundle_id: &str,
    point: [f64; 2],
    display_id: u32,
    display_geometry: (f64, f64, f64, f64),
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::NSRunningApplication;

        if application_pid <= 0
            || application_bundle_id.trim().is_empty()
            || application_bundle_id.len() > 256
        {
            return Err("VISION_POINTER_TARGET_IDENTITY_INVALID".into());
        }
        if !is_trusted() {
            return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
        }
        if !unsafe { CGPreflightPostEventAccess() } {
            return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
        }
        let expected_display = (
            display_geometry.0,
            display_geometry.1,
            display_geometry.2,
            display_geometry.3,
        );
        if display_id == 0
            || !expected_display.0.is_finite()
            || !expected_display.1.is_finite()
            || !expected_display.2.is_finite()
            || !expected_display.3.is_finite()
            || expected_display.2 <= 0.0
            || expected_display.3 <= 0.0
            || !point_is_inside_display_bounds(
                (point[0], point[1]),
                (expected_display.0, expected_display.1),
                (expected_display.2, expected_display.3),
            )
        {
            return Err("VISION_POINTER_DISPLAY_GEOMETRY_INVALID".into());
        }
        // The capture surface may be the exact target window rather than the
        // entire display. Its image geometry is therefore smaller than the
        // Core Graphics display bounds; validate the mapped point against the
        // live display and the target window below instead of comparing those
        // two different coordinate extents for equality.
        if !active_display_contains_point(display_id, (point[0], point[1]))? {
            return Err("VISION_POINTER_DISPLAY_GEOMETRY_CHANGED".into());
        }
        let running =
            NSRunningApplication::runningApplicationWithProcessIdentifier(application_pid)
                .ok_or("ACCESSIBILITY_TARGET_APPLICATION_NOT_RUNNING")?;
        if running.isTerminated()
            || running
                .bundleIdentifier()
                .is_none_or(|value| value.to_string() != application_bundle_id)
        {
            return Err("VISION_POINTER_TARGET_IDENTITY_MISMATCH".into());
        }
        let observation = observe_macos(Some(application_pid))?;
        if observation.application_pid != Some(application_pid)
            || observation.application_bundle_id.as_deref() != Some(application_bundle_id)
            || observation.coordinate_space != "ax_global_screen_coordinates"
        {
            return Err("VISION_POINTER_AX_IDENTITY_OR_COORDINATE_SPACE_MISMATCH".into());
        }
        let window = observation
            .window_frame
            .as_ref()
            .ok_or("VISION_POINTER_TARGET_WINDOW_FRAME_UNAVAILABLE")?;
        if !point_is_inside_accessibility_rect((point[0], point[1]), window) {
            return Err("VISION_POINTER_POINT_OUTSIDE_TARGET_WINDOW".into());
        }
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            application_pid,
            application_bundle_id,
            point,
            display_id,
            display_geometry,
        );
        Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into())
    }
}

/// Recheck a target after pointer movement using a fresh, same-process AX tree.
/// When an app omits AX geometry, a freshly tracked visual frame may validate
/// the pointer for AXButtons only; semantic identity and actionability still
/// come from AX, and AXPress must remain unavailable.
pub fn validate_moved_pointer_target(
    observation: &AccessibilityObservation,
    application_pid: i32,
    application_bundle_id: &str,
    element_id: &str,
    label: &str,
    expected_role: &str,
    point: [f64; 2],
    tracked_visual_frame: Option<&AccessibilityRect>,
) -> Result<(), String> {
    if observation.source != "AX"
        || observation.permission_granted != true
        || observation.application_pid != Some(application_pid)
        || observation.application_bundle_id.as_deref() != Some(application_bundle_id)
        || observation.coordinate_space != "ax_global_screen_coordinates"
    {
        return Err("POINTER_MOVE_FRESH_AX_IDENTITY_MISMATCH".into());
    }
    let matches = observation
        .elements
        .iter()
        .filter(|element| element.role == expected_role && element.label == label)
        .collect::<Vec<_>>();
    if matches.len() != 1 || matches[0].id != element_id {
        return Err("POINTER_MOVE_TARGET_NOT_UNIQUE".into());
    }
    let window = observation
        .window_frame
        .as_ref()
        .ok_or("POINTER_MOVE_FRESH_AX_WINDOW_UNAVAILABLE")?;
    if !point_is_inside_accessibility_rect((point[0], point[1]), window) {
        return Err("POINTER_MOVE_POINT_OUTSIDE_FRESH_AX_WINDOW".into());
    }
    let target = matches[0];
    let frame = target.frame.as_ref().and_then(|frame| {
        valid_pointer_frame(&(frame.x, frame.y, frame.width, frame.height)).then_some(frame)
    });
    let point_aligned = frame
        .is_some_and(|frame| point_is_inside_accessibility_rect((point[0], point[1]), frame))
        || (expected_role == "AXButton"
            && tracked_visual_frame.is_some_and(|frame| {
                valid_pointer_frame(&(frame.x, frame.y, frame.width, frame.height))
                    && point_is_inside_accessibility_rect((point[0], point[1]), frame)
            }));
    let role_state_is_actionable = match expected_role {
        "AXButton" => target.enabled == Some(true),
        "AXDisclosureTriangle" => target.expanded == Some(false),
        _ => false,
    };
    if target.visible != Some(true)
        || !role_state_is_actionable
        || target
            .supported_actions
            .iter()
            .any(|action| action == "AXPress")
        || !point_aligned
    {
        return Err("POINTER_MOVE_TARGET_CHANGED_OR_POINT_MISALIGNED".into());
    }
    Ok(())
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityChange {
    pub source: &'static str,
    pub notification: String,
    pub application_pid: i32,
    pub session_id: String,
    pub observed_at: f64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessibilityMonitorStatus {
    pub active: bool,
    pub application_pid: Option<i32>,
}

struct MonitorWorker {
    stop: Arc<AtomicBool>,
    application_pid: i32,
    join: JoinHandle<()>,
}

#[derive(Default)]
pub struct AccessibilityMonitor {
    worker: Mutex<Option<MonitorWorker>>,
}

impl AccessibilityMonitor {
    pub fn start(
        &self,
        app: AppHandle,
        application_pid: i32,
    ) -> Result<AccessibilityMonitorStatus, String> {
        if !is_trusted() {
            return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
        }
        if application_pid <= 0 || application_pid == std::process::id() as i32 {
            return Err("ACCESSIBILITY_TARGET_APPLICATION_INVALID".into());
        }
        let mut worker = self
            .worker
            .lock()
            .map_err(|_| "ACCESSIBILITY_MONITOR_LOCK_FAILED")?;
        if let Some(current) = worker.as_ref() {
            return Ok(AccessibilityMonitorStatus {
                active: true,
                application_pid: Some(current.application_pid),
            });
        }
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let join = thread::Builder::new()
            .name("olcr-ax-observer".into())
            .spawn(move || {
                #[cfg(target_os = "macos")]
                let subscription = application_notification_stream(application_pid);
                #[cfg(not(target_os = "macos"))]
                let subscription: Result<_, String> =
                    Err("ACCESSIBILITY_UNSUPPORTED_PLATFORM".into());
                let (application_pid, stream) = match subscription {
                    Ok(subscription) => subscription,
                    Err(error) => {
                        let _ = ready_tx.send(Err(error));
                        return;
                    }
                };
                if ready_tx.send(Ok(application_pid)).is_err() {
                    return;
                }
                while !thread_stop.load(Ordering::Relaxed) {
                    if let Some(event) = stream.try_next() {
                        if is_perception_notification(&event.notification) {
                            let payload = AccessibilityChange {
                                source: "AX",
                                notification: event.notification.clone(),
                                application_pid,
                                session_id: format!("macos-ax-{application_pid}"),
                                observed_at: SystemTime::now()
                                    .duration_since(UNIX_EPOCH)
                                    .unwrap_or_default()
                                    .as_secs_f64(),
                            };
                            let _ = app.emit("accessibility-change", payload);
                        }
                    } else {
                        thread::sleep(Duration::from_millis(50));
                    }
                }
            })
            .map_err(|error| format!("ACCESSIBILITY_MONITOR_THREAD_FAILED: {error}"))?;
        let application_pid = match ready_rx.recv_timeout(Duration::from_secs(3)) {
            Ok(Ok(pid)) => pid,
            Ok(Err(error)) => {
                let _ = join.join();
                return Err(error);
            }
            Err(_) => {
                let _ = join.join();
                return Err("ACCESSIBILITY_MONITOR_START_TIMEOUT".into());
            }
        };
        *worker = Some(MonitorWorker {
            stop,
            application_pid,
            join,
        });
        Ok(AccessibilityMonitorStatus {
            active: true,
            application_pid: Some(application_pid),
        })
    }

    pub fn stop(&self) -> Result<AccessibilityMonitorStatus, String> {
        let worker = self
            .worker
            .lock()
            .map_err(|_| "ACCESSIBILITY_MONITOR_LOCK_FAILED")?
            .take();
        if let Some(worker) = worker {
            worker.stop.store(true, Ordering::Relaxed);
            worker
                .join
                .join()
                .map_err(|_| "ACCESSIBILITY_MONITOR_STOP_FAILED")?;
        }
        Ok(AccessibilityMonitorStatus {
            active: false,
            application_pid: None,
        })
    }

    pub fn status(&self) -> Result<AccessibilityMonitorStatus, String> {
        let worker = self
            .worker
            .lock()
            .map_err(|_| "ACCESSIBILITY_MONITOR_LOCK_FAILED")?;
        Ok(match worker.as_ref() {
            Some(worker) => AccessibilityMonitorStatus {
                active: true,
                application_pid: Some(worker.application_pid),
            },
            None => AccessibilityMonitorStatus {
                active: false,
                application_pid: None,
            },
        })
    }
}

#[cfg(target_os = "macos")]
fn application_notification_stream(
    application_pid: i32,
) -> Result<(i32, axuielement::async_api::AXNotificationStream), String> {
    if objc2_app_kit::NSRunningApplication::runningApplicationWithProcessIdentifier(application_pid)
        .is_none_or(|application| {
            application.isTerminated() || application.bundleIdentifier().is_none()
        })
    {
        return Err("ACCESSIBILITY_TARGET_APPLICATION_NOT_RUNNING".into());
    }
    subscribe_application(application_pid)
}

#[cfg(target_os = "macos")]
fn subscribe_application(
    application_pid: i32,
) -> Result<(i32, axuielement::async_api::AXNotificationStream), String> {
    use axuielement::async_api::AXNotificationStream;
    use axuielement::ax_notification::{
        AX_FOCUSED_UI_ELEMENT_CHANGED_NOTIFICATION, AX_FOCUSED_WINDOW_CHANGED_NOTIFICATION,
        AX_MAIN_WINDOW_CHANGED_NOTIFICATION, AX_SELECTED_CHILDREN_CHANGED_NOTIFICATION,
        AX_SELECTED_TEXT_CHANGED_NOTIFICATION, AX_VALUE_CHANGED_NOTIFICATION,
        AX_WINDOW_CREATED_NOTIFICATION, AX_WINDOW_MOVED_NOTIFICATION,
        AX_WINDOW_RESIZED_NOTIFICATION,
    };

    let application = axuielement::AXUIElement::from_pid(application_pid)
        .ok_or("ACCESSIBILITY_TARGET_APPLICATION_HANDLE_FAILED")?;
    let stream = AXNotificationStream::subscribe_many(
        &application,
        &[
            AX_FOCUSED_UI_ELEMENT_CHANGED_NOTIFICATION,
            AX_FOCUSED_WINDOW_CHANGED_NOTIFICATION,
            AX_MAIN_WINDOW_CHANGED_NOTIFICATION,
            AX_WINDOW_CREATED_NOTIFICATION,
            AX_WINDOW_MOVED_NOTIFICATION,
            AX_WINDOW_RESIZED_NOTIFICATION,
            AX_VALUE_CHANGED_NOTIFICATION,
            AX_SELECTED_CHILDREN_CHANGED_NOTIFICATION,
            AX_SELECTED_TEXT_CHANGED_NOTIFICATION,
        ],
        16,
    )
    .or_else(|_| {
        AXNotificationStream::subscribe_many(
            &application,
            &[
                AX_FOCUSED_UI_ELEMENT_CHANGED_NOTIFICATION,
                AX_FOCUSED_WINDOW_CHANGED_NOTIFICATION,
                AX_VALUE_CHANGED_NOTIFICATION,
            ],
            16,
        )
    })
    .or_else(|_| {
        AXNotificationStream::subscribe_many(
            &application,
            &[
                AX_FOCUSED_UI_ELEMENT_CHANGED_NOTIFICATION,
                AX_FOCUSED_WINDOW_CHANGED_NOTIFICATION,
            ],
            16,
        )
    })
    .or_else(|_| {
        AXNotificationStream::subscribe_many(
            &application,
            &[AX_FOCUSED_UI_ELEMENT_CHANGED_NOTIFICATION],
            16,
        )
    })
    .map_err(|error| format!("ACCESSIBILITY_NOTIFICATION_SUBSCRIBE_FAILED: {error}"))?;
    Ok((application_pid, stream))
}

#[cfg(target_os = "macos")]
fn observe_macos(target_pid: Option<i32>) -> Result<AccessibilityObservation, String> {
    observe_macos_with_menu_scope(target_pid, true)
}

#[cfg(target_os = "macos")]
fn observe_macos_with_menu_scope(
    target_pid: Option<i32>,
    include_menu_bar: bool,
) -> Result<AccessibilityObservation, String> {
    use axuielement::ax_attribute::attributes as attr;
    use axuielement::prelude::*;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    let deadline = Instant::now() + SNAPSHOT_BUDGET;
    let application = if let Some(pid) = target_pid {
        let running =
            objc2_app_kit::NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
                .ok_or("ACCESSIBILITY_TARGET_APPLICATION_NOT_RUNNING")?;
        if running.isTerminated() || running.bundleIdentifier().is_none() {
            return Err("ACCESSIBILITY_TARGET_APPLICATION_IDENTITY_UNAVAILABLE".into());
        }
        axuielement::AXUIElement::from_pid(pid)
            .ok_or("ACCESSIBILITY_TARGET_APPLICATION_HANDLE_FAILED")?
    } else {
        let system = system_wide().ok_or("ACCESSIBILITY_SYSTEM_UNAVAILABLE")?;
        let _ = system.set_timeout(0.1);
        system
            .focused_application()
            .map_err(|error| format!("ACCESSIBILITY_FOCUSED_APPLICATION_FAILED: {error}"))?
            .ok_or("ACCESSIBILITY_NO_FOCUSED_APPLICATION")?
    };
    let application_pid = application.pid().ok();
    let application_bundle_id = application_pid.and_then(|pid| {
        objc2_app_kit::NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
            .and_then(|running| running.bundleIdentifier())
            .map(|bundle_id| bundle_id.to_string())
    });
    let _ = application.set_timeout(0.1);
    let (window, window_selection) = select_observation_window(&application)?;
    let _ = window.set_timeout(0.1);
    let window_title = bounded_text(
        window
            .string_attribute(attr::AX_TITLE_ATTRIBUTE)
            .ok()
            .flatten(),
    );
    let window_position = window
        .point_attribute(attr::AX_POSITION_ATTRIBUTE)
        .ok()
        .flatten();
    let window_size = window
        .size_attribute(attr::AX_SIZE_ATTRIBUTE)
        .ok()
        .flatten();
    let window_frame = window_position
        .zip(window_size)
        .map(|(origin, size)| AccessibilityRect {
            x: origin.x,
            y: origin.y,
            width: size.width,
            height: size.height,
        });

    let window_minimized = window
        .bool_attribute(attr::AX_MINIMIZED_ATTRIBUTE)
        .ok()
        .flatten();
    let mut pending = VecDeque::from([(window, 0usize)]);
    let mut menu_bar = if include_menu_bar {
        application
            .element_attribute(attr::AX_MENU_BAR_ATTRIBUTE)
            .ok()
            .flatten()
    } else {
        None
    };
    let mut elements = Vec::with_capacity(MAX_ELEMENTS);
    let mut cached_elements = Vec::with_capacity(MAX_ELEMENTS);
    let mut uncertainty = Vec::new();
    if window_selection != "focused" {
        uncertainty.push(format!("AX_WINDOW_SELECTION_USED_{}", window_selection));
    }
    let mut truncated = false;
    let mut visited = 0usize;
    loop {
        if pending.is_empty() {
            if let Some(menu_bar) = menu_bar.take() {
                pending.push_back((menu_bar, 0));
            } else {
                break;
            }
        }
        let Some((element, depth)) = pending.pop_front() else {
            break;
        };
        if elements.len() >= MAX_ELEMENTS
            || visited >= MAX_VISITED_NODES
            || Instant::now() >= deadline
        {
            truncated = true;
            break;
        }
        visited += 1;
        let _ = element.set_timeout(0.1);
        let role = element
            .string_attribute(attr::AX_ROLE_ATTRIBUTE)
            .ok()
            .flatten()
            .unwrap_or_default();
        let subrole = element
            .string_attribute(attr::AX_SUBROLE_ATTRIBUTE)
            .ok()
            .flatten()
            .and_then(|value| bounded_text(Some(value)));
        let title = element
            .string_attribute(attr::AX_TITLE_ATTRIBUTE)
            .ok()
            .flatten();
        let description = element
            .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
            .ok()
            .flatten();
        // Static display text contributes visible evidence for post-action
        // verification. Never read AXValue from editable or secure controls.
        let display_value = (role == "AXStaticText")
            .then(|| element.string_attribute("AXValue").ok().flatten())
            .flatten();
        let label =
            accessible_observation_text(&role, title, description, display_value).or_else(|| {
                match role.as_str() {
                    "AXScrollArea" | "AXWebArea" => Some("Scrollable area".to_owned()),
                    "AXScrollBar" => Some("Scrollbar".to_owned()),
                    _ => None,
                }
            });
        if let Some(label) = label.filter(|_| !role.is_empty()) {
            let enabled = element
                .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
                .ok()
                .flatten();
            let hidden = element.bool_attribute("AXHidden").ok().flatten();
            let expanded = element
                .bool_attribute(attr::AX_EXPANDED_ATTRIBUTE)
                .ok()
                .flatten();
            // AXValue is sensitive for editable controls. Expose only a bounded
            // numeric position for scroll bars so chat can verify wheel effects.
            let scroll_value = (role == "AXScrollBar")
                .then(|| {
                    element
                        .f64_attribute(attr::AX_VALUE_ATTRIBUTE)
                        .ok()
                        .flatten()
                })
                .flatten()
                .filter(|value| value.is_finite() && (0.0..=1.0).contains(value));
            let focused = element
                .bool_attribute(attr::AX_FOCUSED_ATTRIBUTE)
                .ok()
                .flatten();
            let supported_actions = element
                .action_names()
                .unwrap_or_default()
                .into_iter()
                .filter(|action| {
                    matches!(action.as_str(), "AXPress" | "AXIncrement" | "AXDecrement")
                })
                .take(4)
                .collect::<Vec<_>>();
            let keyboard_shortcut = if role == axuielement::ax_attribute::roles::AX_MENU_ITEM_ROLE {
                let key_code = element
                    .i64_attribute(attr::AX_MENU_ITEM_CMD_VIRTUAL_KEY_ATTRIBUTE)
                    .ok()
                    .flatten();
                let modifiers = element
                    .i64_attribute(attr::AX_MENU_ITEM_CMD_MODIFIERS_ATTRIBUTE)
                    .ok()
                    .flatten();
                match (key_code, modifiers) {
                    (Some(key_code), Some(modifiers))
                        if (0..=127).contains(&key_code) && (0..=15).contains(&modifiers) =>
                    {
                        let key = element
                            .string_attribute(attr::AX_MENU_ITEM_CMD_CHAR_ATTRIBUTE)
                            .ok()
                            .flatten()
                            .filter(|value| !value.is_empty())
                            .unwrap_or_else(|| format!("keycode-{key_code}"));
                        Some(KeyboardShortcut {
                            key_code: key_code as u16,
                            modifiers: modifiers as u32,
                            key,
                            provenance: "application_menu",
                        })
                    }
                    _ => None,
                }
            } else {
                None
            };
            let position = element
                .point_attribute(attr::AX_POSITION_ATTRIBUTE)
                .ok()
                .flatten();
            let size = element
                .size_attribute(attr::AX_SIZE_ATTRIBUTE)
                .ok()
                .flatten();
            let frame = position.zip(size).map(|(origin, size)| AccessibilityRect {
                x: origin.x,
                y: origin.y,
                width: size.width,
                height: size.height,
            });
            let visible = observed_element_visibility(
                hidden,
                &role,
                frame.as_ref(),
                window_frame.as_ref(),
                window_minimized,
            );
            let id = format!("ax-{}", elements.len());
            elements.push(AccessibilityElement {
                id: id.clone(),
                role: role.clone(),
                subrole: subrole.clone(),
                label: label.clone(),
                frame: frame.clone(),
                visible,
                enabled,
                focused,
                selected: element
                    .bool_attribute(attr::AX_SELECTED_ATTRIBUTE)
                    .ok()
                    .flatten(),
                expanded,
                scroll_value,
                supported_actions: supported_actions.clone(),
                keyboard_shortcut: keyboard_shortcut.clone(),
                depth,
            });
            let cached_frame = frame
                .as_ref()
                .map(|rect| (rect.x, rect.y, rect.width, rect.height));
            cached_elements.push(CachedAXElement {
                id,
                role,
                subrole,
                label,
                visible,
                enabled,
                focused,
                expanded,
                scroll_value,
                depth,
                frame: cached_frame,
                supported_actions,
                keyboard_shortcut,
                element: element.clone(),
            });
        }
        if depth >= MAX_DEPTH {
            continue;
        }
        let child_count = match element.attribute_value_count(attr::AX_CHILDREN_ATTRIBUTE) {
            Ok(count) => count,
            Err(error) => {
                if uncertainty.len() < 4 {
                    uncertainty.push(format!("AX_CHILDREN_UNAVAILABLE: {error}"));
                }
                continue;
            }
        };
        let remaining = MAX_VISITED_NODES.saturating_sub(visited + pending.len());
        let children_to_read = child_count.min(MAX_CHILDREN_PER_ELEMENT).min(remaining);
        if children_to_read < child_count {
            truncated = true;
        }
        if children_to_read > 0 {
            match element.element_array_attribute_range(
                attr::AX_CHILDREN_ATTRIBUTE,
                0,
                children_to_read,
            ) {
                Ok(children) => {
                    pending.extend(children.into_iter().map(|child| (child, depth + 1)))
                }
                Err(error) => {
                    if uncertainty.len() < 4 {
                        uncertainty.push(format!("AX_CHILDREN_UNAVAILABLE: {error}"));
                    }
                }
            }
        }
    }
    if Instant::now() >= deadline && uncertainty.len() < 4 {
        uncertainty.push("AX_SNAPSHOT_TIME_BUDGET_REACHED".into());
    }

    let observed_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let text = elements
        .iter()
        .map(|element| element.label.clone())
        .collect();
    let observation_id = format!(
        "ax-{}-{}",
        application_pid.unwrap_or_default(),
        (observed_at * 1_000_000.0) as u64
    );
    let session_id = application_pid
        .map(|pid| format!("macos-ax-{pid}"))
        .unwrap_or_else(|| "macos-ax-unknown".into());
    let mut observation = AccessibilityObservation {
        source: "AX",
        permission_granted: true,
        observation_id,
        observed_at,
        session_id,
        coordinate_space: "ax_global_screen_coordinates",
        width: None,
        height: None,
        confidence: if elements.is_empty() { 0.0 } else { 0.95 },
        text,
        application_pid,
        application_bundle_id,
        window_title,
        window_frame,
        elements,
        truncated,
        uncertainty,
    };
    bound_observation_bytes(&mut observation);
    cached_elements.truncate(observation.elements.len());
    if let Some(pid) = application_pid {
        let cache = ACTION_SNAPSHOTS.get_or_init(|| Mutex::new(VecDeque::new()));
        if let Ok(mut cache) = cache.lock() {
            cache.push_back(CachedAXSnapshot {
                observation_id: observation.observation_id.clone(),
                session_id: observation.session_id.clone(),
                application_pid: pid,
                application_bundle_id: observation.application_bundle_id.clone(),
                observed_at,
                window_frame: observation
                    .window_frame
                    .as_ref()
                    .map(|frame| (frame.x, frame.y, frame.width, frame.height)),
                elements: cached_elements,
            });
            while cache.len() > 8 {
                cache.pop_front();
            }
        }
    }
    Ok(observation)
}

/// Select a real application window without activating the target app. A background
/// app may expose no focused window (or may return only its menu bar), so use the
/// main window and then the first non-minimized AXWindows entry as bounded fallbacks.
#[cfg(target_os = "macos")]
fn select_observation_window(
    application: &axuielement::AXUIElement,
) -> Result<(axuielement::AXUIElement, &'static str), String> {
    use axuielement::ax_attribute::attributes as attr;

    let _ = application.set_timeout(0.1);
    let has_accessible_children = |window: &axuielement::AXUIElement| {
        window
            .attribute_value_count(attr::AX_CHILDREN_ATTRIBUTE)
            .ok()
    };
    for (attribute, source) in [
        (attr::AX_FOCUSED_WINDOW_ATTRIBUTE, "focused"),
        (attr::AX_MAIN_WINDOW_ATTRIBUTE, "main"),
    ] {
        if let Some(window) = application
            .element_attribute(attribute)
            .ok()
            .flatten()
            .filter(|candidate| {
                let _ = candidate.set_timeout(0.1);
                candidate
                    .string_attribute(attr::AX_ROLE_ATTRIBUTE)
                    .ok()
                    .flatten()
                    .map(|role| {
                        accessible_window_candidate(
                            &role,
                            has_accessible_children(candidate),
                            None,
                            false,
                        )
                    })
                    .unwrap_or(false)
            })
        {
            return Ok((window, source));
        }
    }

    let count = application
        .attribute_value_count(attr::AX_WINDOWS_ATTRIBUTE)
        .map_err(|error| format!("ACCESSIBILITY_WINDOWS_UNAVAILABLE: {error}"))?;
    let windows = application
        .element_array_attribute_range(
            attr::AX_WINDOWS_ATTRIBUTE,
            0,
            count.min(MAX_CHILDREN_PER_ELEMENT),
        )
        .map_err(|error| format!("ACCESSIBILITY_WINDOWS_UNAVAILABLE: {error}"))?;
    for window in windows {
        let _ = window.set_timeout(0.1);
        let role = window
            .string_attribute(attr::AX_ROLE_ATTRIBUTE)
            .ok()
            .flatten();
        let minimized = window
            .bool_attribute(attr::AX_MINIMIZED_ATTRIBUTE)
            .ok()
            .flatten();
        if accessible_window_candidate(
            role.as_deref().unwrap_or_default(),
            has_accessible_children(&window),
            minimized,
            true,
        ) {
            return Ok((window, "windows"));
        }
    }
    Err("ACCESSIBILITY_NO_NONMINIMIZED_CONTENT_WINDOW".into())
}

fn accessible_window_candidate(
    role: &str,
    child_count: Option<usize>,
    minimized: Option<bool>,
    require_non_minimized: bool,
) -> bool {
    role == "AXWindow"
        && child_count.is_some_and(|count| count > 0)
        && (!require_non_minimized || minimized == Some(false))
}

#[cfg(target_os = "macos")]
fn validate_cached_application_identity(
    pid: i32,
    expected_bundle_id: Option<&str>,
) -> Result<(), String> {
    let running = objc2_app_kit::NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
        .ok_or("ACCESSIBILITY_TARGET_APPLICATION_NOT_RUNNING")?;
    if running.isTerminated() {
        return Err("ACCESSIBILITY_TARGET_APPLICATION_NOT_RUNNING".into());
    }
    let actual_bundle_id = running.bundleIdentifier().map(|value| value.to_string());
    if actual_bundle_id.as_deref() != expected_bundle_id || actual_bundle_id.is_none() {
        return Err("ACCESSIBILITY_TARGET_APPLICATION_IDENTITY_CHANGED".into());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn press_fresh_disclosure_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
) -> Result<AccessibilityActionResult, String> {
    use axuielement::ax_action::AX_PRESS_ACTION;
    use axuielement::ax_attribute::attributes as attr;
    use axuielement::ax_attribute::roles;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, target) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == element_id
                    && element.role == roles::AX_DISCLOSURE_TRIANGLE_ROLE
                    && element.label == label
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("ACCESSIBILITY_TARGET_NOT_UNIQUE".into());
        }
        let target = matches[0];
        if !disclosure_press_is_allowed(
            &target.role,
            target.enabled,
            target.expanded,
            target.frame.is_some(),
            &target.supported_actions,
        ) {
            if target.enabled != Some(true) {
                return Err("ACCESSIBILITY_TARGET_DISABLED_OR_UNKNOWN".into());
            }
            if target.expanded != Some(false) {
                return Err("ACCESSIBILITY_TARGET_NOT_CLOSED".into());
            }
            return Err("ACCESSIBILITY_AXPRESS_UNSUPPORTED".into());
        }
        let frame = target.frame.ok_or("ACCESSIBILITY_TARGET_FRAME_REQUIRED")?;
        if !frame.0.is_finite()
            || !frame.1.is_finite()
            || !frame.2.is_finite()
            || !frame.3.is_finite()
            || frame.2 <= 0.0
            || frame.3 <= 0.0
        {
            return Err("ACCESSIBILITY_TARGET_FRAME_INVALID".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            target.element.clone(),
        )
    };
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_title = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = target
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_label_matches = accessible_label_matches(&label, live_title, live_description);
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_expanded = target
        .bool_attribute(attr::AX_EXPANDED_ATTRIBUTE)
        .ok()
        .flatten();
    if live_role != roles::AX_DISCLOSURE_TRIANGLE_ROLE
        || !live_label_matches
        || live_enabled != Some(true)
        || live_expanded != Some(false)
    {
        return Err("ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION".into());
    }
    let actions = target
        .action_names()
        .map_err(|error| format!("ACCESSIBILITY_ACTION_QUERY_FAILED: {error}"))?;
    if !actions.iter().any(|action| action == AX_PRESS_ACTION) {
        return Err("ACCESSIBILITY_AXPRESS_UNSUPPORTED".into());
    }
    target
        .perform_action(AX_PRESS_ACTION)
        .map_err(|error| format!("ACCESSIBILITY_AXPRESS_FAILED: {error}"))?;
    let expected_frame = {
        let snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .and_then(|snapshot| {
                snapshot
                    .elements
                    .iter()
                    .find(|element| element.id == element_id)
            })
            .and_then(|element| element.frame)
    };
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    let verified = observation.application_pid == Some(cached_pid)
        && observation.elements.iter().any(|element| {
            element.role == roles::AX_DISCLOSURE_TRIANGLE_ROLE
                && element.label == label
                && element.expanded == Some(true)
                && match (expected_frame.as_ref(), element.frame.as_ref()) {
                    (Some((x, y, width, height)), Some(frame)) => {
                        (frame.x - x).abs() < 1.0
                            && (frame.y - y).abs() < 1.0
                            && (frame.width - width).abs() < 1.0
                            && (frame.height - height).abs() < 1.0
                    }
                    _ => false,
                }
        });
    Ok(AccessibilityActionResult {
        status: if verified { "PASS" } else { "FAILED" },
        verified,
        execution_attempted: true,
        action: AX_PRESS_ACTION,
        observation,
    })
}

#[cfg(target_os = "macos")]
fn press_fresh_button_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
) -> Result<AccessibilityActionResult, String> {
    use axuielement::ax_action::AX_PRESS_ACTION;
    use axuielement::ax_attribute::attributes as attr;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, target) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == element_id && element.role == "AXButton" && element.label == label
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("ACCESSIBILITY_TARGET_NOT_UNIQUE".into());
        }
        let target = matches[0];
        if target.visible != Some(true) || target.enabled != Some(true) {
            return Err("ACCESSIBILITY_TARGET_HIDDEN_DISABLED_OR_UNKNOWN".into());
        }
        if !target
            .supported_actions
            .iter()
            .any(|action| action == AX_PRESS_ACTION)
        {
            return Err("ACCESSIBILITY_AXPRESS_UNSUPPORTED".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            target.element.clone(),
        )
    };
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_title = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = target
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_hidden = target.bool_attribute("AXHidden").ok().flatten();
    let live_visible = if let Some(hidden) = live_hidden {
        Some(!hidden)
    } else {
        // Re-observe the same application before dispatch so the geometric
        // fallback uses current window/minimization/element evidence too.
        let live_observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
        let live_position = target
            .point_attribute(attr::AX_POSITION_ATTRIBUTE)
            .ok()
            .flatten();
        let live_size = target
            .size_attribute(attr::AX_SIZE_ATTRIBUTE)
            .ok()
            .flatten();
        let matches = live_observation
            .elements
            .iter()
            .filter(|element| {
                element.role == "AXButton"
                    && element.label == label
                    && element.visible == Some(true)
                    && element.frame.as_ref().is_some_and(|frame| {
                        live_position.zip(live_size).is_some_and(|(origin, size)| {
                            (frame.x - origin.x).abs() < 1.0
                                && (frame.y - origin.y).abs() < 1.0
                                && (frame.width - size.width).abs() < 1.0
                                && (frame.height - size.height).abs() < 1.0
                        })
                    })
            })
            .count();
        (matches == 1).then_some(true)
    };
    if live_role != "AXButton"
        || !accessible_label_matches(label, live_title, live_description)
        || live_visible != Some(true)
        || live_enabled != Some(true)
    {
        return Err("ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION".into());
    }
    let actions = target
        .action_names()
        .map_err(|error| format!("ACCESSIBILITY_ACTION_QUERY_FAILED: {error}"))?;
    if !actions.iter().any(|action| action == AX_PRESS_ACTION) {
        return Err("ACCESSIBILITY_AXPRESS_UNSUPPORTED".into());
    }
    target
        .perform_action(AX_PRESS_ACTION)
        .map_err(|error| format!("ACCESSIBILITY_AXPRESS_FAILED: {error}"))?;
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    Ok(AccessibilityActionResult {
        status: "PASS",
        verified: false,
        execution_attempted: true,
        action: AX_PRESS_ACTION,
        observation,
    })
}

#[cfg(target_os = "macos")]
fn enter_fresh_text_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
    input_text: &str,
) -> Result<AccessibilityTextEntryResult, String> {
    use axuielement::ax_attribute::attributes as attr;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, target, role) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == element_id
                    && element.label == label
                    && text_entry_role_allowed(&element.role, element.subrole.as_deref())
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("ACCESSIBILITY_TEXT_TARGET_NOT_UNIQUE".into());
        }
        let target = matches[0];
        if target.visible != Some(true) || target.enabled != Some(true) {
            return Err("ACCESSIBILITY_TEXT_TARGET_NOT_READY_OR_SECURE".into());
        }
        if input_text.contains('\n') && target.role != "AXTextArea" {
            return Err("ACCESSIBILITY_MULTILINE_TEXT_REQUIRES_TEXT_AREA".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            target.element.clone(),
            target.role.clone(),
        )
    };
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_subrole = target
        .string_attribute(attr::AX_SUBROLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_title = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = target
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_focused = target
        .bool_attribute(attr::AX_FOCUSED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_hidden = target.bool_attribute("AXHidden").ok().flatten();
    let value_settable = target
        .is_attribute_settable(attr::AX_VALUE_ATTRIBUTE)
        .unwrap_or(false);
    if live_role != role
        || !text_entry_role_allowed(&live_role, live_subrole.as_deref())
        || !accessible_label_matches(label, live_title, live_description)
        || live_enabled != Some(true)
        || live_hidden == Some(true)
        || !value_settable
    {
        return Err("ACCESSIBILITY_TEXT_TARGET_CHANGED_BEFORE_INPUT".into());
    }

    // Chat may target a visible field before it has keyboard focus. Move focus
    // through AX only when the selected control explicitly supports it, then
    // confirm focus on the same native element before setting its value.
    if live_focused != Some(true) {
        let focus_settable = target
            .is_attribute_settable(attr::AX_FOCUSED_ATTRIBUTE)
            .unwrap_or(false);
        if !focus_settable {
            return Err("ACCESSIBILITY_TEXT_TARGET_CANNOT_BE_FOCUSED".into());
        }
        target
            .set_bool_attribute(attr::AX_FOCUSED_ATTRIBUTE, true)
            .map_err(|_| "ACCESSIBILITY_TEXT_TARGET_FOCUS_FAILED")?;
        let focused = target
            .bool_attribute(attr::AX_FOCUSED_ATTRIBUTE)
            .ok()
            .flatten();
        if focused != Some(true) {
            return Err("ACCESSIBILITY_TEXT_TARGET_FOCUS_NOT_CONFIRMED".into());
        }
    }

    // Do not print or serialize `input_text` or the current AXValue. Verify the
    // set operation locally and export only the boolean receipt and fresh AX tree.
    target
        .set_string_attribute(attr::AX_VALUE_ATTRIBUTE, input_text)
        .map_err(|_| "ACCESSIBILITY_TEXT_SET_FAILED")?;
    let value_matched = target
        .string_attribute(attr::AX_VALUE_ATTRIBUTE)
        .ok()
        .flatten()
        .is_some_and(|value| value == input_text);
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    let focus_revalidated = observation.elements.iter().any(|element| {
        element.id == element_id
            && element.role == role
            && element.label == label
            && element.enabled == Some(true)
            && element.focused == Some(true)
            && element.visible == Some(true)
    });
    let verified = value_matched && focus_revalidated;
    Ok(AccessibilityTextEntryResult {
        status: if verified { "PASS" } else { "FAILED" },
        verified,
        execution_attempted: true,
        value_matched,
        observation,
    })
}

#[cfg(target_os = "macos")]
fn click_fresh_disclosure_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
    vision_point: Option<(f64, f64)>,
    vision_button: bool,
) -> Result<AccessibilityActionResult, String> {
    use axuielement::ax_attribute::attributes as attr;
    use axuielement::ax_attribute::roles;
    use objc2_app_kit::{NSApplicationActivationOptions, NSRunningApplication};

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    if !unsafe { CGPreflightPostEventAccess() } {
        return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let mut action_observation_id = observation_id.to_owned();
    let mut action_element_id = element_id.to_owned();
    if vision_point.is_some() {
        let (snapshot_is_stale, expected_bundle_id) = {
            let mut snapshots = cache
                .lock()
                .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            snapshots.retain(|snapshot| {
                now >= snapshot.observed_at
                    && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
            });
            let snapshot = snapshots
                .iter()
                .find(|snapshot| snapshot.observation_id == observation_id)
                .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
            if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
                return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
            }
            (
                now - snapshot.observed_at > 2.5,
                snapshot.application_bundle_id.clone(),
            )
        };
        if snapshot_is_stale {
            // Local grounding and action planning may outlive the short AX
            // snapshot window. Re-observe the same PID immediately before
            // dispatch, then require one current enabled button with the
            // exact chat-selected label and no newly available AXPress.
            let refreshed = observe_macos_with_menu_scope(Some(application_pid), false)?;
            if refreshed.application_pid != Some(application_pid)
                || refreshed.session_id != session_id
                || refreshed.application_bundle_id != expected_bundle_id
            {
                return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
            }
            action_element_id = unique_fresh_vision_button_id(&refreshed.elements, label)?;
            action_observation_id = refreshed.observation_id;
        }
    }
    let (cached_pid, cached_bundle_id, target, expected_frame) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == action_observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        let expected_role = if vision_button {
            "AXButton"
        } else {
            roles::AX_DISCLOSURE_TRIANGLE_ROLE
        };
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == action_element_id
                    && element.role == expected_role
                    && element.label == label
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("ACCESSIBILITY_TARGET_NOT_UNIQUE".into());
        }
        let target = matches[0];
        let action_allowed = if let Some(point) = vision_point {
            if vision_button {
                button_vision_pointer_is_allowed(
                    &target.role,
                    target.enabled,
                    &target.supported_actions,
                    point,
                )
            } else {
                disclosure_vision_pointer_is_allowed(
                    &target.role,
                    target.enabled,
                    target.expanded,
                    &target.supported_actions,
                    point,
                )
            }
        } else {
            disclosure_pointer_is_allowed(
                &target.role,
                target.enabled,
                target.expanded,
                target.frame,
                &target.supported_actions,
            )
        };
        if !action_allowed {
            if target.enabled != Some(true) {
                return Err("ACCESSIBILITY_TARGET_DISABLED_OR_UNKNOWN".into());
            }
            if !vision_button && target.expanded != Some(false) {
                return Err("ACCESSIBILITY_TARGET_NOT_CLOSED".into());
            }
            if target
                .supported_actions
                .iter()
                .any(|action| action == "AXPress")
            {
                return Err("ACCESSIBILITY_AXPRESS_AVAILABLE_USE_SEMANTIC_ACTION".into());
            }
            return Err("ACCESSIBILITY_TARGET_FRAME_INVALID".into());
        }
        let accessible_frame = target.frame;
        let frame = match (usable_pointer_frame(accessible_frame), vision_point) {
            (Some(frame), _) => Some(frame),
            (None, Some(_)) => None,
            (None, None) => return Err("ACCESSIBILITY_TARGET_FRAME_REQUIRED".into()),
        };
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            target.element.clone(),
            frame,
        )
    };
    let bundle_id = cached_bundle_id
        .as_deref()
        .filter(|value| !value.is_empty())
        .ok_or("ACCESSIBILITY_TARGET_APPLICATION_IDENTITY_REQUIRED")?;
    validate_cached_application_identity(cached_pid, Some(bundle_id))?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_title = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = target
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_expanded = target
        .bool_attribute(attr::AX_EXPANDED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_frame = target
        .point_attribute(attr::AX_POSITION_ATTRIBUTE)
        .ok()
        .flatten()
        .zip(
            target
                .size_attribute(attr::AX_SIZE_ATTRIBUTE)
                .ok()
                .flatten(),
        )
        .map(|(position, size)| (position.x, position.y, size.width, size.height));
    // Some apps expose a zero-sized AXFrame while intentionally leaving
    // geometry unavailable. Treat that like no frame so the separately
    // grounded, window-contained vision point can use the guarded fallback.
    let live_frame = usable_pointer_frame(live_frame);
    let expected_role = if vision_button {
        "AXButton"
    } else {
        roles::AX_DISCLOSURE_TRIANGLE_ROLE
    };
    if live_role != expected_role {
        return Err(if vision_button {
            "VISION_POINTER_LIVE_AX_ROLE_MISMATCH"
        } else {
            "ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION"
        }
        .into());
    }
    if !accessible_label_matches(label, live_title, live_description) {
        return Err(if vision_button {
            "VISION_POINTER_LIVE_AX_LABEL_MISMATCH"
        } else {
            "ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION"
        }
        .into());
    }
    if live_enabled != Some(true) || (!vision_button && live_expanded != Some(false)) {
        return Err(if vision_button {
            "VISION_POINTER_LIVE_AX_TARGET_NOT_ENABLED"
        } else {
            "ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION"
        }
        .into());
    }
    let geometry_mismatch = match (live_frame, expected_frame, vision_point) {
        // Moving controls are expected in vision-tracked actions. Compare
        // the tracked click point with the live AX button frame instead
        // of requiring the button to remain at its earlier AX position.
        (Some(frame), Some(_expected), Some(point)) if vision_button => {
            !tracked_point_is_inside_live_button_frame(point, frame)
        }
        (Some(frame), Some(expected), _) => {
            !valid_pointer_frame(&frame)
                || (frame.0 - expected.0).abs() >= 1.0
                || (frame.1 - expected.1).abs() >= 1.0
                || (frame.2 - expected.2).abs() >= 1.0
                || (frame.3 - expected.3).abs() >= 1.0
        }
        (Some(frame), None, Some(point)) => {
            !valid_pointer_frame(&frame)
                || !point_is_inside_display_bounds(point, (frame.0, frame.1), (frame.2, frame.3))
        }
        (None, None, Some(_)) => false,
        _ => true,
    };
    if geometry_mismatch {
        return Err(if vision_button && live_frame.is_some() {
            "VISION_POINTER_POINT_OUTSIDE_LIVE_AX_BUTTON_FRAME"
        } else if vision_button {
            "VISION_POINTER_LIVE_AX_FRAME_UNAVAILABLE"
        } else {
            "ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION"
        }
        .into());
    }
    let actions = target
        .action_names()
        .map_err(|error| format!("ACCESSIBILITY_ACTION_QUERY_FAILED: {error}"))?;
    if actions.iter().any(|action| action == "AXPress") {
        return Err("ACCESSIBILITY_AXPRESS_AVAILABLE_USE_SEMANTIC_ACTION".into());
    }
    let point = match (vision_point, expected_frame) {
        (Some(point), Some(frame)) => {
            if !valid_pointer_frame(&frame)
                || !point_is_inside_display_bounds(point, (frame.0, frame.1), (frame.2, frame.3))
            {
                return Err("VISION_POINTER_POINT_OUTSIDE_TARGET_FRAME".into());
            }
            point
        }
        (Some(point), None) => point,
        (None, Some(frame)) => pointer_frame_center(frame)?,
        (None, None) => return Err("ACCESSIBILITY_TARGET_FRAME_REQUIRED".into()),
    };
    let target_application =
        NSRunningApplication::runningApplicationWithProcessIdentifier(cached_pid);
    let target_identity_matches = target_application.as_ref().is_some_and(|application| {
        !application.isTerminated()
            && application
                .bundleIdentifier()
                .is_some_and(|value| value.to_string() == bundle_id)
    });
    if !target_identity_matches {
        return Err("POINTER_TARGET_APPLICATION_IDENTITY_MISMATCH".into());
    }
    if vision_point.is_some() {
        if !target_application.is_some_and(|application| application.isActive()) {
            return Err("POINTER_TARGET_APPLICATION_NOT_FRONTMOST".into());
        }
    } else if !target_application.is_some_and(|application| {
        application.activateWithOptions(NSApplicationActivationOptions(0))
    }) {
        return Err("POINTER_TARGET_APPLICATION_ACTIVATION_FAILED".into());
    }
    thread::sleep(Duration::from_millis(120));
    if !frontmost_application_matches(cached_pid, bundle_id) {
        return Err("POINTER_TARGET_APPLICATION_NOT_FRONTMOST".into());
    }
    // Recheck identity immediately before the system-wide mouse event.
    validate_cached_application_identity(cached_pid, Some(bundle_id))?;
    if !frontmost_application_matches(cached_pid, bundle_id) {
        return Err("POINTER_TARGET_APPLICATION_NOT_FRONTMOST".into());
    }
    post_left_click(point)?;
    thread::sleep(Duration::from_millis(120));
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    let verified = observation.application_pid == Some(cached_pid)
        && observation.application_bundle_id.as_deref() == Some(bundle_id)
        && observation.elements.iter().any(|element| {
            element.role == expected_role
                && element.label == label
                && if vision_button {
                    element.visible == Some(true) && element.enabled == Some(true)
                } else {
                    element.expanded == Some(true)
                }
                && expected_frame.is_none_or(|expected| {
                    element.frame.as_ref().is_some_and(|frame| {
                        (frame.x - expected.0).abs() < 1.0
                            && (frame.y - expected.1).abs() < 1.0
                            && (frame.width - expected.2).abs() < 1.0
                            && (frame.height - expected.3).abs() < 1.0
                    })
                })
        });
    Ok(AccessibilityActionResult {
        status: if vision_button || verified {
            "PASS"
        } else {
            "FAILED"
        },
        verified: verified && !vision_button,
        execution_attempted: true,
        action: if vision_point.is_some() {
            "VISION_GROUNDED_POINTER"
        } else {
            "DETERMINISTIC_POINTER"
        },
        observation,
    })
}

#[cfg(target_os = "macos")]
#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn CGEventCreateMouseEvent(
        source: *const c_void,
        mouse_type: u32,
        mouse_cursor_position: QuartzPoint,
        mouse_button: u32,
    ) -> *mut c_void;
    fn CGEventPost(tap: u32, event: *mut c_void);
    fn CGEventCreate(source: *const c_void) -> *mut c_void;
    fn CGEventGetLocation(event: *mut c_void) -> QuartzPoint;
    fn CGPreflightPostEventAccess() -> bool;
    fn CGRequestPostEventAccess() -> bool;
    fn CGGetActiveDisplayList(
        max_displays: u32,
        active_displays: *mut u32,
        display_count: *mut u32,
    ) -> i32;
    fn CGDisplayBounds(display: u32) -> QuartzRect;
    fn CGEventCreateKeyboardEvent(
        source: *const c_void,
        virtual_key: u16,
        key_down: bool,
    ) -> *mut c_void;
    fn CGEventCreateScrollWheelEvent(
        source: *const c_void,
        units: u32,
        wheel_count: u32,
        wheel1: i32,
        wheel2: i32,
        wheel3: i32,
    ) -> *mut c_void;
    fn CGEventSetLocation(event: *mut c_void, location: QuartzPoint);
    fn CGEventSetFlags(event: *mut c_void, flags: u64);
    fn CGEventPostToPid(pid: i32, event: *mut c_void);
}

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Clone, Copy)]
struct QuartzPoint {
    x: f64,
    y: f64,
}

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Clone, Copy)]
struct QuartzSize {
    width: f64,
    height: f64,
}

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Clone, Copy)]
struct QuartzRect {
    origin: QuartzPoint,
    size: QuartzSize,
}

#[cfg(target_os = "macos")]
#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRelease(value: *const c_void);
}

#[cfg(target_os = "macos")]
fn post_keyboard_shortcut(pid: i32, shortcut: &KeyboardShortcut) -> Result<(), String> {
    let events = find_shortcut_events(shortcut.key_code, shortcut.modifiers)?;
    let mut created = Vec::with_capacity(events.len());
    for spec in events {
        // SAFETY: The key code and modifier mask are validated from focused-app AX menu metadata.
        let event =
            unsafe { CGEventCreateKeyboardEvent(std::ptr::null(), spec.key_code, spec.key_down) };
        if event.is_null() {
            for (prior, _) in created {
                unsafe {
                    CFRelease(prior);
                }
            }
            return Err("SHORTCUT_EVENT_CREATE_FAILED".into());
        }
        created.push((event, spec.flags));
    }
    for (event, flags) in created {
        // SAFETY: event is a non-null owned CGEventRef created above.
        unsafe {
            let event = event.cast_mut();
            CGEventSetFlags(event, flags);
            CGEventPostToPid(pid, event);
            CFRelease(event);
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn point_is_on_active_display(point: (f64, f64)) -> Result<bool, String> {
    let mut count = 0;
    // SAFETY: A null display buffer is permitted when asking Core Graphics for the count.
    let first_result = unsafe { CGGetActiveDisplayList(0, std::ptr::null_mut(), &mut count) };
    if first_result != 0 || count == 0 || count > 64 {
        return Err("POINTER_ACTIVE_DISPLAY_QUERY_FAILED".into());
    }
    let mut displays = vec![0_u32; count as usize];
    // SAFETY: `displays` owns `count` writable entries, as required by the API.
    let result = unsafe { CGGetActiveDisplayList(count, displays.as_mut_ptr(), &mut count) };
    if result != 0 || count == 0 || count as usize > displays.len() {
        return Err("POINTER_ACTIVE_DISPLAY_QUERY_FAILED".into());
    }
    for display_id in displays.into_iter().take(count as usize) {
        // SAFETY: the id was returned as an active display by Core Graphics.
        let bounds = unsafe { CGDisplayBounds(display_id) };
        if point_is_inside_display_bounds(
            point,
            (bounds.origin.x, bounds.origin.y),
            (bounds.size.width, bounds.size.height),
        ) {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(target_os = "macos")]
fn active_display_contains_point(display_id: u32, point: (f64, f64)) -> Result<bool, String> {
    let mut count = 0;
    // SAFETY: A null display buffer is permitted when asking Core Graphics for the count.
    let first_result = unsafe { CGGetActiveDisplayList(0, std::ptr::null_mut(), &mut count) };
    if first_result != 0 || count == 0 || count > 64 {
        return Err("POINTER_ACTIVE_DISPLAY_QUERY_FAILED".into());
    }
    let mut displays = vec![0_u32; count as usize];
    // SAFETY: `displays` owns `count` writable entries, as required by the API.
    let result = unsafe { CGGetActiveDisplayList(count, displays.as_mut_ptr(), &mut count) };
    if result != 0 || count == 0 || count as usize > displays.len() {
        return Err("POINTER_ACTIVE_DISPLAY_QUERY_FAILED".into());
    }
    if !displays
        .into_iter()
        .take(count as usize)
        .any(|id| id == display_id)
    {
        return Ok(false);
    }
    // SAFETY: `display_id` was returned by Core Graphics as an active display.
    let bounds = unsafe { CGDisplayBounds(display_id) };
    let actual = (
        bounds.origin.x,
        bounds.origin.y,
        bounds.size.width,
        bounds.size.height,
    );
    Ok(actual.0.is_finite()
        && actual.1.is_finite()
        && actual.2.is_finite()
        && actual.3.is_finite()
        && actual.2 > 0.0
        && actual.3 > 0.0
        && point_is_inside_display_bounds(point, (actual.0, actual.1), (actual.2, actual.3)))
}

#[cfg(target_os = "macos")]
fn frontmost_application_matches(pid: i32, bundle_id: &str) -> bool {
    use objc2_app_kit::NSWorkspace;

    NSWorkspace::sharedWorkspace()
        .frontmostApplication()
        .is_some_and(|application| {
            application.processIdentifier() == pid
                && application
                    .bundleIdentifier()
                    .is_some_and(|value| value.to_string() == bundle_id)
                && !application.isTerminated()
        })
}

/// Read-only Quartz position for provenance-bound pointer feedback.
pub fn pointer_location() -> Option<[f64; 2]> {
    #[cfg(target_os = "macos")]
    {
        // SAFETY: CGEventCreate returns an owned event, released after reading.
        let event = unsafe { CGEventCreate(std::ptr::null()) };
        if event.is_null() {
            return None;
        }
        let point = unsafe { CGEventGetLocation(event) };
        unsafe { CFRelease(event) };
        if point.x.is_finite() && point.y.is_finite() {
            Some([point.x, point.y])
        } else {
            None
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

/// Post one mouse-move event only to the already-active identity-bound target.
/// The caller sequences bounded steps and verifies each sampled Quartz position.
pub fn move_pointer_one_step(
    application_pid: i32,
    application_bundle_id: &str,
    point: [f64; 2],
) -> Result<[f64; 2], String> {
    #[cfg(target_os = "macos")]
    {
        if !is_trusted() {
            return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
        }
        if !unsafe { CGPreflightPostEventAccess() } {
            return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
        }
        if !point[0].is_finite() || !point[1].is_finite() {
            return Err("POINTER_MOVE_COORDINATE_INVALID".into());
        }
        validate_cached_application_identity(application_pid, Some(application_bundle_id))?;
        if !frontmost_application_matches(application_pid, application_bundle_id) {
            return Err("POINTER_TARGET_APPLICATION_NOT_FRONTMOST".into());
        }
        if !point_is_on_active_display((point[0], point[1]))? {
            return Err("POINTER_COORDINATE_OUTSIDE_ACTIVE_DISPLAYS".into());
        }
        let event = unsafe {
            CGEventCreateMouseEvent(
                std::ptr::null(),
                5, // kCGEventMouseMoved
                QuartzPoint {
                    x: point[0],
                    y: point[1],
                },
                0,
            )
        };
        if event.is_null() {
            return Err("POINTER_MOVE_EVENT_CREATE_FAILED".into());
        }
        unsafe {
            CGEventPost(0, event);
            CFRelease(event);
        }
        // CGEventPost is asynchronous. Under screen capture and AppKit
        // activation load, an 8 ms single sample can still observe the
        // previous cursor location even though the posted move is accepted.
        // Poll briefly for the exact system cursor to catch up; remain
        // bounded and fail closed when it genuinely diverges.
        let deadline = std::time::Instant::now() + Duration::from_millis(160);
        let actual = loop {
            let actual = pointer_location().ok_or("POINTER_MOVE_POSITION_UNAVAILABLE")?;
            if (actual[0] - point[0]).hypot(actual[1] - point[1]) <= 3.0 {
                break actual;
            }
            if std::time::Instant::now() >= deadline {
                return Err("POINTER_MOVE_POSITION_MISMATCH".into());
            }
            thread::sleep(Duration::from_millis(4));
        };
        if !frontmost_application_matches(application_pid, application_bundle_id) {
            return Err("POINTER_TARGET_APPLICATION_NOT_FRONTMOST".into());
        }
        Ok(actual)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (application_pid, application_bundle_id, point);
        Err("COMPUTER_USE_POINTER_UNSUPPORTED_PLATFORM".into())
    }
}

pub fn target_application_is_frontmost(pid: i32, bundle_id: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        frontmost_application_matches(pid, bundle_id)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (pid, bundle_id);
        false
    }
}

#[cfg(target_os = "macos")]
fn post_left_click(point: (f64, f64)) -> Result<(), String> {
    if !unsafe { CGPreflightPostEventAccess() } {
        return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
    }
    // Quartz mouse events use global coordinates. The AX frame and event point are both
    // in the macOS global logical-point space; reject centers outside every active display.
    if !point_is_on_active_display(point)? {
        return Err("POINTER_COORDINATE_OUTSIDE_ACTIVE_DISPLAYS".into());
    }
    const LEFT_MOUSE_DOWN: u32 = 1;
    const LEFT_MOUSE_UP: u32 = 2;
    const LEFT_MOUSE_BUTTON: u32 = 0;
    const HID_EVENT_TAP: u32 = 0;
    // SAFETY: Both event references are checked before posting and released afterward.
    let down = unsafe {
        CGEventCreateMouseEvent(
            std::ptr::null(),
            LEFT_MOUSE_DOWN,
            QuartzPoint {
                x: point.0,
                y: point.1,
            },
            LEFT_MOUSE_BUTTON,
        )
    };
    let up = unsafe {
        CGEventCreateMouseEvent(
            std::ptr::null(),
            LEFT_MOUSE_UP,
            QuartzPoint {
                x: point.0,
                y: point.1,
            },
            LEFT_MOUSE_BUTTON,
        )
    };
    if down.is_null() || up.is_null() {
        if !down.is_null() {
            unsafe { CFRelease(down) };
        }
        if !up.is_null() {
            unsafe { CFRelease(up) };
        }
        return Err("POINTER_EVENT_CREATE_FAILED".into());
    }
    unsafe {
        CGEventPost(HID_EVENT_TAP, down);
        CGEventPost(HID_EVENT_TAP, up);
        CFRelease(down);
        CFRelease(up);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn activate_find_shortcut_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
) -> Result<AccessibilityShortcutResult, String> {
    use axuielement::ax_attribute::attributes as attr;
    use axuielement::ax_attribute::roles;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    if !safe_find_menu_label(label) {
        return Err("SHORTCUT_ACTION_NOT_ALLOWLISTED".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, target, shortcut, already_focused) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == element_id
                    && element.role == roles::AX_MENU_ITEM_ROLE
                    && element.label == label
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("SHORTCUT_MENU_ACTION_NOT_UNIQUE".into());
        }
        let target = matches[0];
        if target.enabled != Some(true) {
            return Err("SHORTCUT_MENU_ACTION_DISABLED_OR_UNKNOWN".into());
        }
        let shortcut = target
            .keyboard_shortcut
            .clone()
            .ok_or("SHORTCUT_MENU_METADATA_UNAVAILABLE")?;
        if shortcut.provenance != "application_menu" {
            return Err("SHORTCUT_PROVENANCE_UNTRUSTED".into());
        }
        let already_focused = snapshot.elements.iter().any(|item| {
            item.focused == Some(true)
                && item.role == axuielement::ax_attribute::roles::AX_TEXT_FIELD_ROLE
                && (item.subrole.as_deref() == Some("AXSearchField")
                    || safe_find_menu_label(&item.label))
        });
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            target.element.clone(),
            shortcut,
            already_focused,
        )
    };
    if already_focused {
        return Err("SEARCH_FIELD_ALREADY_FOCUSED".into());
    }
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_label = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten()
        .and_then(|value| bounded_text(Some(value)))
        .unwrap_or_default();
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_key_code = target
        .i64_attribute(attr::AX_MENU_ITEM_CMD_VIRTUAL_KEY_ATTRIBUTE)
        .ok()
        .flatten();
    let live_modifiers = target
        .i64_attribute(attr::AX_MENU_ITEM_CMD_MODIFIERS_ATTRIBUTE)
        .ok()
        .flatten();
    if live_role != roles::AX_MENU_ITEM_ROLE
        || live_label != label
        || live_enabled != Some(true)
        || live_key_code != Some(i64::from(shortcut.key_code))
        || live_modifiers != Some(i64::from(shortcut.modifiers))
    {
        return Err("SHORTCUT_MENU_METADATA_CHANGED".into());
    }
    post_keyboard_shortcut(cached_pid, &shortcut)?;
    thread::sleep(Duration::from_millis(120));
    let observation = observe_macos(Some(cached_pid))?;
    let verified = observation.application_pid == Some(cached_pid)
        && observation.elements.iter().any(|item| {
            item.role == roles::AX_TEXT_FIELD_ROLE
                && item.focused == Some(true)
                && (item.subrole.as_deref() == Some("AXSearchField")
                    || safe_find_menu_label(&item.label))
        });
    Ok(AccessibilityShortcutResult {
        status: if verified { "PASS" } else { "FAILED" },
        verified,
        execution_attempted: true,
        shortcut,
        observation,
    })
}

fn vertical_scrollbar_frame_is_valid(frame: &(f64, f64, f64, f64)) -> bool {
    valid_pointer_frame(frame) && frame.3 >= frame.2 * 2.0
}

fn scrollbar_frame_belongs_to_area(
    area: &(f64, f64, f64, f64),
    scrollbar: &(f64, f64, f64, f64),
) -> bool {
    if !valid_pointer_frame(area) || !vertical_scrollbar_frame_is_valid(scrollbar) {
        return false;
    }
    let overlap_y = (area.1 + area.3).min(scrollbar.1 + scrollbar.3) - area.1.max(scrollbar.1);
    let tolerance = 16.0_f64.max(scrollbar.2);
    let scrollbar_center_x = scrollbar.0 + scrollbar.2 / 2.0;
    overlap_y >= scrollbar.3 * 0.5
        && scrollbar_center_x >= area.0 - tolerance
        && scrollbar_center_x <= area.0 + area.2 + tolerance
}

#[cfg(target_os = "macos")]
fn scroll_fresh_area_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    area_element_id: &str,
    area_label: &str,
    scrollbar_element_id: &str,
    before_scroll_value: f64,
    direction: &str,
) -> Result<AccessibilityScrollResult, String> {
    use axuielement::ax_attribute::attributes as attr;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    if !unsafe { CGPreflightPostEventAccess() } {
        return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
    }
    if application_pid <= 0 || area_label.trim().is_empty() {
        return Err("ACCESSIBILITY_SCROLL_TARGET_INVALID".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, area, scrollbar, area_frame, window_frame) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        if now - snapshot.observed_at > 2.5 {
            return Err("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN".into());
        }
        let areas = snapshot
            .elements
            .iter()
            .enumerate()
            .filter(|(_, element)| {
                matches!(element.role.as_str(), "AXScrollArea" | "AXWebArea")
                    && element.id == area_element_id
                    && element.label == area_label
                    && element.visible == Some(true)
                    && element
                        .frame
                        .is_some_and(|frame| valid_pointer_frame(&frame))
            })
            .collect::<Vec<_>>();
        if areas.len() != 1 {
            return Err("ACCESSIBILITY_SCROLL_AREA_NOT_UNIQUE_OR_VISIBLE".into());
        }
        let (_, area) = areas[0];
        let area_depth = area.depth;
        let area_frame = area
            .frame
            .ok_or("ACCESSIBILITY_SCROLL_AREA_FRAME_REQUIRED")?;
        let window_frame = snapshot
            .window_frame
            .ok_or("ACCESSIBILITY_TARGET_WINDOW_FRAME_REQUIRED")?;
        if !point_is_inside_display_bounds(
            (
                area_frame.0 + area_frame.2 / 2.0,
                area_frame.1 + area_frame.3 / 2.0,
            ),
            (window_frame.0, window_frame.1),
            (window_frame.2, window_frame.3),
        ) {
            return Err("ACCESSIBILITY_SCROLL_AREA_OUTSIDE_TARGET_WINDOW".into());
        }
        let scrollbars = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.depth > area_depth
                    && element.role == "AXScrollBar"
                    && element.visible == Some(true)
                    && element
                        .scroll_value
                        .is_some_and(|value| (value - before_scroll_value).abs() < 0.005)
                    && element
                        .frame
                        .is_some_and(|frame| scrollbar_frame_belongs_to_area(&area_frame, &frame))
            })
            .collect::<Vec<_>>();
        if scrollbars.len() != 1 || scrollbars[0].id != scrollbar_element_id {
            return Err("ACCESSIBILITY_VERTICAL_SCROLLBAR_NOT_UNIQUE_OR_FRESH".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            area.element.clone(),
            scrollbars[0].element.clone(),
            area_frame,
            window_frame,
        )
    };
    let bundle_id = cached_bundle_id
        .as_deref()
        .ok_or("ACCESSIBILITY_TARGET_APPLICATION_IDENTITY_UNAVAILABLE")?;
    validate_cached_application_identity(cached_pid, Some(bundle_id))?;
    if !frontmost_application_matches(cached_pid, bundle_id) {
        return Err("ACCESSIBILITY_TARGET_APPLICATION_NOT_FRONTMOST".into());
    }

    let _ = area.set_timeout(0.1);
    let live_role = area
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_title = area
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = area
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_label = accessible_label(live_title, live_description)
        .unwrap_or_else(|| "Scrollable area".to_owned());
    let live_hidden = area.bool_attribute("AXHidden").ok().flatten();
    let live_position = area
        .point_attribute(attr::AX_POSITION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_size = area.size_attribute(attr::AX_SIZE_ATTRIBUTE).ok().flatten();
    let same_frame = live_position
        .zip(live_size)
        .is_some_and(|(position, size)| {
            (position.x - area_frame.0).abs() < 1.0
                && (position.y - area_frame.1).abs() < 1.0
                && (size.width - area_frame.2).abs() < 1.0
                && (size.height - area_frame.3).abs() < 1.0
        });
    if !matches!(live_role.as_str(), "AXScrollArea" | "AXWebArea")
        || live_label != area_label
        || live_hidden == Some(true)
        || !same_frame
    {
        return Err("ACCESSIBILITY_SCROLL_AREA_CHANGED_BEFORE_INPUT".into());
    }
    let _ = scrollbar.set_timeout(0.1);
    let live_scrollbar_role = scrollbar
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_scroll_value = scrollbar
        .f64_attribute(attr::AX_VALUE_ATTRIBUTE)
        .ok()
        .flatten();
    if live_scrollbar_role != "AXScrollBar"
        || live_scroll_value.is_none_or(|value| {
            !value.is_finite()
                || !(0.0..=1.0).contains(&value)
                || (value - before_scroll_value).abs() >= 0.005
        })
    {
        return Err("ACCESSIBILITY_SCROLL_VALUE_CHANGED_BEFORE_INPUT".into());
    }

    let point = QuartzPoint {
        x: area_frame.0 + area_frame.2 / 2.0,
        y: area_frame.1 + area_frame.3 / 2.0,
    };
    if !point_is_inside_display_bounds(
        (point.x, point.y),
        (window_frame.0, window_frame.1),
        (window_frame.2, window_frame.3),
    ) || !point_is_on_active_display((point.x, point.y))?
    {
        return Err("ACCESSIBILITY_SCROLL_POINT_OUTSIDE_ACTIVE_TARGET".into());
    }
    let wheel_delta = if direction == "down" { -3 } else { 3 };
    let event = unsafe { CGEventCreateScrollWheelEvent(std::ptr::null(), 1, 1, wheel_delta, 0, 0) };
    if event.is_null() {
        return Err("ACCESSIBILITY_SCROLL_EVENT_CREATE_FAILED".into());
    }
    unsafe {
        CGEventSetLocation(event, point);
        CGEventPostToPid(cached_pid, event);
        CFRelease(event);
    }
    thread::sleep(Duration::from_millis(150));
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    if !frontmost_application_matches(cached_pid, bundle_id) {
        return Err("ACCESSIBILITY_TARGET_APPLICATION_CHANGED_AFTER_SCROLL".into());
    }
    Ok(AccessibilityScrollResult {
        status: "PASS",
        execution_attempted: true,
        direction: if direction == "down" { "down" } else { "up" },
        observation,
    })
}

#[cfg(target_os = "macos")]
fn post_keyboard_key_to_pid(pid: i32, key_code: u16) -> Result<(), String> {
    let down = unsafe { CGEventCreateKeyboardEvent(std::ptr::null(), key_code, true) };
    let up = unsafe { CGEventCreateKeyboardEvent(std::ptr::null(), key_code, false) };
    if down.is_null() || up.is_null() {
        if !down.is_null() {
            unsafe { CFRelease(down) };
        }
        if !up.is_null() {
            unsafe { CFRelease(up) };
        }
        return Err("KEY_EVENT_CREATE_FAILED".into());
    }
    unsafe {
        CGEventPostToPid(pid, down);
        CGEventPostToPid(pid, up);
        CFRelease(down);
        CFRelease(up);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn press_fresh_key_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    element_id: &str,
    label: &str,
    key: &str,
) -> Result<AccessibilityKeyResult, String> {
    use axuielement::ax_attribute::attributes as attr;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    if !unsafe { CGPreflightPostEventAccess() } {
        return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
    }
    let (key_name, key_code) = bounded_chat_key(key).ok_or("KEY_NOT_ALLOWLISTED")?;
    if application_pid <= 0 || element_id.is_empty() || label.trim().is_empty() {
        return Err("KEYPRESS_TARGET_IDENTITY_INVALID".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, target) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        if now - snapshot.observed_at > 2.5 {
            return Err("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN".into());
        }
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == element_id
                    && element.label == label
                    && element.visible == Some(true)
                    && keypress_target_enabled(&element.role, element.enabled)
                    && element.focused == Some(true)
                    && keypress_focus_role_allowed(&element.role, element.subrole.as_deref())
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err("KEYPRESS_TARGET_CHANGED_BEFORE_INPUT".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            matches[0].element.clone(),
        )
    };
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    let _ = target.set_timeout(0.1);
    let live_role = target
        .string_attribute(attr::AX_ROLE_ATTRIBUTE)
        .ok()
        .flatten()
        .unwrap_or_default();
    let live_subrole = target
        .string_attribute(attr::AX_SUBROLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_title = target
        .string_attribute(attr::AX_TITLE_ATTRIBUTE)
        .ok()
        .flatten();
    let live_description = target
        .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
        .ok()
        .flatten();
    let live_enabled = target
        .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_focused = target
        .bool_attribute(attr::AX_FOCUSED_ATTRIBUTE)
        .ok()
        .flatten();
    let live_hidden = target.bool_attribute("AXHidden").ok().flatten();
    if !accessible_label_matches(label, live_title, live_description)
        || !keypress_focus_role_allowed(&live_role, live_subrole.as_deref())
        || !keypress_target_enabled(&live_role, live_enabled)
        || live_focused != Some(true)
        || live_hidden == Some(true)
        || cached_bundle_id
            .as_deref()
            .map(|bundle_id| !frontmost_application_matches(cached_pid, bundle_id))
            .unwrap_or(true)
    {
        return Err("KEYPRESS_TARGET_CHANGED_BEFORE_INPUT".into());
    }
    post_keyboard_key_to_pid(cached_pid, key_code)?;
    thread::sleep(Duration::from_millis(120));
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    if observation.application_pid != Some(cached_pid)
        || observation.application_bundle_id != cached_bundle_id
        || observation.session_id != session_id
    {
        return Err("KEYPRESS_TARGET_CHANGED_AFTER_INPUT".into());
    }
    Ok(AccessibilityKeyResult {
        status: "PASS",
        // Semantic success is evaluated separately by the fresh AX verifier.
        verified: false,
        execution_attempted: true,
        key: key_name,
        observation,
    })
}

#[cfg(target_os = "macos")]
fn press_fresh_tab_macos(
    observation_id: &str,
    session_id: &str,
    application_pid: i32,
    current_focus_element_id: &str,
    current_focus_label: &str,
    target_element_id: &str,
    target_label: &str,
) -> Result<AccessibilityKeyResult, String> {
    use axuielement::ax_attribute::attributes as attr;

    if !is_trusted() {
        return Err("ACCESSIBILITY_PERMISSION_REQUIRED".into());
    }
    if !unsafe { CGPreflightPostEventAccess() } {
        return Err("POINTER_EVENT_PERMISSION_REQUIRED".into());
    }
    if application_pid <= 0 || current_focus_element_id == target_element_id {
        return Err("TAB_FOCUS_TARGET_IDENTITY_INVALID".into());
    }
    let cache = ACTION_SNAPSHOTS
        .get()
        .ok_or("ACCESSIBILITY_OBSERVATION_NOT_CACHED")?;
    let (cached_pid, cached_bundle_id, current_focus, target) = {
        let mut snapshots = cache
            .lock()
            .map_err(|_| "ACCESSIBILITY_ACTION_CACHE_LOCK_FAILED")?;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        snapshots.retain(|snapshot| {
            now >= snapshot.observed_at
                && now - snapshot.observed_at <= ACTION_STATE_MAX_AGE.as_secs_f64()
        });
        let snapshot = snapshots
            .iter()
            .find(|snapshot| snapshot.observation_id == observation_id)
            .ok_or("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN")?;
        if snapshot.session_id != session_id || snapshot.application_pid != application_pid {
            return Err("ACCESSIBILITY_OBSERVATION_SESSION_MISMATCH".into());
        }
        if now - snapshot.observed_at > 2.5 {
            return Err("ACCESSIBILITY_OBSERVATION_STALE_OR_UNKNOWN".into());
        }
        let current = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == current_focus_element_id
                    && element.label == current_focus_label
                    && element.visible == Some(true)
                    && element.focused == Some(true)
            })
            .collect::<Vec<_>>();
        let matches = snapshot
            .elements
            .iter()
            .filter(|element| {
                element.id == target_element_id
                    && element.label == target_label
                    && element.visible == Some(true)
                    && element.enabled == Some(true)
                    && element.focused != Some(true)
                    && tab_focus_role_allowed(&element.role)
            })
            .collect::<Vec<_>>();
        if current.len() != 1 || matches.len() != 1 {
            return Err("TAB_FOCUS_TARGET_CHANGED_BEFORE_INPUT".into());
        }
        (
            snapshot.application_pid,
            snapshot.application_bundle_id.clone(),
            current[0].element.clone(),
            matches[0].element.clone(),
        )
    };
    validate_cached_application_identity(cached_pid, cached_bundle_id.as_deref())?;
    for (element, label, must_be_focused) in [
        (&current_focus, current_focus_label, true),
        (&target, target_label, false),
    ] {
        let _ = element.set_timeout(0.1);
        let live_role = element
            .string_attribute(attr::AX_ROLE_ATTRIBUTE)
            .ok()
            .flatten()
            .unwrap_or_default();
        let live_title = element
            .string_attribute(attr::AX_TITLE_ATTRIBUTE)
            .ok()
            .flatten();
        let live_description = element
            .string_attribute(attr::AX_DESCRIPTION_ATTRIBUTE)
            .ok()
            .flatten();
        let live_visible = element.bool_attribute("AXHidden").ok().flatten() != Some(true);
        let live_enabled = element
            .bool_attribute(attr::AX_ENABLED_ATTRIBUTE)
            .ok()
            .flatten();
        let live_focused = element
            .bool_attribute(attr::AX_FOCUSED_ATTRIBUTE)
            .ok()
            .flatten();
        if !accessible_label_matches(label, live_title, live_description)
            || !live_visible
            || (!must_be_focused && live_enabled != Some(true))
            || live_focused != Some(must_be_focused)
            || (must_be_focused && live_role.is_empty())
            || (!must_be_focused && !tab_focus_role_allowed(&live_role))
        {
            return Err("TAB_FOCUS_TARGET_CHANGED_BEFORE_INPUT".into());
        }
    }
    post_keyboard_key_to_pid(cached_pid, 48).map_err(|_| "TAB_KEY_EVENT_CREATE_FAILED")?;
    thread::sleep(Duration::from_millis(120));
    let observation = observe_macos_with_menu_scope(Some(cached_pid), false)?;
    let verified = observation.application_pid == Some(cached_pid)
        && observation.application_bundle_id == cached_bundle_id
        && observation.session_id == session_id
        && observation.elements.iter().any(|element| {
            element.id == target_element_id
                && element.label == target_label
                && element.visible == Some(true)
                && element.enabled == Some(true)
                && element.focused == Some(true)
        })
        && observation
            .elements
            .iter()
            .any(|element| element.id == current_focus_element_id && element.focused != Some(true));
    Ok(AccessibilityKeyResult {
        status: if verified { "PASS" } else { "FAILED" },
        verified,
        execution_attempted: true,
        key: "Tab",
        observation,
    })
}

fn bounded_text(value: Option<String>) -> Option<String> {
    value
        .map(|text| {
            text.chars()
                .filter(|character| !character.is_control() || *character == ' ')
                .take(MAX_LABEL_CHARS)
                .collect::<String>()
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        })
        .filter(|text| !text.is_empty())
}

fn accessible_label(title: Option<String>, description: Option<String>) -> Option<String> {
    bounded_text(title).or_else(|| bounded_text(description))
}

fn accessible_observation_text(
    role: &str,
    title: Option<String>,
    description: Option<String>,
    static_value: Option<String>,
) -> Option<String> {
    accessible_label(title, description).or_else(|| {
        (role == "AXStaticText")
            .then(|| bounded_text(static_value))
            .flatten()
    })
}

fn accessible_label_matches(
    expected: &str,
    title: Option<String>,
    description: Option<String>,
) -> bool {
    accessible_label(title, description).as_deref() == Some(expected)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pointer_move_observation(elements: Vec<AccessibilityElement>) -> AccessibilityObservation {
        AccessibilityObservation {
            source: "AX",
            permission_granted: true,
            observation_id: "fresh-observation".into(),
            observed_at: 10.0,
            session_id: "macos-ax-42".into(),
            coordinate_space: "ax_global_screen_coordinates",
            width: None,
            height: None,
            confidence: 1.0,
            text: vec![],
            application_pid: Some(42),
            application_bundle_id: Some("local.fixture".into()),
            window_title: Some("Fixture".into()),
            window_frame: Some(AccessibilityRect {
                x: 0.0,
                y: 0.0,
                width: 800.0,
                height: 600.0,
            }),
            elements,
            truncated: false,
            uncertainty: vec![],
        }
    }

    fn pointer_move_element(id: &str, role: &str, label: &str) -> AccessibilityElement {
        AccessibilityElement {
            id: id.into(),
            role: role.into(),
            subrole: None,
            label: label.into(),
            frame: Some(AccessibilityRect {
                x: 100.0,
                y: 100.0,
                width: 120.0,
                height: 40.0,
            }),
            visible: Some(true),
            enabled: Some(true),
            focused: None,
            selected: None,
            expanded: (role == "AXDisclosureTriangle").then_some(false),
            scroll_value: None,
            supported_actions: vec![],
            keyboard_shortcut: None,
            depth: 1,
        }
    }

    #[test]
    fn moved_pointer_must_match_unique_fresh_button_identity_and_frame() {
        let observation =
            pointer_move_observation(vec![pointer_move_element("button-1", "AXButton", "Start")]);
        assert!(validate_moved_pointer_target(
            &observation,
            42,
            "local.fixture",
            "button-1",
            "Start",
            "AXButton",
            [110.0, 110.0],
            None,
        )
        .is_ok());
        assert_eq!(
            validate_moved_pointer_target(
                &observation,
                42,
                "local.fixture",
                "button-other",
                "Start",
                "AXButton",
                [110.0, 110.0],
                None,
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_NOT_UNIQUE"
        );
        assert_eq!(
            validate_moved_pointer_target(
                &observation,
                42,
                "local.fixture",
                "button-1",
                "Start",
                "AXButton",
                [300.0, 110.0],
                None,
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_CHANGED_OR_POINT_MISALIGNED"
        );
    }

    #[test]
    fn moved_pointer_uses_grounded_visual_frame_when_ax_button_geometry_is_missing() {
        let mut element = pointer_move_element("button-1", "AXButton", "Start");
        element.frame = Some(AccessibilityRect {
            x: 0.0,
            y: 0.0,
            width: 0.0,
            height: 0.0,
        });
        let observation = pointer_move_observation(vec![element]);
        let tracked_visual_frame = AccessibilityRect {
            x: 140.0,
            y: 100.0,
            width: 40.0,
            height: 40.0,
        };

        assert!(validate_moved_pointer_target(
            &observation,
            42,
            "local.fixture",
            "button-1",
            "Start",
            "AXButton",
            [160.0, 120.0],
            Some(&tracked_visual_frame),
        )
        .is_ok());
        assert_eq!(
            validate_moved_pointer_target(
                &observation,
                42,
                "local.fixture",
                "button-1",
                "Start",
                "AXButton",
                [220.0, 120.0],
                Some(&tracked_visual_frame),
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_CHANGED_OR_POINT_MISALIGNED"
        );
        assert_eq!(
            validate_moved_pointer_target(
                &observation,
                42,
                "local.fixture",
                "button-1",
                "Start",
                "AXDisclosureTriangle",
                [160.0, 120.0],
                Some(&tracked_visual_frame),
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_NOT_UNIQUE"
        );
    }

    #[test]
    fn moved_pointer_disclosure_must_still_be_closed_and_axpress_unavailable() {
        let observation = pointer_move_observation(vec![pointer_move_element(
            "disclosure-1",
            "AXDisclosureTriangle",
            "Details",
        )]);
        assert!(validate_moved_pointer_target(
            &observation,
            42,
            "local.fixture",
            "disclosure-1",
            "Details",
            "AXDisclosureTriangle",
            [110.0, 110.0],
            None,
        )
        .is_ok());
        let mut opened = observation.clone();
        opened.elements[0].expanded = Some(true);
        assert_eq!(
            validate_moved_pointer_target(
                &opened,
                42,
                "local.fixture",
                "disclosure-1",
                "Details",
                "AXDisclosureTriangle",
                [110.0, 110.0],
                None,
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_CHANGED_OR_POINT_MISALIGNED"
        );
        let mut semantic = observation;
        semantic.elements[0].supported_actions = vec!["AXPress".into()];
        assert_eq!(
            validate_moved_pointer_target(
                &semantic,
                42,
                "local.fixture",
                "disclosure-1",
                "Details",
                "AXDisclosureTriangle",
                [110.0, 110.0],
                None,
            )
            .unwrap_err(),
            "POINTER_MOVE_TARGET_CHANGED_OR_POINT_MISALIGNED"
        );
    }

    #[test]
    fn text_entry_allows_only_non_secure_text_fields_and_areas() {
        assert!(text_entry_role_allowed("AXTextField", None));
        assert!(text_entry_role_allowed("AXTextArea", None));
        assert!(!text_entry_role_allowed(
            "AXTextField",
            Some("AXSecureTextField")
        ));
        assert!(!text_entry_role_allowed("AXSecureTextField", None));
        assert!(!text_entry_role_allowed("AXComboBox", None));
        assert!(!text_entry_role_allowed("AXButton", None));
    }

    #[test]
    fn chat_keypress_is_allowlisted_and_never_targets_secure_fields() {
        assert_eq!(Some(("Enter", 36)), bounded_chat_key("Enter"));
        assert_eq!(Some(("Space", 49)), bounded_chat_key("Space"));
        assert_eq!(Some(("ArrowLeft", 123)), bounded_chat_key("ArrowLeft"));
        assert_eq!(Some(("ArrowRight", 124)), bounded_chat_key("ArrowRight"));
        assert_eq!(Some(("ArrowDown", 125)), bounded_chat_key("ArrowDown"));
        assert_eq!(Some(("ArrowUp", 126)), bounded_chat_key("ArrowUp"));
        assert_eq!(Some(("Escape", 53)), bounded_chat_key("Escape"));
        assert_eq!(None, bounded_chat_key("Delete"));
        assert_eq!(None, bounded_chat_key("a"));
        assert!(keypress_focus_role_allowed("AXWindow", None));
        assert!(keypress_focus_role_allowed("AXTextField", None));
        assert!(!keypress_focus_role_allowed(
            "AXSecureTextField",
            Some("AXSecureTextField")
        ));
        assert!(!keypress_focus_role_allowed(
            "AXButton",
            Some("AXSecureTextField")
        ));
        assert!(keypress_target_enabled("AXWindow", None));
        assert!(keypress_target_enabled("AXWebArea", Some(true)));
        assert!(!keypress_target_enabled("AXGroup", Some(false)));
        assert!(!keypress_target_enabled("AXButton", None));
    }

    #[test]
    fn tab_focus_accepts_only_known_focusable_ax_roles() {
        assert!(tab_focus_role_allowed("AXTextField"));
        assert!(tab_focus_role_allowed("AXButton"));
        assert!(tab_focus_role_allowed("AXLink"));
        assert!(!tab_focus_role_allowed("AXStaticText"));
        assert!(!tab_focus_role_allowed("AXWindow"));
    }

    #[test]
    fn vertical_scrollbar_geometry_matches_the_same_scroll_area() {
        let area = (10.0, 20.0, 400.0, 300.0);
        let adjacent_scrollbar = (398.0, 30.0, 12.0, 280.0);
        let unrelated_scrollbar = (700.0, 30.0, 12.0, 280.0);
        let horizontal_scrollbar = (20.0, 310.0, 360.0, 12.0);

        assert!(scrollbar_frame_belongs_to_area(&area, &adjacent_scrollbar));
        assert!(!scrollbar_frame_belongs_to_area(
            &area,
            &unrelated_scrollbar
        ));
        assert!(!scrollbar_frame_belongs_to_area(
            &area,
            &horizontal_scrollbar
        ));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn explicit_application_bundle_is_validated_by_path_and_bundle_identity() {
        use std::time::{SystemTime, UNIX_EPOCH};

        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let bundle = std::env::temp_dir()
            .join(format!("OLCR app path validation {suffix}.app"))
            .join("Contents");
        std::fs::create_dir_all(&bundle).unwrap();
        std::fs::write(
            bundle.join("Info.plist"),
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>local.olcr.fixture.unregistered</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>"#,
        )
        .unwrap();
        let bundle_path = bundle.parent().unwrap();
        let validated = validated_application_bundle_path(
            "local.olcr.fixture.unregistered",
            bundle_path.to_str().unwrap(),
        )
        .unwrap();
        assert_eq!(validated, bundle_path.canonicalize().unwrap());
        assert_eq!(
            validated_application_bundle_path(
                "local.olcr.fixture.other",
                bundle_path.to_str().unwrap(),
            )
            .unwrap_err(),
            "COMPUTER_USE_APP_BUNDLE_ID_MISMATCH"
        );
        std::fs::write(
            bundle.join("Info.plist"),
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>local.olcr.fixture.unregistered</string><key>CFBundlePackageType</key><string>BNDL</string></dict></plist>"#,
        )
        .unwrap();
        assert_eq!(
            validated_application_bundle_path(
                "local.olcr.fixture.unregistered",
                bundle_path.to_str().unwrap(),
            )
            .unwrap_err(),
            "COMPUTER_USE_APP_BUNDLE_INVALID"
        );
        std::fs::remove_dir_all(bundle_path).unwrap();
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn exact_path_launch_uses_launch_services_without_bundle_id_lookup() {
        use std::ffi::OsStr;
        use std::path::Path;

        let path = Path::new("/tmp/project with spaces/Game.app");
        let command = open_application_command_at_path(path);
        assert_eq!(command.get_program(), OsStr::new("/usr/bin/open"));
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![OsStr::new("-n"), path.as_os_str()]
        );
    }

    #[test]
    fn missing_axhidden_requires_contained_control_and_nonminimized_window() {
        let window = AccessibilityRect {
            x: 100.0,
            y: 100.0,
            width: 400.0,
            height: 300.0,
        };
        let button = AccessibilityRect {
            x: 150.0,
            y: 150.0,
            width: 90.0,
            height: 24.0,
        };
        assert_eq!(
            observed_element_visibility(
                None,
                "AXButton",
                Some(&button),
                Some(&window),
                Some(false)
            ),
            Some(true)
        );
        assert_eq!(
            observed_element_visibility(
                Some(true),
                "AXButton",
                Some(&button),
                Some(&window),
                Some(false)
            ),
            Some(false)
        );
        assert_eq!(
            observed_element_visibility(None, "AXButton", Some(&button), Some(&window), Some(true)),
            None
        );
        let text_field = AccessibilityRect {
            x: 120.0,
            y: 120.0,
            width: 220.0,
            height: 30.0,
        };
        assert_eq!(
            observed_element_visibility(
                None,
                "AXTextField",
                Some(&text_field),
                Some(&window),
                Some(false)
            ),
            Some(true)
        );
        assert_eq!(
            observed_element_visibility(
                Some(true),
                "AXTextField",
                Some(&text_field),
                Some(&window),
                Some(false)
            ),
            Some(false)
        );
        assert_eq!(
            observed_element_visibility(
                None,
                "AXTextField",
                Some(&text_field),
                Some(&window),
                Some(true)
            ),
            None
        );
        assert_eq!(
            observed_element_visibility(None, "AXButton", Some(&button), Some(&window), None),
            None
        );
        assert_eq!(
            observed_element_visibility(
                None,
                "AXMenuItem",
                Some(&button),
                Some(&window),
                Some(false)
            ),
            None
        );
        let outside = AccessibilityRect {
            x: 490.0,
            ..button.clone()
        };
        assert_eq!(
            observed_element_visibility(
                None,
                "AXButton",
                Some(&outside),
                Some(&window),
                Some(false)
            ),
            None
        );
    }

    #[test]
    fn action_snapshot_keeps_window_controls_under_api_byte_limit() {
        let elements = (0..MAX_ELEMENTS)
            .map(|index| AccessibilityElement {
                id: format!("ax-{index}"),
                role: "AXButton".into(),
                subrole: None,
                label: if index == 0 {
                    "Start game".into()
                } else {
                    "日本語メニュー".repeat(20)
                },
                frame: Some(AccessibilityRect {
                    x: 10.0,
                    y: 20.0,
                    width: 90.0,
                    height: 30.0,
                }),
                visible: Some(true),
                enabled: Some(true),
                focused: Some(false),
                selected: None,
                expanded: None,
                scroll_value: None,
                supported_actions: vec!["AXPress".into()],
                keyboard_shortcut: None,
                depth: 2,
            })
            .collect::<Vec<_>>();
        let mut observation = AccessibilityObservation {
            source: "AX",
            permission_granted: true,
            observation_id: "ax-fixture".into(),
            observed_at: 1000.0,
            session_id: "macos-ax-123".into(),
            coordinate_space: "ax_global_screen_coordinates",
            width: None,
            height: None,
            confidence: 0.95,
            text: elements
                .iter()
                .map(|element| element.label.clone())
                .collect(),
            application_pid: Some(123),
            application_bundle_id: Some("local.example.game".into()),
            window_title: Some("Game".into()),
            window_frame: None,
            elements,
            truncated: false,
            uncertainty: vec![],
        };
        bound_observation_bytes(&mut observation);
        assert!(observation.truncated);
        assert_eq!(observation.elements[0].label, "Start game");
        assert_eq!(observation.text.len(), observation.elements.len());
        let value = serde_json::to_string(&observation).unwrap();
        assert!(value.len() <= MAX_OBSERVATION_BYTES);
        // Python json.dumps adds spaces after punctuation. This conservative
        // count also charges punctuation inside labels, retaining headroom.
        let backend_size = value.len() + value.matches([',', ':']).count() + 100;
        assert!(backend_size < 24_000);
        assert!(observation
            .uncertainty
            .contains(&"AX_SNAPSHOT_BYTE_BUDGET_REACHED".into()));
    }

    #[test]
    fn accessibility_monitor_accepts_allowlisted_perception_events_only() {
        assert!(is_perception_notification("AXFocusedUIElementChanged"));
        assert!(is_perception_notification("AXFocusedWindowChanged"));
        assert!(is_perception_notification("AXMainWindowChanged"));
        assert!(is_perception_notification("AXWindowCreated"));
        assert!(is_perception_notification("AXWindowMoved"));
        assert!(is_perception_notification("AXWindowResized"));
        assert!(is_perception_notification("AXValueChanged"));
        assert!(is_perception_notification("AXSelectedChildrenChanged"));
        assert!(is_perception_notification("AXSelectedTextChanged"));
        assert!(!is_perception_notification("AXMenuOpened"));
    }

    #[test]
    fn accessibility_label_is_bounded_and_control_characters_are_removed() {
        let value = format!("button\u{0007}  {}", "x".repeat(MAX_LABEL_CHARS + 20));
        let normalized = bounded_text(Some(value)).unwrap();
        assert!(!normalized.chars().any(char::is_control));
        assert!(normalized.chars().count() <= MAX_LABEL_CHARS + 7);
    }

    #[test]
    fn empty_accessibility_labels_are_omitted() {
        assert!(bounded_text(None).is_none());
        assert!(bounded_text(Some(" \n\t ".into())).is_none());
    }

    #[test]
    fn accessibility_description_fills_only_a_missing_title() {
        assert_eq!(
            Some("Primary title".into()),
            accessible_label(
                Some("Primary title".into()),
                Some("Secondary description".into())
            )
        );
        assert_eq!(
            Some("Accessible button".into()),
            accessible_label(None, Some("Accessible button".into()))
        );
        assert_eq!(
            Some("Accessible button".into()),
            accessible_label(Some(" ".into()), Some("Accessible button".into()))
        );
    }

    #[test]
    fn observation_reads_static_display_text_but_never_editable_values() {
        assert_eq!(
            Some("Game started".into()),
            accessible_observation_text("AXStaticText", None, None, Some("Game started".into()))
        );
        assert_eq!(
            Some("Named status".into()),
            accessible_observation_text(
                "AXStaticText",
                Some("Named status".into()),
                None,
                Some("ignored value".into())
            )
        );
        assert_eq!(
            None,
            accessible_observation_text("AXTextField", None, None, Some("private input".into()))
        );
        assert_eq!(
            None,
            accessible_observation_text("AXSecureTextField", None, None, Some("secret".into()))
        );
    }

    #[test]
    fn action_label_revalidation_uses_the_same_title_then_description_rule() {
        assert!(accessible_label_matches(
            "Accessible disclosure",
            None,
            Some("Accessible disclosure".into())
        ));
        assert!(accessible_label_matches(
            "Primary title",
            Some("Primary title".into()),
            Some("Different description".into())
        ));
        assert!(!accessible_label_matches(
            "Description label",
            Some("Different title".into()),
            Some("Description label".into())
        ));
    }

    #[test]
    fn window_selection_requires_content_and_known_non_minimized_state_for_window_list() {
        assert!(accessible_window_candidate(
            "AXWindow",
            Some(2),
            None,
            false
        ));
        assert!(!accessible_window_candidate(
            "AXMenuBar",
            Some(2),
            Some(false),
            false
        ));
        assert!(!accessible_window_candidate(
            "AXWindow",
            Some(0),
            Some(false),
            false
        ));
        assert!(!accessible_window_candidate(
            "AXWindow",
            None,
            Some(false),
            false
        ));
        assert!(accessible_window_candidate(
            "AXWindow",
            Some(1),
            Some(false),
            true
        ));
        assert!(!accessible_window_candidate(
            "AXWindow",
            Some(1),
            None,
            true
        ));
        assert!(!accessible_window_candidate(
            "AXWindow",
            Some(1),
            Some(true),
            true
        ));
    }

    #[test]
    fn accessibility_press_allows_only_enabled_collapsed_disclosures_with_axpress() {
        let press = vec!["AXPress".to_string()];
        assert!(disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXButton",
            Some(true),
            Some(false),
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            None,
            Some(false),
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(false),
            Some(false),
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            None,
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(true),
            true,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            false,
            &press
        ));
        assert!(!disclosure_press_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            true,
            &[]
        ));
    }

    #[test]
    fn pointer_fallback_allows_only_fresh_closed_enabled_disclosures_without_axpress() {
        let frame = Some((120.0, 80.0, 14.0, 14.0));
        assert!(disclosure_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            frame,
            &[]
        ));
        assert!(!disclosure_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            frame,
            &["AXPress".into()]
        ));
        assert!(!disclosure_pointer_is_allowed(
            "AXButton",
            Some(true),
            Some(false),
            frame,
            &[]
        ));
        assert!(!disclosure_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(false),
            Some(false),
            frame,
            &[]
        ));
        assert!(!disclosure_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(true),
            frame,
            &[]
        ));
    }

    #[test]
    fn vision_pointer_gate_allows_only_bounded_closed_disclosures_without_axpress() {
        assert!(disclosure_vision_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            &[],
            (240.0, 180.0),
        ));
        assert!(!disclosure_vision_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            &["AXPress".into()],
            (240.0, 180.0),
        ));
        assert!(!disclosure_vision_pointer_is_allowed(
            "AXButton",
            Some(true),
            Some(false),
            &[],
            (240.0, 180.0),
        ));
        assert!(!disclosure_vision_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(true),
            &[],
            (240.0, 180.0),
        ));
        assert!(!disclosure_vision_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            Some(false),
            &[],
            (f64::NAN, 180.0),
        ));
    }

    #[test]
    fn vision_pointer_button_requires_enabled_axbutton_without_axpress_and_finite_point() {
        assert!(button_vision_pointer_is_allowed(
            "AXButton",
            Some(true),
            &[],
            (240.0, 180.0)
        ));
        assert!(!button_vision_pointer_is_allowed(
            "AXButton",
            Some(true),
            &["AXPress".into()],
            (240.0, 180.0)
        ));
        assert!(!button_vision_pointer_is_allowed(
            "AXDisclosureTriangle",
            Some(true),
            &[],
            (240.0, 180.0)
        ));
        assert!(!button_vision_pointer_is_allowed(
            "AXButton",
            Some(false),
            &[],
            (240.0, 180.0)
        ));
        assert!(!button_vision_pointer_is_allowed(
            "AXButton",
            Some(true),
            &[],
            (f64::NAN, 180.0)
        ));
        assert!(!button_vision_pointer_is_allowed(
            "AXButton",
            Some(true),
            &[],
            (100_001.0, 180.0)
        ));
    }

    #[test]
    fn stale_vision_observation_refresh_resolves_only_one_current_enabled_button() {
        let button =
            |id: &str, label: &str, visible, enabled, supported_actions| AccessibilityElement {
                id: id.into(),
                role: "AXButton".into(),
                subrole: None,
                label: label.into(),
                frame: None,
                visible,
                enabled,
                focused: None,
                selected: None,
                expanded: None,
                scroll_value: None,
                supported_actions,
                keyboard_shortcut: None,
                depth: 0,
            };
        assert_eq!(
            unique_fresh_vision_button_id(
                &[button(
                    "new-ax-id",
                    "Start tracking demo",
                    Some(true),
                    Some(true),
                    vec![]
                )],
                "Start tracking demo",
            )
            .unwrap(),
            "new-ax-id"
        );
        assert_eq!(
            unique_fresh_vision_button_id(
                &[
                    button(
                        "left",
                        "Start tracking demo",
                        Some(true),
                        Some(true),
                        vec![]
                    ),
                    button(
                        "right",
                        "Start tracking demo",
                        Some(true),
                        Some(true),
                        vec![]
                    ),
                ],
                "Start tracking demo",
            )
            .unwrap_err(),
            "VISION_POINTER_FRESH_TARGET_NOT_UNIQUE"
        );
        assert_eq!(
            unique_fresh_vision_button_id(
                &[button(
                    "start",
                    "Start tracking demo",
                    Some(true),
                    Some(true),
                    vec!["AXPress".into()],
                )],
                "Start tracking demo",
            )
            .unwrap_err(),
            "ACCESSIBILITY_AXPRESS_AVAILABLE_USE_SEMANTIC_ACTION"
        );
    }

    #[test]
    fn pointer_frame_center_rejects_malformed_geometry_and_preserves_global_origin() {
        assert_eq!(
            pointer_frame_center((-1440.0, 8.0, 20.0, 12.0)),
            Ok((-1430.0, 14.0))
        );
        assert_eq!(
            pointer_frame_center((0.0, 0.0, f64::NAN, 10.0)),
            Err("ACCESSIBILITY_TARGET_FRAME_INVALID")
        );
        assert_eq!(
            pointer_frame_center((0.0, 0.0, 0.0, 10.0)),
            Err("ACCESSIBILITY_TARGET_FRAME_INVALID")
        );
        assert_eq!(
            pointer_frame_center((0.0, 0.0, 32_769.0, 10.0)),
            Err("ACCESSIBILITY_TARGET_FRAME_INVALID")
        );
    }

    #[test]
    fn grounded_pointer_point_must_stay_inside_fresh_ax_window_bounds() {
        let window = AccessibilityRect {
            x: -800.0,
            y: 40.0,
            width: 600.0,
            height: 400.0,
        };
        assert!(point_is_inside_accessibility_rect((-500.0, 240.0), &window));
        assert!(!point_is_inside_accessibility_rect(
            (-200.0, 240.0),
            &window
        ));
        assert!(!point_is_inside_accessibility_rect(
            (-500.0, 440.0),
            &window
        ));
        let malformed = AccessibilityRect {
            width: f64::NAN,
            ..window
        };
        assert!(!point_is_inside_accessibility_rect(
            (-500.0, 240.0),
            &malformed
        ));
    }

    #[test]
    fn tracked_pointer_may_follow_a_moving_button_only_inside_its_live_ax_frame() {
        let moved_frame = (420.0, 220.0, 180.0, 48.0);
        assert!(!valid_pointer_frame(&(0.0, 0.0, 0.0, 0.0)));
        assert_eq!(usable_pointer_frame(Some((0.0, 0.0, 0.0, 0.0))), None);
        assert_eq!(usable_pointer_frame(Some(moved_frame)), Some(moved_frame));
        assert!(valid_pointer_frame(&moved_frame));
        assert!(tracked_point_is_inside_live_button_frame(
            (500.0, 244.0),
            moved_frame
        ));
        assert!(!tracked_point_is_inside_live_button_frame(
            (320.0, 244.0),
            moved_frame
        ));
        assert!(!tracked_point_is_inside_live_button_frame(
            (500.0, 244.0),
            (420.0, 220.0, f64::NAN, 48.0)
        ));
    }

    #[test]
    fn pointer_center_must_fall_inside_an_active_display_rect() {
        assert!(point_is_inside_display_bounds(
            (-1430.0, 14.0),
            (-1440.0, 0.0),
            (1440.0, 900.0)
        ));
        assert!(!point_is_inside_display_bounds(
            (-1.0, 14.0),
            (0.0, 0.0),
            (1440.0, 900.0)
        ));
        assert!(!point_is_inside_display_bounds(
            (1440.0, 14.0),
            (0.0, 0.0),
            (1440.0, 900.0)
        ));
        assert!(!point_is_inside_display_bounds(
            (1.0, 1.0),
            (0.0, 0.0),
            (f64::NAN, 900.0)
        ));
    }

    #[test]
    fn window_capture_geometry_can_be_smaller_than_its_active_display() {
        let target_window = (-800.0, 120.0, 600.0, 500.0);
        let active_display = (-1440.0, 0.0, 2880.0, 1800.0);
        let mapped_point = (-500.0, 240.0);

        assert_ne!(target_window, active_display);
        assert!(point_is_inside_display_bounds(
            mapped_point,
            (target_window.0, target_window.1),
            (target_window.2, target_window.3)
        ));
        assert!(point_is_inside_display_bounds(
            mapped_point,
            (active_display.0, active_display.1),
            (active_display.2, active_display.3)
        ));
    }

    #[test]
    fn discovered_shortcut_sequence_releases_modifier_and_rejects_ambiguous_masks() {
        let command = find_shortcut_events(3, 0).unwrap();
        assert_eq!(4, command.len());
        assert_eq!(55, command[0].key_code);
        assert!(command[0].key_down);
        assert_eq!(3, command[1].key_code);
        assert_eq!(3, command[2].key_code);
        assert!(!command[2].key_down);
        assert!(!command[3].key_down);
        assert_eq!(0, command[3].flags);
        let no_command = find_shortcut_events(3, 8).unwrap();
        assert_eq!(2, no_command.len());
        assert!(no_command.iter().all(|event| event.flags == 0));
        assert!(find_shortcut_events(3, 9).is_err());
        assert!(find_shortcut_events(128, 0).is_err());
    }

    #[test]
    fn find_shortcut_menu_label_allowlist_is_narrow_and_case_insensitive() {
        assert!(safe_find_menu_label("Find…"));
        assert!(safe_find_menu_label("SEARCH"));
        assert!(safe_find_menu_label("検索"));
        assert!(!safe_find_menu_label("Find and Replace"));
        assert!(!safe_find_menu_label("Delete"));
    }

    #[test]
    fn accessibility_snapshot_serializes_with_existing_screen_state_provenance_keys() {
        let observation = AccessibilityObservation {
            source: "AX",
            permission_granted: true,
            observation_id: "ax-123-456".into(),
            observed_at: 1.0,
            session_id: "macos-ax-123".into(),
            coordinate_space: "ax_global_screen_coordinates",
            width: None,
            height: None,
            confidence: 0.95,
            text: vec!["Continue".into()],
            application_pid: Some(123),
            application_bundle_id: Some("com.example.demo".into()),
            window_title: Some("Demo".into()),
            window_frame: None,
            elements: vec![AccessibilityElement {
                id: "ax-0".into(),
                role: "AXButton".into(),
                subrole: None,
                label: "Continue".into(),
                frame: None,
                visible: Some(true),
                enabled: Some(true),
                focused: Some(false),
                selected: None,
                expanded: Some(false),
                scroll_value: None,
                supported_actions: vec!["AXPress".into()],
                keyboard_shortcut: None,
                depth: 1,
            }],
            truncated: false,
            uncertainty: vec![],
        };
        let value = serde_json::to_value(observation).unwrap();
        assert_eq!(value["source"], "AX");
        assert_eq!(value["observationId"], "ax-123-456");
        assert_eq!(value["sessionId"], "macos-ax-123");
        assert_eq!(value["applicationBundleId"], "com.example.demo");
        assert_eq!(value["coordinateSpace"], "ax_global_screen_coordinates");
        assert!(value["width"].is_null());
        assert!(value["height"].is_null());
        assert_eq!(value["elements"][0]["role"], "AXButton");
        assert_eq!(value["elements"][0]["label"], "Continue");
        assert!(value["elements"][0]["supportedActions"].is_array());
        assert!(value["elements"][0]["keyboardShortcut"].is_null());
        assert!(value["elements"][0].get("value").is_none());
    }

    #[test]
    fn accessibility_change_event_contains_only_invalidation_metadata() {
        let event = AccessibilityChange {
            source: "AX",
            notification: "AXFocusedUIElementChanged".into(),
            application_pid: 123,
            session_id: "macos-ax-123".into(),
            observed_at: 2.0,
        };
        let value = serde_json::to_value(event).unwrap();
        assert_eq!(value["notification"], "AXFocusedUIElementChanged");
        assert_eq!(value["applicationPid"], 123);
        assert_eq!(value["sessionId"], "macos-ax-123");
        assert!(value.get("text").is_none());
        assert!(value.get("value").is_none());
    }
}
