//! User-started, bounded ScreenCaptureKit stream.
//!
//! The native stream runs for the lifetime of one explicit capture session.
//! Its callback retains only the latest downscaled frame; listeners receive a
//! notice and pull that latest frame when perception needs it.

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

const MAX_WIDTH: usize = 1920;
const MAX_HEIGHT: usize = 1080;
const TARGET_FPS: u32 = 2;
const ADAPTIVE_CAPTURE_FPS_CEILING: u32 = 30;
const JPEG_QUALITY: u8 = 65;
const SENTINEL_WIDTH: u32 = 64;
const SENTINEL_HEIGHT: u32 = 36;
const PIXEL_DELTA_THRESHOLD: u8 = 20;
const CHANGED_PIXEL_MINIMUM: u32 = 2;
const SCENE_CHANGE_RATIO: f64 = 0.35;
const PROCESSING_SAMPLE_CAPACITY: usize = 120;
const CAPTURE_RATE_SAMPLE_CAPACITY: usize = 120;
const CAPTURE_RATE_SAMPLE_WINDOW: Duration = Duration::from_secs(5);
const MINIMUM_REGION_PIXELS: u32 = 1;
const MAX_CHANGED_REGIONS: usize = 8;
const MAX_TRACKING_FRAME_GAP_SECONDS: f64 = 2.5;
// Grounding and chat-driven action planning can take several seconds before
// the one-shot seed reaches the native tracker. Allow up to ten seconds for
// this handoff; the pointer executor still requires a fresh tracked frame and
// recomputes the transform immediately before input.
const MAX_GROUNDING_SEED_AGE_SECONDS: f64 = 10.0;
static NEXT_CAPTURE_SESSION_ID: AtomicU64 = AtomicU64::new(1);

#[cfg(target_os = "macos")]
fn screen_capture_access_preflight() -> bool {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
    }

    // SAFETY: this CoreGraphics preflight query has no pointers or ownership
    // requirements and does not request or mutate the user's TCC decision.
    unsafe { CGPreflightScreenCaptureAccess() }
}

pub fn screen_capture_access_status() -> bool {
    #[cfg(target_os = "macos")]
    {
        return screen_capture_access_preflight();
    }
    #[cfg(not(target_os = "macos"))]
    false
}

#[cfg(target_os = "macos")]
fn request_screen_capture_access() -> bool {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGRequestScreenCaptureAccess() -> bool;
    }

    // SAFETY: this CoreGraphics API is called only from the explicit Settings
    // permission action, never as a side effect of starting a chat task.
    unsafe { CGRequestScreenCaptureAccess() }
}

pub fn request_screen_capture_permission() -> bool {
    #[cfg(target_os = "macos")]
    {
        if !screen_capture_access_preflight() {
            let _ = request_screen_capture_access();
        }
        return screen_capture_access_preflight();
    }
    #[cfg(not(target_os = "macos"))]
    false
}

pub fn supports_runtime_capture_rate_updates() -> bool {
    #[cfg(target_os = "macos")]
    {
        use objc2_foundation::{NSOperatingSystemVersion, NSProcessInfo};

        return NSProcessInfo::processInfo().isOperatingSystemAtLeastVersion(
            NSOperatingSystemVersion {
                majorVersion: 14,
                minorVersion: 0,
                patchVersion: 0,
            },
        );
    }
    #[cfg(not(target_os = "macos"))]
    false
}

pub fn capture_fps_ceiling() -> u32 {
    if supports_runtime_capture_rate_updates() {
        ADAPTIVE_CAPTURE_FPS_CEILING
    } else {
        TARGET_FPS
    }
}

fn apply_capture_rate_policy(
    target_fps: u32,
    compute_level_target: u8,
    policy_revision: u64,
    configured_capture_fps: &AtomicU64,
    configured_compute_level: &AtomicU64,
    applied_policy_revision: &AtomicU64,
    fps_ceiling: u32,
    runtime_updates_supported: bool,
    update_stream: impl FnOnce(u32) -> Result<(), String>,
) -> Result<(), String> {
    if policy_revision < applied_policy_revision.load(Ordering::Acquire) {
        return Ok(());
    }
    applied_policy_revision.store(policy_revision, Ordering::Release);
    configured_compute_level.store(compute_level_target.min(4) as u64, Ordering::Release);

    let desired_fps = target_fps.clamp(TARGET_FPS, fps_ceiling.max(TARGET_FPS));
    let configured_fps = configured_capture_fps.load(Ordering::Relaxed) as u32;
    if desired_fps == configured_fps || !runtime_updates_supported {
        return Ok(());
    }

    update_stream(desired_fps)?;
    configured_capture_fps.store(desired_fps as u64, Ordering::Release);
    Ok(())
}

fn new_capture_session_id() -> String {
    let time_nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let counter = NEXT_CAPTURE_SESSION_ID.fetch_add(1, Ordering::Relaxed);
    format!("{:x}-{:x}-{:x}", std::process::id(), time_nonce, counter)
}

#[derive(Clone)]
struct CapturedFrame {
    capture_session_id: String,
    target_application: Option<CaptureTargetApplication>,
    sequence: u64,
    observed_at: f64,
    width: usize,
    height: usize,
    display_geometry: CaptureDisplayGeometry,
    rgb: Arc<Vec<u8>>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CaptureTargetApplication {
    pub application_pid: i32,
    pub application_bundle_id: String,
}

/// Point-space bounds and identity of the display which produced a captured frame.
/// `origin_x/y` use the global Quartz/Accessibility screen space with origin at the
/// upper-left of the menu-bar display. Frame pixels can be Retina or downscaled;
/// normalized coordinates map through these logical point bounds.
#[derive(Clone, Copy, Debug, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct CaptureDisplayGeometry {
    pub display_id: u32,
    pub origin_x: f64,
    pub origin_y: f64,
    pub width_points: f64,
    pub height_points: f64,
}

impl CaptureDisplayGeometry {
    fn is_valid(self) -> bool {
        self.display_id != 0
            && self.origin_x.is_finite()
            && self.origin_y.is_finite()
            && self.width_points.is_finite()
            && self.height_points.is_finite()
            && self.width_points > 0.0
            && self.height_points > 0.0
    }
}

/// Map an image-relative box center to global macOS screen points, provided the
/// image dimensions are consistent with the captured display's aspect ratio.
pub fn map_normalized_bbox_to_global_point(
    geometry: CaptureDisplayGeometry,
    image_width: usize,
    image_height: usize,
    bbox: [f64; 4],
) -> Result<[f64; 2], &'static str> {
    if !geometry.is_valid() || image_width == 0 || image_height == 0 {
        return Err("SCREEN_POINTER_DISPLAY_GEOMETRY_INVALID");
    }
    if bbox
        .iter()
        .any(|value| !value.is_finite() || !(0.0..=1.0).contains(value))
        || bbox[0] >= bbox[2]
        || bbox[1] >= bbox[3]
    {
        return Err("SCREEN_POINTER_BBOX_INVALID");
    }
    let image_aspect = image_width as f64 / image_height as f64;
    let display_aspect = geometry.width_points / geometry.height_points;
    let aspect_error = (image_aspect - display_aspect).abs() / display_aspect;
    if !aspect_error.is_finite() || aspect_error > 0.01 {
        return Err("SCREEN_POINTER_DISPLAY_ASPECT_MISMATCH");
    }
    let x = geometry.origin_x + ((bbox[0] + bbox[2]) / 2.0) * geometry.width_points;
    let y = geometry.origin_y + ((bbox[1] + bbox[3]) / 2.0) * geometry.height_points;
    if !x.is_finite() || !y.is_finite() {
        return Err("SCREEN_POINTER_COORDINATE_INVALID");
    }
    Ok([x, y])
}

fn validate_tracking_frames_with_max_gap(
    first: &CapturedFrame,
    next: &CapturedFrame,
    max_gap_seconds: f64,
) -> Result<(), String> {
    if first.capture_session_id != next.capture_session_id {
        return Err("SCREEN_CAPTURE_TRACKING_SESSION_MISMATCH".into());
    }
    if first.width != next.width || first.height != next.height {
        return Err("SCREEN_CAPTURE_TRACKING_DIMENSIONS_MISMATCH".into());
    }
    if first.display_geometry != next.display_geometry {
        return Err("SCREEN_CAPTURE_TRACKING_DISPLAY_MISMATCH".into());
    }
    if first.target_application != next.target_application {
        return Err("SCREEN_CAPTURE_TRACKING_TARGET_MISMATCH".into());
    }
    if !first.observed_at.is_finite()
        || !next.observed_at.is_finite()
        || next.observed_at <= first.observed_at
        || next.observed_at - first.observed_at > max_gap_seconds
    {
        return Err("SCREEN_CAPTURE_TRACKING_FRAMES_STALE".into());
    }
    Ok(())
}

#[cfg(test)]
fn validate_tracking_frames(first: &CapturedFrame, next: &CapturedFrame) -> Result<(), String> {
    validate_tracking_frames_with_max_gap(first, next, MAX_TRACKING_FRAME_GAP_SECONDS)
}

fn validate_grounding_seed_pair(
    seed: &CapturedFrame,
    next: &CapturedFrame,
    requested_seed_sequence: u64,
) -> Result<(), String> {
    if seed.sequence != requested_seed_sequence {
        return Err("SCREEN_CAPTURE_TRACKING_SEED_SEQUENCE_MISMATCH".into());
    }
    if next.sequence <= seed.sequence {
        return Err("SCREEN_CAPTURE_TRACKING_NEXT_FRAME_NOT_NEWER".into());
    }
    validate_tracking_frames_with_max_gap(seed, next, MAX_GROUNDING_SEED_AGE_SECONDS)
}

fn validate_grounding_seed_freshness(seed: &CapturedFrame, now: f64) -> Result<(), String> {
    if !now.is_finite()
        || !seed.observed_at.is_finite()
        || seed.observed_at <= 0.0
        || seed.observed_at > now + 1.0
        || now - seed.observed_at > MAX_GROUNDING_SEED_AGE_SECONDS
    {
        return Err("SCREEN_CAPTURE_GROUNDING_SEED_STALE".into());
    }
    Ok(())
}

fn clone_pinned_grounding_seed(
    seed: &Arc<Mutex<Option<CapturedFrame>>>,
) -> Result<CapturedFrame, String> {
    seed.lock()
        .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED".to_string())?
        .as_ref()
        .cloned()
        .ok_or_else(|| "SCREEN_CAPTURE_GROUNDING_SEED_NOT_PINNED".to_string())
}

fn display_fully_contains_window(
    display: CaptureDisplayGeometry,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> bool {
    if !display.is_valid()
        || !x.is_finite()
        || !y.is_finite()
        || !width.is_finite()
        || !height.is_finite()
        || width <= 0.0
        || height <= 0.0
    {
        return false;
    }
    let right = x + width;
    let bottom = y + height;
    right.is_finite()
        && bottom.is_finite()
        && x >= display.origin_x
        && y >= display.origin_y
        && right <= display.origin_x + display.width_points
        && bottom <= display.origin_y + display.height_points
}

fn capture_window_matches_target(
    is_on_screen: bool,
    application_pid: i32,
    application_bundle_id: &str,
    target: &CaptureTargetApplication,
) -> bool {
    is_on_screen
        && application_pid == target.application_pid
        && application_bundle_id == target.application_bundle_id
}

fn dominant_capture_window_index(sizes: &[(f64, f64)]) -> Result<usize, &'static str> {
    let mut ranked = sizes
        .iter()
        .enumerate()
        .filter_map(|(index, (width, height))| {
            if !width.is_finite() || !height.is_finite() || *width <= 0.0 || *height <= 0.0 {
                return None;
            }
            let area = width * height;
            area.is_finite().then_some((index, area))
        })
        .collect::<Vec<_>>();
    ranked.sort_by(|left, right| right.1.total_cmp(&left.1));

    let Some((largest_index, largest_area)) = ranked.first().copied() else {
        return Err("SCREEN_CAPTURE_TARGET_WINDOW_NOT_FOUND");
    };
    let Some((_, second_area)) = ranked.get(1).copied() else {
        return Ok(largest_index);
    };

    // ScreenCaptureKit can expose small title-bar controls as separate windows
    // owned by the same app. Select the content window only when its area is
    // clearly dominant; two plausible app windows remain an explicit ambiguity.
    if largest_area >= second_area * 8.0 {
        Ok(largest_index)
    } else {
        Err("SCREEN_CAPTURE_TARGET_WINDOW_AMBIGUOUS")
    }
}

fn capture_dimensions(
    geometry: CaptureDisplayGeometry,
    point_pixel_scale: f64,
) -> Result<(usize, usize), &'static str> {
    if !geometry.is_valid() || !point_pixel_scale.is_finite() || point_pixel_scale <= 0.0 {
        return Err("SCREEN_CAPTURE_FILTER_SCALE_INVALID");
    }
    let source_width = (geometry.width_points * point_pixel_scale).round().max(1.0);
    let source_height = (geometry.height_points * point_pixel_scale)
        .round()
        .max(1.0);
    if !source_width.is_finite() || !source_height.is_finite() {
        return Err("SCREEN_CAPTURE_FILTER_SCALE_INVALID");
    }
    let scale = (MAX_WIDTH as f64 / source_width)
        .min(MAX_HEIGHT as f64 / source_height)
        .min(1.0);
    Ok((
        ((source_width * scale).round() as usize).max(1),
        ((source_height * scale).round() as usize).max(1),
    ))
}

pub struct TrackingFramePair {
    pub capture_session_id: String,
    pub target_application: Option<CaptureTargetApplication>,
    pub first_sequence: u64,
    pub first_observed_at: f64,
    pub next_sequence: u64,
    pub next_observed_at: f64,
    pub width: usize,
    pub height: usize,
    pub display_geometry: CaptureDisplayGeometry,
    pub first_jpeg: Vec<u8>,
    pub next_jpeg: Vec<u8>,
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
struct ActiveGroundedTrack {
    tracker: crate::visual_tracking::OpenCvFlowTracker,
    processing_samples_ms: VecDeque<f64>,
    capture_session_id: String,
    target_application: CaptureTargetApplication,
    last_sequence: u64,
    last_observed_at: f64,
    width: usize,
    height: usize,
    display_geometry: CaptureDisplayGeometry,
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ContinuousTrackNotice {
    pub capture_session_id: String,
    pub target_application: CaptureTargetApplication,
    pub sequence: u64,
    pub observed_at: f64,
    pub width: usize,
    pub height: usize,
    pub display_geometry: CaptureDisplayGeometry,
    pub active: bool,
    pub reason: Option<String>,
    pub tracking: Option<crate::visual_tracking::VisualTrackObservation>,
    pub tracker_processing_p50_ms: Option<f64>,
    pub tracker_processing_p95_ms: Option<f64>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CaptureFrameNotice {
    pub capture_session_id: String,
    pub target_application: Option<CaptureTargetApplication>,
    pub sequence: u64,
    pub frames_received: u64,
    pub frames_processed: u64,
    pub frames_processing_skipped: u64,
    pub frame_status_counts: ScreenCaptureFrameStatusCounts,
    pub observed_at: f64,
    pub width: usize,
    pub height: usize,
    pub display_geometry: CaptureDisplayGeometry,
    pub initial_baseline: bool,
    pub changed: bool,
    pub changed_ratio: f64,
    pub scene_change: bool,
    pub processing_p50_ms: f64,
    pub processing_p95_ms: f64,
    /// Cumulative OpenCV change detection and grounded-tracking time.
    pub opencv_processing_ms: f64,
    pub capture_fps_ceiling: u32,
    pub configured_capture_fps: u32,
    pub observed_capture_fps: Option<f64>,
    pub changed_region: Option<ChangedRegion>,
    pub changed_regions: Vec<ChangedRegion>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenCaptureFrameStatusCounts {
    pub complete: u64,
    pub idle: u64,
    pub blank: u64,
    pub suspended: u64,
    pub started: u64,
    pub stopped: u64,
    pub unknown: u64,
    pub unavailable: u64,
}

#[cfg(target_os = "macos")]
struct AtomicFrameStatusCounts([AtomicU64; 8]);

#[cfg(target_os = "macos")]
impl Default for AtomicFrameStatusCounts {
    fn default() -> Self {
        Self(std::array::from_fn(|_| AtomicU64::new(0)))
    }
}

#[cfg(target_os = "macos")]
impl AtomicFrameStatusCounts {
    fn record(&self, status: Option<screencapturekit::cm::SCFrameStatus>) {
        use screencapturekit::cm::SCFrameStatus;
        let index = match status {
            Some(SCFrameStatus::Complete) => 0,
            Some(SCFrameStatus::Idle) => 1,
            Some(SCFrameStatus::Blank) => 2,
            Some(SCFrameStatus::Suspended) => 3,
            Some(SCFrameStatus::Started) => 4,
            Some(SCFrameStatus::Stopped) => 5,
            Some(SCFrameStatus::Unknown(_)) => 6,
            None => 7,
        };
        self.0[index].fetch_add(1, Ordering::Relaxed);
    }

    fn snapshot(&self) -> ScreenCaptureFrameStatusCounts {
        let load = |index: usize| self.0[index].load(Ordering::Relaxed);
        ScreenCaptureFrameStatusCounts {
            complete: load(0),
            idle: load(1),
            blank: load(2),
            suspended: load(3),
            started: load(4),
            stopped: load(5),
            unknown: load(6),
            unavailable: load(7),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChangedRegion {
    pub x: usize,
    pub y: usize,
    pub width: usize,
    pub height: usize,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenCaptureStatus {
    pub running: bool,
    pub tracking_backend: &'static str,
    pub capture_session_id: Option<String>,
    pub target_application: Option<CaptureTargetApplication>,
    pub frames_received: u64,
    pub frames_processed: u64,
    pub frames_processing_skipped: u64,
    pub frame_status_counts: ScreenCaptureFrameStatusCounts,
    pub last_frame_at: Option<f64>,
    pub processing_p50_ms: Option<f64>,
    pub processing_p95_ms: Option<f64>,
    /// Cumulative OpenCV change detection and grounded-tracking time.
    pub opencv_processing_ms: f64,
    pub capture_fps_ceiling: u32,
    pub configured_capture_fps: Option<u32>,
    pub observed_capture_fps: Option<f64>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LatestScreenFrame {
    pub capture_session_id: String,
    pub target_application: Option<CaptureTargetApplication>,
    pub sequence: u64,
    pub observed_at: f64,
    pub width: usize,
    pub height: usize,
    pub display_geometry: CaptureDisplayGeometry,
    pub mime_type: &'static str,
    pub data_url: String,
}

#[cfg(target_os = "macos")]
struct ActiveCapture {
    capture_session_id: String,
    target_application: Option<CaptureTargetApplication>,
    stream: screencapturekit::stream::sc_stream::SCStream,
    width: usize,
    height: usize,
    configured_capture_fps: Arc<AtomicU64>,
    compute_level_target: Arc<AtomicU64>,
    applied_policy_revision: AtomicU64,
    latest: Arc<Mutex<Option<CapturedFrame>>>,
    grounding_seed: Arc<Mutex<Option<CapturedFrame>>>,
    frames_received: Arc<AtomicU64>,
    frames_processed: Arc<AtomicU64>,
    frames_processing_skipped: Arc<AtomicU64>,
    frame_status_counts: Arc<AtomicFrameStatusCounts>,
    is_running: Arc<std::sync::atomic::AtomicBool>,
    previous_gray: Arc<Mutex<Option<image::GrayImage>>>,
    processing_samples_ms: Arc<Mutex<VecDeque<f64>>>,
    capture_rate_samples: Arc<Mutex<VecDeque<Instant>>>,
    #[cfg(feature = "opencv-perception")]
    opencv_processing_us_total: Arc<AtomicU64>,
    #[cfg(feature = "opencv-perception")]
    continuous_track: Arc<Mutex<Option<ActiveGroundedTrack>>>,
}

#[cfg(target_os = "macos")]
struct FrameProcessingGuard<'a> {
    skipped: &'a AtomicU64,
    processed: bool,
}

#[cfg(target_os = "macos")]
impl FrameProcessingGuard<'_> {
    fn mark_processed(&mut self) {
        self.processed = true;
    }
}

#[cfg(target_os = "macos")]
impl Drop for FrameProcessingGuard<'_> {
    fn drop(&mut self) {
        if !self.processed {
            self.skipped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(target_os = "macos")]
#[derive(Default)]
pub struct ScreenCaptureManager {
    active: Mutex<Option<ActiveCapture>>,
    starting: Mutex<()>,
    stop_revision: AtomicU64,
}

#[cfg(not(target_os = "macos"))]
#[derive(Default)]
pub struct ScreenCaptureManager;

impl ScreenCaptureManager {
    pub fn start(
        &self,
        app: AppHandle,
        target_application: Option<CaptureTargetApplication>,
    ) -> Result<ScreenCaptureStatus, String> {
        #[cfg(target_os = "macos")]
        {
            use screencapturekit::cv::CVPixelBufferLockFlags;
            use screencapturekit::prelude::*;

            let _starting = self
                .starting
                .try_lock()
                .map_err(|_| "SCREEN_CAPTURE_START_ALREADY_PENDING")?;
            let start_revision = self.stop_revision.load(Ordering::Acquire);
            let mut active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            if active
                .as_ref()
                .is_some_and(|current| !current.is_running.load(Ordering::Acquire))
            {
                if let Some(stopped) = active.take() {
                    let _ = stopped.stream.stop_capture();
                }
            }
            if let Some(current) = active.as_ref() {
                if current.target_application != target_application {
                    return Err("SCREEN_CAPTURE_ALREADY_RUNNING_FOR_DIFFERENT_TARGET".into());
                }
                return Ok(status_for(current));
            }

            // Keep synchronous status reads free while macOS may show consent UI.
            drop(active);
            if !screen_capture_access_preflight() {
                return Err("SCREEN_CAPTURE_PERMISSION_REQUIRED: Grant Screen Recording in Settings, then retry.".into());
            }
            let mut content = SCShareableContent::get()
                .map_err(|error| format!("SCREEN_CAPTURE_CONTENT_QUERY_FAILED: {error:?}"))?;
            let target_window = if let Some(target) = target_application.as_ref() {
                if target.application_pid <= 0
                    || target.application_bundle_id.trim().is_empty()
                    || target.application_pid == std::process::id() as i32
                {
                    return Err("SCREEN_CAPTURE_TARGET_APPLICATION_INVALID".into());
                }
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    let matches = content
                        .windows()
                        .into_iter()
                        .filter(|window| {
                            window.owning_application().is_some_and(|owner| {
                                capture_window_matches_target(
                                    window.is_on_screen(),
                                    owner.process_id(),
                                    &owner.bundle_identifier(),
                                    target,
                                )
                            })
                        })
                        .collect::<Vec<_>>();
                    match matches.len() {
                        0 if Instant::now() < deadline => {
                            std::thread::sleep(Duration::from_millis(100));
                            content = SCShareableContent::get().map_err(|error| {
                                format!("SCREEN_CAPTURE_CONTENT_QUERY_FAILED: {error:?}")
                            })?;
                        }
                        0 => return Err("SCREEN_CAPTURE_TARGET_WINDOW_NOT_FOUND".into()),
                        1 => break matches.into_iter().next(),
                        _ => {
                            let sizes = matches
                                .iter()
                                .map(|window| {
                                    let frame = window.frame();
                                    (frame.size.width, frame.size.height)
                                })
                                .collect::<Vec<_>>();
                            let selected = dominant_capture_window_index(&sizes)?;
                            break matches.into_iter().nth(selected);
                        }
                    }
                }
            } else {
                None
            };
            let displays = content.displays();
            let display = if let Some(window) = target_window.as_ref() {
                let frame = window.frame();
                let matches = displays
                    .iter()
                    .filter(|display| {
                        let bounds = display.frame();
                        display_fully_contains_window(
                            CaptureDisplayGeometry {
                                display_id: display.display_id(),
                                origin_x: bounds.origin.x,
                                origin_y: bounds.origin.y,
                                width_points: bounds.size.width,
                                height_points: bounds.size.height,
                            },
                            frame.origin.x,
                            frame.origin.y,
                            frame.size.width,
                            frame.size.height,
                        )
                    })
                    .collect::<Vec<_>>();
                match matches.as_slice() {
                    [display] => *display,
                    [] => return Err("SCREEN_CAPTURE_TARGET_WINDOW_DISPLAY_UNAVAILABLE".into()),
                    _ => return Err("SCREEN_CAPTURE_TARGET_WINDOW_DISPLAY_AMBIGUOUS".into()),
                }
            } else {
                displays.first().ok_or("SCREEN_CAPTURE_NO_DISPLAY")?
            };
            let bounds = target_window
                .as_ref()
                .map(|window| window.frame())
                .unwrap_or_else(|| display.frame());
            let display_geometry = CaptureDisplayGeometry {
                display_id: display.display_id(),
                origin_x: bounds.origin.x,
                origin_y: bounds.origin.y,
                width_points: bounds.size.width,
                height_points: bounds.size.height,
            };
            if !display_geometry.is_valid() {
                return Err("SCREEN_CAPTURE_DISPLAY_GEOMETRY_INVALID".into());
            }
            // Capture the exact window's own pixels, preserving the anti-mirror fix.
            let filter = if let Some(window) = target_window.as_ref() {
                SCContentFilter::create().with_window(window).build()
            } else {
                SCContentFilter::create()
                    .with_display(display)
                    .with_excluding_windows(&[])
                    .build()
            }
            .map_err(|error| format!("SCREEN_CAPTURE_FILTER_FAILED: {error:?}"))?;
            eprintln!(
                "SCREEN_CAPTURE_DIRECT_SELECTED target_pid={} target_bundle_id={} window_id={}",
                target_application
                    .as_ref()
                    .map(|target| target.application_pid)
                    .unwrap_or(0),
                target_application
                    .as_ref()
                    .map(|target| target.application_bundle_id.as_str())
                    .unwrap_or("none"),
                target_window
                    .as_ref()
                    .map(|window| window.window_id())
                    .unwrap_or(0)
            );
            let point_pixel_scale = filter.point_pixel_scale() as f64;
            if !point_pixel_scale.is_finite() || point_pixel_scale <= 0.0 {
                return Err("SCREEN_CAPTURE_FILTER_SCALE_INVALID".into());
            }
            let capture_session_id = new_capture_session_id();
            let (width, height) = capture_dimensions(display_geometry, point_pixel_scale)?;

            let configuration = SCStreamConfiguration::new()
                .with_width(width as u32)
                .with_height(height as u32)
                .with_pixel_format(PixelFormat::BGRA)
                .with_queue_depth(3)
                .with_fps(TARGET_FPS);
            let latest = Arc::new(Mutex::new(None::<CapturedFrame>));
            let grounding_seed = Arc::new(Mutex::new(None::<CapturedFrame>));
            let frames_received = Arc::new(AtomicU64::new(0));
            let frames_processed = Arc::new(AtomicU64::new(0));
            let frames_processing_skipped = Arc::new(AtomicU64::new(0));
            let frame_status_counts = Arc::new(AtomicFrameStatusCounts::default());
            let configured_capture_fps = Arc::new(AtomicU64::new(TARGET_FPS as u64));
            let compute_level_target = Arc::new(AtomicU64::new(0));
            let is_running = Arc::new(std::sync::atomic::AtomicBool::new(true));
            let callback_latest = Arc::clone(&latest);
            let callback_count = Arc::clone(&frames_received);
            let callback_processed_count = Arc::clone(&frames_processed);
            let callback_skipped_count = Arc::clone(&frames_processing_skipped);
            let callback_frame_status_counts = Arc::clone(&frame_status_counts);
            let callback_configured_capture_fps = Arc::clone(&configured_capture_fps);
            let callback_compute_level_target = Arc::clone(&compute_level_target);
            let callback_running = Arc::clone(&is_running);
            #[cfg(feature = "opencv-perception")]
            let opencv_processing_us_total = Arc::new(AtomicU64::new(0));
            #[cfg(feature = "opencv-perception")]
            let callback_opencv_processing_us_total = Arc::clone(&opencv_processing_us_total);
            let previous_gray = Arc::new(Mutex::new(None::<image::GrayImage>));
            let callback_previous_gray = Arc::clone(&previous_gray);
            #[cfg(feature = "opencv-perception")]
            let continuous_track = Arc::new(Mutex::new(None::<ActiveGroundedTrack>));
            #[cfg(feature = "opencv-perception")]
            let callback_continuous_track = Arc::clone(&continuous_track);
            let processing_samples_ms = Arc::new(Mutex::new(VecDeque::with_capacity(
                PROCESSING_SAMPLE_CAPACITY,
            )));
            let callback_processing_samples = Arc::clone(&processing_samples_ms);
            let capture_rate_samples = Arc::new(Mutex::new(VecDeque::with_capacity(
                CAPTURE_RATE_SAMPLE_CAPACITY,
            )));
            let callback_capture_rate_samples = Arc::clone(&capture_rate_samples);
            let callback_app = app.clone();
            let callback_display_geometry = display_geometry;
            let callback_target_window = target_window.clone();
            let callback_capture_session_id = capture_session_id.clone();
            let callback_target_application = target_application.clone();

            let stopped = Arc::clone(&is_running);
            let delegate = screencapturekit::stream::delegate_trait::StreamCallbacks::new()
                .on_stop(move |_| {
                    stopped.store(false, Ordering::Release);
                });
            let mut stream = SCStream::new_with_delegate(&filter, &configuration, delegate)
                .map_err(|error| format!("SCREEN_CAPTURE_STREAM_CREATE_FAILED: {error:?}"))?;
            stream
                .add_output_handler(
                    move |sample: CMSampleBuffer, output_type: SCStreamOutputType| {
                        let processing_started = Instant::now();
                        if !callback_running.load(Ordering::Acquire)
                            || output_type != SCStreamOutputType::Screen
                        {
                            return;
                        }
                        callback_count.fetch_add(1, Ordering::Relaxed);
                        callback_frame_status_counts.record(sample.frame_status());
                        let mut frame_outcome = FrameProcessingGuard {
                            skipped: callback_skipped_count.as_ref(),
                            processed: false,
                        };
                        let Some(pixel_buffer) = sample.pixel_buffer() else {
                            return;
                        };
                        let frame_width = pixel_buffer.width();
                        let frame_height = pixel_buffer.height();
                        if frame_width == 0
                            || frame_height == 0
                            || frame_width > MAX_WIDTH
                            || frame_height > MAX_HEIGHT
                        {
                            return;
                        }
                        let stride = pixel_buffer.bytes_per_row();
                        let Ok(guard) = pixel_buffer.lock(CVPixelBufferLockFlags::READ_ONLY) else {
                            return;
                        };
                        let Some(source) = (unsafe { guard.as_slice() }) else {
                            return;
                        };
                        let Some(rgb) = bgra_to_rgb(source, frame_width, frame_height, stride)
                        else {
                            return;
                        };
                        drop(guard);
                        let Ok(mut previous_gray) = callback_previous_gray.lock() else {
                            return;
                        };
                        let compute_level_target =
                            callback_compute_level_target.load(Ordering::Acquire).min(4) as u8;
                        let initial_baseline = previous_gray.is_none();
                        #[cfg(feature = "opencv-perception")]
                        let opencv_processing_started = Instant::now();
                        let change_estimate = estimate_change(
                            &rgb,
                            frame_width as u32,
                            frame_height as u32,
                            previous_gray.as_ref(),
                            compute_level_target,
                        );
                        #[cfg(feature = "opencv-perception")]
                        let opencv_processing_us = opencv_processing_started
                            .elapsed()
                            .as_micros()
                            .min(u64::MAX as u128)
                            as u64;
                        #[cfg(feature = "opencv-perception")]
                        let opencv_processing_total_us = callback_opencv_processing_us_total
                            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |total| {
                                Some(total.saturating_add(opencv_processing_us))
                            })
                            .unwrap_or_else(|total| total)
                            .saturating_add(opencv_processing_us);
                        #[cfg(not(feature = "opencv-perception"))]
                        let opencv_processing_total_us = 0_u64;
                        let Some((
                            changed,
                            changed_ratio,
                            scene_change,
                            changed_region,
                            changed_regions,
                            gray,
                        )) = change_estimate
                        else {
                            return;
                        };
                        frame_outcome.mark_processed();
                        let sequence = callback_processed_count.fetch_add(1, Ordering::Relaxed) + 1;
                        if should_advance_change_baseline(
                            compute_level_target,
                            changed,
                            previous_gray.is_some(),
                        ) {
                            *previous_gray = Some(gray);
                        }
                        drop(previous_gray);
                        let processing_ms = processing_started.elapsed().as_secs_f64() * 1_000.0;
                        let (processing_p50_ms, processing_p95_ms) =
                            if let Ok(mut samples) = callback_processing_samples.lock() {
                                if samples.len() == PROCESSING_SAMPLE_CAPACITY {
                                    samples.pop_front();
                                }
                                samples.push_back(processing_ms);
                                processing_percentiles_ms(&samples)
                            } else {
                                (processing_ms, processing_ms)
                            };
                        let observed_at = now_seconds();
                        let observed_capture_fps =
                            if let Ok(mut samples) = callback_capture_rate_samples.lock() {
                                if samples.len() == CAPTURE_RATE_SAMPLE_CAPACITY {
                                    samples.pop_front();
                                }
                                let captured_at = Instant::now();
                                samples.push_back(captured_at);
                                while samples.front().is_some_and(|sample| {
                                    captured_at.duration_since(*sample) > CAPTURE_RATE_SAMPLE_WINDOW
                                }) {
                                    samples.pop_front();
                                }
                                observed_capture_fps(&samples)
                            } else {
                                None
                            };
                        let mut frame = CapturedFrame {
                            capture_session_id: callback_capture_session_id.clone(),
                            target_application: callback_target_application.clone(),
                            sequence: 0,
                            observed_at,
                            width: frame_width,
                            height: frame_height,
                            display_geometry: callback_target_window
                                .as_ref()
                                .map(|window| {
                                    let bounds = window.frame();
                                    CaptureDisplayGeometry {
                                        display_id: callback_display_geometry.display_id,
                                        origin_x: bounds.origin.x,
                                        origin_y: bounds.origin.y,
                                        width_points: bounds.size.width,
                                        height_points: bounds.size.height,
                                    }
                                })
                                .filter(|geometry| geometry.is_valid())
                                .unwrap_or(callback_display_geometry),
                            rgb: Arc::new(rgb),
                        };
                        let sequence = if let Ok(mut slot) = callback_latest.lock() {
                            if !callback_running.load(Ordering::Acquire) {
                                return;
                            }
                            frame.sequence = sequence;
                            *slot = Some(frame.clone());
                            let notice = CaptureFrameNotice {
                                capture_session_id: callback_capture_session_id.clone(),
                                target_application: callback_target_application.clone(),
                                sequence,
                                frames_received: callback_count.load(Ordering::Relaxed),
                                frames_processed: callback_processed_count.load(Ordering::Relaxed),
                                frames_processing_skipped: callback_skipped_count
                                    .load(Ordering::Relaxed),
                                frame_status_counts: callback_frame_status_counts.snapshot(),
                                observed_at,
                                width: frame_width,
                                height: frame_height,
                                display_geometry: frame.display_geometry,
                                initial_baseline,
                                changed,
                                changed_ratio,
                                scene_change,
                                processing_p50_ms,
                                processing_p95_ms,
                                opencv_processing_ms: opencv_processing_total_us as f64 / 1_000.0,
                                capture_fps_ceiling: capture_fps_ceiling(),
                                configured_capture_fps: callback_configured_capture_fps
                                    .load(Ordering::Relaxed)
                                    as u32,
                                observed_capture_fps,
                                changed_region,
                                changed_regions,
                            };
                            let _ = callback_app.emit("screen-capture-frame", notice);
                            Some(sequence)
                        } else {
                            None
                        };
                        #[cfg(feature = "opencv-perception")]
                        if let Some(sequence) = sequence {
                            update_continuous_track(
                                &callback_app,
                                &callback_continuous_track,
                                &frame,
                                sequence,
                                compute_level_target,
                                &callback_opencv_processing_us_total,
                            );
                        }
                        #[cfg(not(feature = "opencv-perception"))]
                        let _ = sequence;
                    },
                    SCStreamOutputType::Screen,
                )
                .map_err(|error| format!("SCREEN_CAPTURE_OUTPUT_FAILED: {error:?}"))?;
            let mut active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            if self.stop_revision.load(Ordering::Acquire) != start_revision {
                return Err("SCREEN_CAPTURE_START_STOPPED".into());
            }
            if let Err(error) = stream.start_capture() {
                is_running.store(false, Ordering::Release);
                return Err(format!("SCREEN_CAPTURE_START_FAILED: {error:?}"));
            }

            let capture = ActiveCapture {
                capture_session_id,
                target_application,
                stream,
                width,
                height,
                configured_capture_fps,
                compute_level_target,
                applied_policy_revision: AtomicU64::new(0),
                latest,
                grounding_seed,
                frames_received,
                frames_processed,
                frames_processing_skipped,
                frame_status_counts,
                is_running,
                previous_gray,
                processing_samples_ms,
                capture_rate_samples,
                #[cfg(feature = "opencv-perception")]
                opencv_processing_us_total,
                #[cfg(feature = "opencv-perception")]
                continuous_track,
            };
            let status = status_for(&capture);
            *active = Some(capture);
            Ok(status)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (app, target_application);
            Err("SCREEN_CAPTURE_UNSUPPORTED_PLATFORM".into())
        }
    }

    pub fn apply_capture_policy(
        &self,
        target_fps: u32,
        compute_level_target: u8,
        policy_revision: u64,
    ) -> Result<(), String> {
        #[cfg(target_os = "macos")]
        {
            use screencapturekit::prelude::*;

            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            let Some(capture) = active.as_ref() else {
                return Ok(());
            };
            apply_capture_rate_policy(
                target_fps,
                compute_level_target,
                policy_revision,
                &capture.configured_capture_fps,
                &capture.compute_level_target,
                &capture.applied_policy_revision,
                capture_fps_ceiling(),
                supports_runtime_capture_rate_updates(),
                |desired_fps| {
                    let configuration = SCStreamConfiguration::new()
                        .with_width(capture.width as u32)
                        .with_height(capture.height as u32)
                        .with_pixel_format(PixelFormat::BGRA)
                        .with_queue_depth(3)
                        .with_fps(desired_fps);
                    capture
                        .stream
                        .update_configuration(&configuration)
                        .map_err(|error| format!("SCREEN_CAPTURE_RATE_UPDATE_FAILED: {error:?}"))
                },
            )
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (target_fps, compute_level_target, policy_revision);
            Ok(())
        }
    }

    pub fn stop(&self) -> Result<ScreenCaptureStatus, String> {
        #[cfg(target_os = "macos")]
        {
            self.stop_revision.fetch_add(1, Ordering::AcqRel);
            let capture = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?
                .take();
            if let Some(capture) = capture {
                capture.is_running.store(false, Ordering::Release);
                let _ = capture.stream.stop_capture();
                if let Ok(mut latest) = capture.latest.lock() {
                    *latest = None;
                }
                if let Ok(mut seed) = capture.grounding_seed.lock() {
                    *seed = None;
                }
                if let Ok(mut previous) = capture.previous_gray.lock() {
                    *previous = None;
                }
                #[cfg(feature = "opencv-perception")]
                if let Ok(mut tracking) = capture.continuous_track.lock() {
                    *tracking = None;
                }
            }
            Ok(ScreenCaptureStatus {
                running: false,
                tracking_backend: crate::visual_tracking::tracking_backend_name(),
                capture_session_id: None,
                target_application: None,
                frames_received: 0,
                frames_processed: 0,
                frames_processing_skipped: 0,
                frame_status_counts: ScreenCaptureFrameStatusCounts::default(),
                last_frame_at: None,
                processing_p50_ms: None,
                processing_p95_ms: None,
                opencv_processing_ms: 0.0,
                capture_fps_ceiling: capture_fps_ceiling(),
                configured_capture_fps: None,
                observed_capture_fps: None,
            })
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(ScreenCaptureStatus {
                running: false,
                tracking_backend: crate::visual_tracking::tracking_backend_name(),
                capture_session_id: None,
                frames_received: 0,
                frames_processed: 0,
                frames_processing_skipped: 0,
                frame_status_counts: ScreenCaptureFrameStatusCounts::default(),
                last_frame_at: None,
                processing_p50_ms: None,
                processing_p95_ms: None,
                opencv_processing_ms: 0.0,
                capture_fps_ceiling: capture_fps_ceiling(),
                configured_capture_fps: None,
                observed_capture_fps: None,
            })
        }
    }

    pub fn status(&self) -> Result<ScreenCaptureStatus, String> {
        #[cfg(target_os = "macos")]
        {
            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            Ok(active
                .as_ref()
                .map(status_for)
                .unwrap_or(ScreenCaptureStatus {
                    running: false,
                    tracking_backend: crate::visual_tracking::tracking_backend_name(),
                    capture_session_id: None,
                    target_application: None,
                    frames_received: 0,
                    frames_processed: 0,
                    frames_processing_skipped: 0,
                    frame_status_counts: ScreenCaptureFrameStatusCounts::default(),
                    last_frame_at: None,
                    processing_p50_ms: None,
                    processing_p95_ms: None,
                    opencv_processing_ms: 0.0,
                    capture_fps_ceiling: capture_fps_ceiling(),
                    configured_capture_fps: None,
                    observed_capture_fps: None,
                }))
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(ScreenCaptureStatus {
                running: false,
                tracking_backend: crate::visual_tracking::tracking_backend_name(),
                capture_session_id: None,
                target_application: None,
                frames_received: 0,
                frames_processed: 0,
                frames_processing_skipped: 0,
                frame_status_counts: ScreenCaptureFrameStatusCounts::default(),
                last_frame_at: None,
                processing_p50_ms: None,
                processing_p95_ms: None,
                opencv_processing_ms: 0.0,
                capture_fps_ceiling: capture_fps_ceiling(),
                configured_capture_fps: None,
                observed_capture_fps: None,
            })
        }
    }

    pub fn latest_frame(&self) -> Result<Option<LatestScreenFrame>, String> {
        #[cfg(target_os = "macos")]
        {
            use image::codecs::jpeg::JpegEncoder;
            use image::{ExtendedColorType, ImageEncoder};
            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            let Some(capture) = active.as_ref() else {
                return Ok(None);
            };
            let frame = capture
                .latest
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED")?
                .clone();
            let Some(frame) = frame else {
                return Ok(None);
            };
            let mut jpeg = Vec::new();
            JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY)
                .write_image(
                    frame.rgb.as_slice(),
                    frame.width as u32,
                    frame.height as u32,
                    ExtendedColorType::Rgb8,
                )
                .map_err(|error| format!("SCREEN_CAPTURE_ENCODE_FAILED: {error}"))?;
            Ok(Some(LatestScreenFrame {
                capture_session_id: frame.capture_session_id,
                target_application: frame.target_application,
                sequence: frame.sequence,
                observed_at: frame.observed_at,
                width: frame.width,
                height: frame.height,
                display_geometry: frame.display_geometry,
                mime_type: "image/jpeg",
                data_url: format!("data:image/jpeg;base64,{}", base64_encode(&jpeg)),
            }))
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(None)
        }
    }

    /// Return a chat-safe frame from the exact application window being
    /// controlled. Targeted sessions use a window-only ScreenCaptureKit filter,
    /// so this frame cannot contain the OLCR chat preview or covering windows.
    pub fn latest_target_window_preview(&self) -> Result<Option<LatestScreenFrame>, String> {
        #[cfg(target_os = "macos")]
        {
            let frame = self.latest_frame()?;
            if frame
                .as_ref()
                .is_some_and(|frame| frame.target_application.is_none())
            {
                return Err("SCREEN_CAPTURE_TARGET_APPLICATION_REQUIRED".into());
            }
            Ok(frame)
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(None)
        }
    }

    /// Pin the exact latest frame that will be sent for GUI grounding. The
    /// bounded single-frame pin survives subsequent capture callbacks until a
    /// matching tracking request is validated or capture stops. Reusing this
    /// bounded seed allows tracking and pointer preflight to verify the same
    /// exact frame; each use still enforces the seed and pair freshness limits.
    pub fn pin_latest_grounding_frame(&self) -> Result<Option<LatestScreenFrame>, String> {
        #[cfg(target_os = "macos")]
        {
            use image::codecs::jpeg::JpegEncoder;
            use image::{ExtendedColorType, ImageEncoder};

            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            let Some(capture) = active.as_ref() else {
                return Ok(None);
            };
            let frame = capture
                .latest
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED")?
                .clone();
            let Some(frame) = frame else {
                return Ok(None);
            };
            if frame.target_application.is_none() {
                return Err("SCREEN_CAPTURE_GROUNDING_TARGET_REQUIRED".into());
            }
            validate_grounding_seed_freshness(&frame, now_seconds())?;
            #[cfg(feature = "opencv-perception")]
            if let Ok(mut tracking) = capture.continuous_track.lock() {
                *tracking = None;
            }
            let mut jpeg = Vec::new();
            JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY)
                .write_image(
                    frame.rgb.as_slice(),
                    frame.width as u32,
                    frame.height as u32,
                    ExtendedColorType::Rgb8,
                )
                .map_err(|error| format!("SCREEN_CAPTURE_ENCODE_FAILED: {error}"))?;
            *capture
                .grounding_seed
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED")? = Some(frame.clone());
            Ok(Some(LatestScreenFrame {
                capture_session_id: frame.capture_session_id,
                target_application: frame.target_application,
                sequence: frame.sequence,
                observed_at: frame.observed_at,
                width: frame.width,
                height: frame.height,
                display_geometry: frame.display_geometry,
                mime_type: "image/jpeg",
                data_url: format!("data:image/jpeg;base64,{}", base64_encode(&jpeg)),
            }))
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(None)
        }
    }

    /// Build a tracking pair whose first frame is the exact frame pinned for
    /// model grounding. A delayed or mismatched request fails closed.
    pub fn tracking_pair_for_grounding_seed(
        &self,
        seed_sequence: u64,
    ) -> Result<Option<TrackingFramePair>, String> {
        #[cfg(target_os = "macos")]
        {
            use image::codecs::jpeg::JpegEncoder;
            use image::{ExtendedColorType, ImageEncoder};

            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            let Some(capture) = active.as_ref() else {
                return Ok(None);
            };
            let seed = clone_pinned_grounding_seed(&capture.grounding_seed)?;
            if seed.sequence != seed_sequence {
                return Err("SCREEN_CAPTURE_TRACKING_SEED_SEQUENCE_MISMATCH".into());
            }
            let next = capture
                .latest
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED")?
                .clone()
                .ok_or("SCREEN_CAPTURE_TRACKING_FRAMES_NOT_READY")?;
            validate_grounding_seed_pair(&seed, &next, seed_sequence)?;

            let encode = |frame: &CapturedFrame| -> Result<Vec<u8>, String> {
                let mut jpeg = Vec::new();
                JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY)
                    .write_image(
                        frame.rgb.as_slice(),
                        frame.width as u32,
                        frame.height as u32,
                        ExtendedColorType::Rgb8,
                    )
                    .map_err(|error| format!("SCREEN_CAPTURE_TRACKING_ENCODE_FAILED: {error}"))?;
                Ok(jpeg)
            };
            Ok(Some(TrackingFramePair {
                capture_session_id: next.capture_session_id.clone(),
                target_application: next.target_application.clone(),
                first_sequence: seed.sequence,
                first_observed_at: seed.observed_at,
                next_sequence: next.sequence,
                next_observed_at: next.observed_at,
                width: next.width,
                height: next.height,
                display_geometry: next.display_geometry,
                first_jpeg: encode(&seed)?,
                next_jpeg: encode(&next)?,
            }))
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = seed_sequence;
            Ok(None)
        }
    }

    /// Arm in-memory OpenCV tracking from the exact frame returned by the
    /// grounded frame-pair command. Pixel frames stay native; only compact
    /// bbox/confidence notices are emitted while tracking remains healthy.
    pub fn arm_grounded_opencv_tracking(
        &self,
        sequence: u64,
        bbox_normalized: [f32; 4],
    ) -> Result<bool, String> {
        #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
        {
            let active = self
                .active
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_LOCK_POISONED")?;
            let Some(capture) = active.as_ref() else {
                return Ok(false);
            };
            let frame = capture
                .latest
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_FRAME_LOCK_POISONED")?
                .clone()
                .ok_or("SCREEN_CAPTURE_TRACKING_FRAMES_NOT_READY")?;
            if frame.sequence != sequence {
                return Ok(false);
            }
            validate_grounding_seed_freshness(&frame, now_seconds())?;
            let Some(target_application) = frame.target_application.clone() else {
                return Err("SCREEN_CAPTURE_GROUNDING_TARGET_REQUIRED".into());
            };
            let tracker = crate::visual_tracking::OpenCvFlowTracker::new(
                frame.rgb.as_slice(),
                frame.width as u32,
                frame.height as u32,
                bbox_normalized,
            )?;
            *capture
                .continuous_track
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_TRACKING_LOCK_POISONED")? =
                Some(ActiveGroundedTrack {
                    tracker,
                    processing_samples_ms: VecDeque::with_capacity(PROCESSING_SAMPLE_CAPACITY),
                    capture_session_id: frame.capture_session_id,
                    target_application,
                    last_sequence: frame.sequence,
                    last_observed_at: frame.observed_at,
                    width: frame.width,
                    height: frame.height,
                    display_geometry: frame.display_geometry,
                });
            Ok(true)
        }
        #[cfg(not(all(target_os = "macos", feature = "opencv-perception")))]
        {
            let _ = (sequence, bbox_normalized);
            Ok(false)
        }
    }
}

impl Drop for ScreenCaptureManager {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn update_continuous_track(
    app: &AppHandle,
    state: &Arc<Mutex<Option<ActiveGroundedTrack>>>,
    frame: &CapturedFrame,
    sequence: u64,
    compute_level_target: u8,
    opencv_processing_us_total: &AtomicU64,
) {
    let Ok(mut active) = state.lock() else {
        return;
    };
    let Some(track) = active.as_mut() else {
        return;
    };
    if compute_level_target < 2 {
        let track = active.take().expect("active track was checked above");
        let (tracker_processing_p50_ms, tracker_processing_p95_ms) =
            optional_processing_percentiles_ms(&track.processing_samples_ms);
        let notice = ContinuousTrackNotice {
            capture_session_id: track.capture_session_id,
            target_application: track.target_application,
            sequence,
            observed_at: frame.observed_at,
            width: frame.width,
            height: frame.height,
            display_geometry: frame.display_geometry,
            active: false,
            reason: Some("APC_COMPUTE_LEVEL_BELOW_TRACKING".into()),
            tracking: None,
            tracker_processing_p50_ms,
            tracker_processing_p95_ms,
        };
        drop(active);
        let _ = app.emit("screen-capture-track", notice);
        return;
    }
    let invalid = if frame.capture_session_id != track.capture_session_id {
        Some("SCREEN_CAPTURE_TRACKING_SESSION_MISMATCH")
    } else if frame.target_application.as_ref() != Some(&track.target_application) {
        Some("SCREEN_CAPTURE_TRACKING_TARGET_MISMATCH")
    } else if frame.width != track.width
        || frame.height != track.height
        || frame.display_geometry != track.display_geometry
    {
        Some("SCREEN_CAPTURE_TRACKING_GEOMETRY_MISMATCH")
    } else if sequence <= track.last_sequence
        || !frame.observed_at.is_finite()
        || frame.observed_at <= track.last_observed_at
        || frame.observed_at - track.last_observed_at > MAX_TRACKING_FRAME_GAP_SECONDS
    {
        Some("SCREEN_CAPTURE_TRACKING_FRAME_GAP")
    } else {
        None
    };
    if let Some(reason) = invalid {
        let track = active.take().expect("active track was checked above");
        let (tracker_processing_p50_ms, tracker_processing_p95_ms) =
            optional_processing_percentiles_ms(&track.processing_samples_ms);
        let notice = ContinuousTrackNotice {
            capture_session_id: track.capture_session_id,
            target_application: track.target_application,
            sequence,
            observed_at: frame.observed_at,
            width: frame.width,
            height: frame.height,
            display_geometry: frame.display_geometry,
            active: false,
            reason: Some(reason.into()),
            tracking: None,
            tracker_processing_p50_ms,
            tracker_processing_p95_ms,
        };
        drop(active);
        let _ = app.emit("screen-capture-track", notice);
        return;
    }
    let previous_bbox = track.tracker.bbox_normalized();
    let tracker_processing_started = Instant::now();
    let result = track.tracker.update(
        frame.rgb.as_slice(),
        frame.width as u32,
        frame.height as u32,
    );
    let tracker_processing_us = tracker_processing_started
        .elapsed()
        .as_micros()
        .min(u64::MAX as u128) as u64;
    add_cumulative_processing_us(opencv_processing_us_total, tracker_processing_us);
    if let Ok(observation) = result.as_ref() {
        record_processing_sample_ms(&mut track.processing_samples_ms, observation.processing_ms);
    }
    let fail_reason = match result.as_ref() {
        Ok(observation) if observation.confidence < 0.5 => Some("OPENCV_TRACK_CONFIDENCE_TOO_LOW"),
        Ok(observation)
            if (observation.bbox_normalized[0] - previous_bbox[0]).abs() > 0.15
                || (observation.bbox_normalized[1] - previous_bbox[1]).abs() > 0.15 =>
        {
            Some("OPENCV_TRACK_DISPLACEMENT_TOO_LARGE")
        }
        Ok(_) => None,
        Err(_) => Some("OPENCV_TRACK_UPDATE_FAILED"),
    };
    if let Some(reason) = fail_reason {
        let track = active.take().expect("active track was checked above");
        let (tracker_processing_p50_ms, tracker_processing_p95_ms) =
            optional_processing_percentiles_ms(&track.processing_samples_ms);
        let notice = ContinuousTrackNotice {
            capture_session_id: track.capture_session_id,
            target_application: track.target_application,
            sequence,
            observed_at: frame.observed_at,
            width: frame.width,
            height: frame.height,
            display_geometry: frame.display_geometry,
            active: false,
            reason: Some(reason.into()),
            tracking: None,
            tracker_processing_p50_ms,
            tracker_processing_p95_ms,
        };
        drop(active);
        let _ = app.emit("screen-capture-track", notice);
        return;
    }
    let observation = result.expect("successful track result checked above");
    let (tracker_processing_p50_ms, tracker_processing_p95_ms) =
        optional_processing_percentiles_ms(&track.processing_samples_ms);
    track.last_sequence = sequence;
    track.last_observed_at = frame.observed_at;
    let notice = ContinuousTrackNotice {
        capture_session_id: track.capture_session_id.clone(),
        target_application: track.target_application.clone(),
        sequence,
        observed_at: frame.observed_at,
        width: frame.width,
        height: frame.height,
        display_geometry: frame.display_geometry,
        active: true,
        reason: None,
        tracking: Some(observation),
        tracker_processing_p50_ms,
        tracker_processing_p95_ms,
    };
    drop(active);
    let _ = app.emit("screen-capture-track", notice);
}

#[cfg(target_os = "macos")]
fn status_for(capture: &ActiveCapture) -> ScreenCaptureStatus {
    let last_frame_at = capture
        .latest
        .lock()
        .ok()
        .and_then(|latest| latest.as_ref().map(|frame| frame.observed_at));
    let (processing_p50_ms, processing_p95_ms) = capture
        .processing_samples_ms
        .lock()
        .ok()
        .and_then(|samples| (!samples.is_empty()).then(|| processing_percentiles_ms(&samples)))
        .map(|(p50, p95)| (Some(p50), Some(p95)))
        .unwrap_or((None, None));
    let observed_capture_fps = capture
        .capture_rate_samples
        .lock()
        .ok()
        .and_then(|samples| observed_capture_fps(&samples));
    ScreenCaptureStatus {
        running: capture.is_running.load(Ordering::Acquire),
        tracking_backend: crate::visual_tracking::tracking_backend_name(),
        capture_session_id: Some(capture.capture_session_id.clone()),
        target_application: capture.target_application.clone(),
        frames_received: capture.frames_received.load(Ordering::Relaxed),
        frames_processed: capture.frames_processed.load(Ordering::Relaxed),
        frames_processing_skipped: capture.frames_processing_skipped.load(Ordering::Relaxed),
        frame_status_counts: capture.frame_status_counts.snapshot(),
        last_frame_at,
        processing_p50_ms,
        processing_p95_ms,
        opencv_processing_ms: opencv_processing_ms(capture),
        capture_fps_ceiling: capture_fps_ceiling(),
        configured_capture_fps: Some(capture.configured_capture_fps.load(Ordering::Acquire) as u32),
        observed_capture_fps,
    }
}

#[cfg(target_os = "macos")]
fn opencv_processing_ms(_capture: &ActiveCapture) -> f64 {
    #[cfg(feature = "opencv-perception")]
    {
        return _capture.opencv_processing_us_total.load(Ordering::Relaxed) as f64 / 1_000.0;
    }
    #[cfg(not(feature = "opencv-perception"))]
    {
        0.0
    }
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn add_cumulative_processing_us(total: &AtomicU64, elapsed_us: u64) {
    let _ = total.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
        Some(current.saturating_add(elapsed_us))
    });
}

fn observed_capture_fps(samples: &VecDeque<Instant>) -> Option<f64> {
    let first = samples.front()?;
    let last = samples.back()?;
    let elapsed = last.duration_since(*first).as_secs_f64();
    if samples.len() < 2 || !elapsed.is_finite() || elapsed <= 0.0 {
        return None;
    }
    let fps = (samples.len() - 1) as f64 / elapsed;
    fps.is_finite().then_some(fps)
}

fn processing_percentiles_ms(samples: &VecDeque<f64>) -> (f64, f64) {
    if samples.is_empty() {
        return (0.0, 0.0);
    }
    let mut sorted: Vec<f64> = samples.iter().copied().collect();
    sorted.sort_by(f64::total_cmp);
    let nearest_rank = |percentile: f64| {
        let rank = (percentile * sorted.len() as f64).ceil() as usize;
        sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
    };
    (nearest_rank(0.50), nearest_rank(0.95))
}

fn record_processing_sample_ms(samples: &mut VecDeque<f64>, value: f64) {
    if !value.is_finite() || value < 0.0 {
        return;
    }
    if samples.len() == PROCESSING_SAMPLE_CAPACITY {
        samples.pop_front();
    }
    samples.push_back(value);
}

fn optional_processing_percentiles_ms(samples: &VecDeque<f64>) -> (Option<f64>, Option<f64>) {
    if samples.is_empty() {
        return (None, None);
    }
    let (p50, p95) = processing_percentiles_ms(samples);
    (Some(p50), Some(p95))
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn bgra_to_rgb(source: &[u8], width: usize, height: usize, stride: usize) -> Option<Vec<u8>> {
    let row_bytes = width.checked_mul(4)?;
    let source_len = height.checked_mul(stride)?;
    let output_len = width.checked_mul(height)?.checked_mul(3)?;
    if width == 0
        || height == 0
        || width > MAX_WIDTH
        || height > MAX_HEIGHT
        || stride < row_bytes
        || source.len() < source_len
    {
        return None;
    }
    let mut output = Vec::with_capacity(output_len);
    for y in 0..height {
        let row = &source[y * stride..y * stride + row_bytes];
        for pixel in row.chunks_exact(4) {
            output.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
        }
    }
    Some(output)
}

#[cfg(target_os = "macos")]
fn estimate_change(
    rgb: &[u8],
    width: u32,
    height: u32,
    previous: Option<&image::GrayImage>,
    compute_level_target: u8,
) -> Option<(
    bool,
    f64,
    bool,
    Option<ChangedRegion>,
    Vec<ChangedRegion>,
    image::GrayImage,
)> {
    #[cfg(feature = "opencv-perception")]
    {
        return opencv_perception::estimate_change(
            rgb,
            width,
            height,
            previous,
            compute_level_target,
        );
    }
    #[cfg(not(feature = "opencv-perception"))]
    {
        let _ = compute_level_target;
        estimate_change_image(rgb, width, height, previous)
    }
}

#[cfg(feature = "opencv-perception")]
fn should_advance_change_baseline(
    compute_level_target: u8,
    changed: bool,
    has_previous: bool,
) -> bool {
    !has_previous || !changed || compute_level_target > 0
}

#[cfg(not(feature = "opencv-perception"))]
fn should_advance_change_baseline(
    _compute_level_target: u8,
    _changed: bool,
    _has_previous: bool,
) -> bool {
    true
}

#[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
fn estimate_change_image(
    rgb: &[u8],
    width: u32,
    height: u32,
    previous: Option<&image::GrayImage>,
) -> Option<(
    bool,
    f64,
    bool,
    Option<ChangedRegion>,
    Vec<ChangedRegion>,
    image::GrayImage,
)> {
    use image::{imageops, DynamicImage, ImageBuffer, Rgb};

    if width == 0
        || height == 0
        || rgb.len()
            != (width as usize)
                .checked_mul(height as usize)?
                .checked_mul(3)?
    {
        return None;
    }
    let source: ImageBuffer<Rgb<u8>, Vec<u8>> = ImageBuffer::from_raw(width, height, rgb.to_vec())?;
    let gray = imageops::resize(
        &DynamicImage::ImageRgb8(source).to_luma8(),
        SENTINEL_WIDTH,
        SENTINEL_HEIGHT,
        imageops::FilterType::Triangle,
    );
    let Some(previous) = previous.filter(|previous| previous.dimensions() == gray.dimensions())
    else {
        return Some((
            true,
            1.0,
            false,
            Some(ChangedRegion {
                x: 0,
                y: 0,
                width: width as usize,
                height: height as usize,
            }),
            vec![ChangedRegion {
                x: 0,
                y: 0,
                width: width as usize,
                height: height as usize,
            }],
            gray,
        ));
    };
    let mut changed = 0u32;
    let mut mask = vec![false; (SENTINEL_WIDTH * SENTINEL_HEIGHT) as usize];
    for y in 0..SENTINEL_HEIGHT {
        for x in 0..SENTINEL_WIDTH {
            let delta = gray.get_pixel(x, y)[0].abs_diff(previous.get_pixel(x, y)[0]);
            if delta < PIXEL_DELTA_THRESHOLD {
                continue;
            }
            changed += 1;
            mask[(y * SENTINEL_WIDTH + x) as usize] = true;
        }
    }
    let ratio = changed as f64 / (SENTINEL_WIDTH * SENTINEL_HEIGHT) as f64;
    let scene_change = ratio >= SCENE_CHANGE_RATIO;
    let changed_regions = connected_regions(&mut mask, width as usize, height as usize);
    let region = changed_regions.first().cloned();
    Some((
        changed >= CHANGED_PIXEL_MINIMUM,
        ratio,
        scene_change,
        region,
        changed_regions,
        gray,
    ))
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
mod opencv_perception {
    use super::{
        ChangedRegion, CHANGED_PIXEL_MINIMUM, MAX_CHANGED_REGIONS, MINIMUM_REGION_PIXELS,
        PIXEL_DELTA_THRESHOLD, SCENE_CHANGE_RATIO, SENTINEL_HEIGHT, SENTINEL_WIDTH,
    };
    use image::GrayImage;
    use opencv::{
        core::{self, AlgorithmHint, Mat, MatTraitConst, MatTraitConstManual, Size, CV_32S},
        imgproc,
    };

    pub(super) fn estimate_change(
        rgb: &[u8],
        width: u32,
        height: u32,
        previous: Option<&GrayImage>,
        compute_level_target: u8,
    ) -> Option<(
        bool,
        f64,
        bool,
        Option<ChangedRegion>,
        Vec<ChangedRegion>,
        GrayImage,
    )> {
        if width == 0
            || height == 0
            || rgb.len()
                != (width as usize)
                    .checked_mul(height as usize)?
                    .checked_mul(3)?
        {
            return None;
        }
        let source_data = Mat::from_slice(rgb).ok()?;
        let source = source_data.reshape(3, height as i32).ok()?;
        let mut source_gray = Mat::default();
        imgproc::cvt_color(
            &source,
            &mut source_gray,
            imgproc::COLOR_RGB2GRAY,
            0,
            AlgorithmHint::ALGO_HINT_DEFAULT,
        )
        .ok()?;
        let mut gray = Mat::default();
        imgproc::resize(
            &source_gray,
            &mut gray,
            Size::new(SENTINEL_WIDTH as i32, SENTINEL_HEIGHT as i32),
            0.0,
            0.0,
            imgproc::INTER_AREA,
        )
        .ok()?;
        let gray_image = GrayImage::from_raw(
            SENTINEL_WIDTH,
            SENTINEL_HEIGHT,
            gray.data_bytes().ok()?.to_vec(),
        )?;
        let Some(previous) =
            previous.filter(|frame| frame.dimensions() == (SENTINEL_WIDTH, SENTINEL_HEIGHT))
        else {
            let full_frame = ChangedRegion {
                x: 0,
                y: 0,
                width: width as usize,
                height: height as usize,
            };
            return Some((
                true,
                1.0,
                false,
                Some(full_frame.clone()),
                vec![full_frame],
                gray_image,
            ));
        };

        let previous_data = Mat::from_slice(previous.as_raw()).ok()?;
        let previous_mat = previous_data.reshape(1, SENTINEL_HEIGHT as i32).ok()?;
        let mut delta = Mat::default();
        core::absdiff(&gray, &previous_mat, &mut delta).ok()?;
        let mut mask = Mat::default();
        imgproc::threshold(
            &delta,
            &mut mask,
            PIXEL_DELTA_THRESHOLD as f64 - 1.0,
            255.0,
            imgproc::THRESH_BINARY,
        )
        .ok()?;
        let changed_pixels = core::count_non_zero(&mask).ok()? as u32;
        let ratio = changed_pixels as f64 / (SENTINEL_WIDTH * SENTINEL_HEIGHT) as f64;
        let changed = changed_pixels >= CHANGED_PIXEL_MINIMUM;
        let scene_change = ratio >= SCENE_CHANGE_RATIO;
        if compute_level_target == 0 || changed_pixels == 0 {
            return Some((changed, ratio, scene_change, None, Vec::new(), gray_image));
        }
        let mut labels = Mat::default();
        let mut stats = Mat::default();
        let mut centroids = Mat::default();
        let component_count = imgproc::connected_components_with_stats(
            &mask,
            &mut labels,
            &mut stats,
            &mut centroids,
            4,
            CV_32S,
        )
        .ok()?;
        let mut components = Vec::new();
        for label in 1..component_count {
            let area = *stats.at_2d::<i32>(label, imgproc::CC_STAT_AREA).ok()? as u32;
            if area < MINIMUM_REGION_PIXELS {
                continue;
            }
            let x = *stats.at_2d::<i32>(label, imgproc::CC_STAT_LEFT).ok()? as usize;
            let y = *stats.at_2d::<i32>(label, imgproc::CC_STAT_TOP).ok()? as usize;
            let component_width = *stats.at_2d::<i32>(label, imgproc::CC_STAT_WIDTH).ok()? as usize;
            let component_height =
                *stats.at_2d::<i32>(label, imgproc::CC_STAT_HEIGHT).ok()? as usize;
            let x0 = x * width as usize / SENTINEL_WIDTH as usize;
            let y0 = y * height as usize / SENTINEL_HEIGHT as usize;
            let x1 = (x + component_width) * width as usize / SENTINEL_WIDTH as usize;
            let y1 = (y + component_height) * height as usize / SENTINEL_HEIGHT as usize;
            components.push((
                area,
                ChangedRegion {
                    x: x0,
                    y: y0,
                    width: x1.saturating_sub(x0),
                    height: y1.saturating_sub(y0),
                },
            ));
        }
        components.sort_by(|left, right| right.0.cmp(&left.0));
        let regions: Vec<_> = components
            .into_iter()
            .take(MAX_CHANGED_REGIONS)
            .map(|(_, region)| region)
            .collect();
        let largest = regions.first().cloned();
        Some((changed, ratio, scene_change, largest, regions, gray_image))
    }
}

#[cfg(not(feature = "opencv-perception"))]
fn connected_regions(mask: &mut [bool], width: usize, height: usize) -> Vec<ChangedRegion> {
    use std::collections::VecDeque;

    if mask.len() != (SENTINEL_WIDTH * SENTINEL_HEIGHT) as usize {
        return Vec::new();
    }
    let grid_width = SENTINEL_WIDTH as usize;
    let grid_height = SENTINEL_HEIGHT as usize;
    let mut bounds = Vec::new();
    for start in 0..mask.len() {
        if !mask[start] {
            continue;
        }
        mask[start] = false;
        let mut pending = VecDeque::from([start]);
        let mut count = 0u32;
        let mut min_x = start % grid_width;
        let mut max_x = min_x;
        let mut min_y = start / grid_width;
        let mut max_y = min_y;
        while let Some(index) = pending.pop_front() {
            count += 1;
            let x = index % grid_width;
            let y = index / grid_width;
            min_x = min_x.min(x);
            max_x = max_x.max(x);
            min_y = min_y.min(y);
            max_y = max_y.max(y);
            for neighbor in [
                (x > 0).then(|| index - 1),
                (x + 1 < grid_width).then(|| index + 1),
                (y > 0).then(|| index - grid_width),
                (y + 1 < grid_height).then(|| index + grid_width),
            ]
            .into_iter()
            .flatten()
            {
                if mask[neighbor] {
                    mask[neighbor] = false;
                    pending.push_back(neighbor);
                }
            }
        }
        if count >= MINIMUM_REGION_PIXELS {
            let x0 = min_x * width / grid_width;
            let y0 = min_y * height / grid_height;
            let x1 = (max_x + 1) * width / grid_width;
            let y1 = (max_y + 1) * height / grid_height;
            bounds.push((
                count,
                ChangedRegion {
                    x: x0,
                    y: y0,
                    width: x1.saturating_sub(x0),
                    height: y1.saturating_sub(y0),
                },
            ));
        }
    }
    bounds.sort_by(|left, right| right.0.cmp(&left.0));
    bounds
        .into_iter()
        .take(MAX_CHANGED_REGIONS)
        .map(|(_, region)| region)
        .collect()
}

fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity((bytes.len() + 2) / 3 * 4);
    for chunk in bytes.chunks(3) {
        let a = chunk[0];
        let b = *chunk.get(1).unwrap_or(&0);
        let c = *chunk.get(2).unwrap_or(&0);
        output.push(TABLE[(a >> 2) as usize] as char);
        output.push(TABLE[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[(((b & 15) << 2) | (c >> 6)) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(c & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_window_capture_dimensions_follow_window_size_and_retina_scale() {
        let window = CaptureDisplayGeometry {
            display_id: 1,
            origin_x: 100.0,
            origin_y: 50.0,
            width_points: 720.0,
            height_points: 320.0,
        };

        assert_eq!(capture_dimensions(window, 2.0), Ok((1440, 640)));
    }

    #[test]
    fn target_window_capture_dimensions_downscale_large_windows_without_changing_aspect() {
        let window = CaptureDisplayGeometry {
            display_id: 1,
            origin_x: 100.0,
            origin_y: 50.0,
            width_points: 1920.0,
            height_points: 1080.0,
        };
        let (width, height) = capture_dimensions(window, 2.0).unwrap();

        assert!(width <= MAX_WIDTH);
        assert!(height <= MAX_HEIGHT);
        assert!((width as f64 / height as f64 - 16.0 / 9.0).abs() < 0.01);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn screen_capture_kit_frame_statuses_are_counted_separately_from_processing_skips() {
        use screencapturekit::cm::SCFrameStatus;

        let counts = AtomicFrameStatusCounts::default();
        for status in [
            SCFrameStatus::Complete,
            SCFrameStatus::Idle,
            SCFrameStatus::Blank,
            SCFrameStatus::Suspended,
            SCFrameStatus::Started,
            SCFrameStatus::Stopped,
            SCFrameStatus::Unknown(99),
        ] {
            counts.record(Some(status));
        }
        counts.record(None);

        assert_eq!(
            counts.snapshot(),
            ScreenCaptureFrameStatusCounts {
                complete: 1,
                idle: 1,
                blank: 1,
                suspended: 1,
                started: 1,
                stopped: 1,
                unknown: 1,
                unavailable: 1,
            }
        );
    }

    #[test]
    fn capture_policy_updates_runtime_rate_with_bounded_latest_revision() {
        let configured_fps = AtomicU64::new(2);
        let compute_level = AtomicU64::new(0);
        let revision = AtomicU64::new(0);
        let mut requested_rates = Vec::new();

        apply_capture_rate_policy(
            20,
            2,
            4,
            &configured_fps,
            &compute_level,
            &revision,
            30,
            true,
            |fps| {
                requested_rates.push(fps);
                Ok(())
            },
        )
        .expect("runtime rate update should succeed");

        assert_eq!(requested_rates, [20]);
        assert_eq!(configured_fps.load(Ordering::Acquire), 20);
        assert_eq!(compute_level.load(Ordering::Acquire), 2);
        assert_eq!(revision.load(Ordering::Acquire), 4);

        let mut stale_update_called = false;
        apply_capture_rate_policy(
            8,
            1,
            3,
            &configured_fps,
            &compute_level,
            &revision,
            30,
            true,
            |_| {
                stale_update_called = true;
                Ok(())
            },
        )
        .expect("stale policy should be ignored");
        assert!(!stale_update_called);
        assert_eq!(configured_fps.load(Ordering::Acquire), 20);
        assert_eq!(compute_level.load(Ordering::Acquire), 2);
        assert_eq!(revision.load(Ordering::Acquire), 4);
    }

    #[test]
    fn capture_policy_respects_unsupported_os_cap_and_retries_failed_updates() {
        let configured_fps = AtomicU64::new(2);
        let compute_level = AtomicU64::new(0);
        let revision = AtomicU64::new(0);
        let mut unsupported_update_called = false;
        apply_capture_rate_policy(
            30,
            4,
            1,
            &configured_fps,
            &compute_level,
            &revision,
            2,
            false,
            |_| {
                unsupported_update_called = true;
                Ok(())
            },
        )
        .expect("unsupported runtime updates should preserve the fixed rate");
        assert!(!unsupported_update_called);
        assert_eq!(configured_fps.load(Ordering::Acquire), 2);
        assert_eq!(compute_level.load(Ordering::Acquire), 4);

        let failed = apply_capture_rate_policy(
            8,
            1,
            2,
            &configured_fps,
            &compute_level,
            &revision,
            30,
            true,
            |_| Err("injected update failure".into()),
        );
        assert_eq!(failed, Err("injected update failure".into()));
        assert_eq!(configured_fps.load(Ordering::Acquire), 2);

        let mut retry_rates = Vec::new();
        apply_capture_rate_policy(
            8,
            1,
            2,
            &configured_fps,
            &compute_level,
            &revision,
            30,
            true,
            |fps| {
                retry_rates.push(fps);
                Ok(())
            },
        )
        .expect("the same policy revision should be retryable");
        assert_eq!(retry_rates, [8]);
        assert_eq!(configured_fps.load(Ordering::Acquire), 8);
        assert_eq!(revision.load(Ordering::Acquire), 2);
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn cumulative_opencv_processing_time_saturates_instead_of_wrapping() {
        let total = AtomicU64::new(u64::MAX - 2);
        add_cumulative_processing_us(&total, 5);
        assert_eq!(total.load(Ordering::Relaxed), u64::MAX);
    }

    #[cfg(target_os = "macos")]
    struct PerceptionReplayResult {
        frames_processed: usize,
        total_change_pipeline_ms: f64,
        transient_roi_delay_ms: Option<f64>,
        persistent_roi_delay_ms: Option<f64>,
        expected_effect_delay_ms: Option<f64>,
        expected_effect_settled_active: bool,
    }

    #[cfg(target_os = "macos")]
    const REPLAY_EXPECTED_EFFECT_MS: f64 = 1_617.0;

    #[cfg(target_os = "macos")]
    fn replay_frame_at(time_ms: f64) -> Vec<u8> {
        let mut rgb = vec![0; 64 * 36 * 3];
        if (1_100.0..1_300.0).contains(&time_ms) {
            for y in 10..15 {
                for x in 12..18 {
                    let offset = (y * 64 + x) * 3;
                    rgb[offset..offset + 3].fill(255);
                }
            }
        }
        if time_ms >= 4_000.0 {
            for y in 25..30 {
                for x in 42..48 {
                    let offset = (y * 64 + x) * 3;
                    rgb[offset..offset + 3].fill(255);
                }
            }
        }
        // Simulated task effect: the known control changes state after the
        // click event. A separate fixture oracle checks this image state.
        if time_ms >= REPLAY_EXPECTED_EFFECT_MS {
            for y in 10..16 {
                for x in 28..34 {
                    let offset = (y * 64 + x) * 3;
                    rgb[offset..offset + 3].fill(192);
                }
            }
        }
        rgb
    }

    #[cfg(target_os = "macos")]
    fn replay_expected_effect_visible(rgb: &[u8]) -> bool {
        (10..16).all(|y| {
            (28..34).all(|x| {
                let offset = (y * 64 + x) * 3;
                rgb.get(offset..offset + 3) == Some(&[192, 192, 192])
            })
        })
    }

    #[cfg(target_os = "macos")]
    fn run_perception_replay(adaptive: bool) -> PerceptionReplayResult {
        use crate::perception_controller::{PerceptionController, PerceptionRates};

        const SOURCE_FPS: f64 = 30.0;
        const DURATION_MS: f64 = 6_000.0;
        const TRANSIENT_CHANGE_MS: f64 = 1_100.0;
        const PERSISTENT_CHANGE_MS: f64 = 4_000.0;

        let mut controller = PerceptionController::new(PerceptionRates {
            idle_fps: 2,
            active_fps: 8,
            high_fps: 20,
            burst_fps: 30,
            capture_fps_ceiling: 30,
        });
        let mut previous_gray = None;
        let mut next_capture_ms = 0.0;
        let mut ax_signaled = false;
        let mut click_signaled = false;
        let mut frames_processed = 0;
        let mut total_change_pipeline_ms = 0.0;
        let mut transient_roi_delay_ms = None;
        let mut persistent_roi_delay_ms = None;
        let mut expected_effect_delay_ms = None;
        let mut expected_effect_settled_active = false;
        let source_step_ms = 1_000.0 / SOURCE_FPS;

        for source_index in 0..=(DURATION_MS / source_step_ms).round() as usize {
            let now_ms = source_index as f64 * source_step_ms;
            if adaptive && !ax_signaled && now_ms >= 1_000.0 {
                controller.signal("ax_change", None, now_ms as u64);
                ax_signaled = true;
            }
            if adaptive && !click_signaled && now_ms >= 1_000.0 {
                controller.signal("action_click", None, now_ms as u64);
                click_signaled = true;
            }
            if now_ms + 0.001 < next_capture_ms {
                continue;
            }

            let compute_level = if adaptive {
                controller.snapshot(now_ms as u64).compute_level_target
            } else {
                1
            };
            let rgb = replay_frame_at(now_ms);
            let expected_effect_visible = replay_expected_effect_visible(&rgb);
            let initial_baseline = previous_gray.is_none();
            let started = Instant::now();
            let (changed, changed_ratio, scene_change, _, changed_regions, gray) =
                estimate_change(&rgb, 64, 36, previous_gray.as_ref(), compute_level)
                    .expect("deterministic replay frames should be valid");
            total_change_pipeline_ms += started.elapsed().as_secs_f64() * 1_000.0;
            frames_processed += 1;
            if should_advance_change_baseline(compute_level, changed, previous_gray.is_some()) {
                previous_gray = Some(gray);
            }

            if !initial_baseline {
                let transient_hit = changed_regions.iter().any(|region| {
                    region.x < 18
                        && region.x.saturating_add(region.width) > 12
                        && region.y < 15
                        && region.y.saturating_add(region.height) > 10
                });
                if transient_hit && transient_roi_delay_ms.is_none() {
                    transient_roi_delay_ms = Some(now_ms - TRANSIENT_CHANGE_MS);
                }
                let persistent_hit = changed_regions.iter().any(|region| {
                    region.x < 48
                        && region.x.saturating_add(region.width) > 42
                        && region.y < 30
                        && region.y.saturating_add(region.height) > 25
                });
                if persistent_hit
                    && now_ms >= PERSISTENT_CHANGE_MS
                    && persistent_roi_delay_ms.is_none()
                {
                    persistent_roi_delay_ms = Some(now_ms - PERSISTENT_CHANGE_MS);
                }
            }

            if adaptive {
                let signal = if initial_baseline {
                    "stable_frame"
                } else if scene_change {
                    "scene_change"
                } else if changed {
                    "pixel_change"
                } else {
                    "stable_frame"
                };
                let mut snapshot = controller.signal(signal, Some(changed_ratio), now_ms as u64);
                if expected_effect_visible && expected_effect_delay_ms.is_none() {
                    expected_effect_delay_ms = Some(now_ms - REPLAY_EXPECTED_EFFECT_MS);
                    // This follows a separate pixel oracle over the resulting
                    // image; it is not a scripted controller timestamp.
                    snapshot = controller.signal("expected_effect", None, now_ms as u64);
                    expected_effect_settled_active =
                        snapshot.state == crate::perception_controller::PerceptionState::Active;
                }
                next_capture_ms = now_ms + 1_000.0 / snapshot.achievable_fps.max(1) as f64;
            } else {
                if expected_effect_visible && expected_effect_delay_ms.is_none() {
                    expected_effect_delay_ms = Some(now_ms - REPLAY_EXPECTED_EFFECT_MS);
                }
                next_capture_ms = now_ms + source_step_ms;
            }
        }

        PerceptionReplayResult {
            frames_processed,
            total_change_pipeline_ms,
            transient_roi_delay_ms,
            persistent_roi_delay_ms,
            expected_effect_delay_ms,
            expected_effect_settled_active,
        }
    }

    #[cfg(target_os = "macos")]
    struct AnimationReplayResult {
        frames_processed: usize,
        high_frames: usize,
        visual_changes: usize,
        high_seen_by_window: [bool; 3],
    }

    #[cfg(target_os = "macos")]
    fn animation_frame_at(time_ms: f64, significant: bool, phase_step_ms: f64) -> Vec<u8> {
        const WIDTH: usize = 64;
        const HEIGHT: usize = 36;
        let mut rgb = vec![0; WIDTH * HEIGHT * 3];
        let phase = ((time_ms / phase_step_ms).floor() as usize) % 4;
        let (size, x) = if significant {
            (8, if phase % 2 == 0 { 8 } else { 48 })
        } else {
            (3, 8 + (phase % 2) * 12)
        };
        let y = if significant { 14 } else { 16 };
        for row in y..y + size {
            for column in x..x + size {
                let offset = (row * WIDTH + column) * 3;
                rgb[offset..offset + 3].fill(255);
            }
        }
        rgb
    }

    #[cfg(target_os = "macos")]
    fn run_animation_replay(
        adaptive: bool,
        significant: bool,
        phase_step_ms: f64,
    ) -> AnimationReplayResult {
        use crate::perception_controller::{
            PerceptionController, PerceptionRates, PerceptionState,
        };

        const SOURCE_FPS: f64 = 30.0;
        const DURATION_MS: f64 = 12_000.0;
        let mut controller = PerceptionController::new(PerceptionRates {
            idle_fps: 2,
            active_fps: 8,
            high_fps: 20,
            burst_fps: 30,
            capture_fps_ceiling: 30,
        });
        let mut previous_gray = None;
        let mut next_capture_ms = 0.0;
        let mut frames_processed = 0;
        let mut high_frames = 0;
        let mut visual_changes = 0;
        let mut high_seen_by_window = [false; 3];
        let mut first_high_at_ms = None;
        let source_step_ms = 1_000.0 / SOURCE_FPS;

        for source_index in 0..=(DURATION_MS / source_step_ms).round() as usize {
            let now_ms = source_index as f64 * source_step_ms;
            if now_ms + 0.001 < next_capture_ms {
                continue;
            }
            let compute_level = if adaptive {
                controller.snapshot(now_ms as u64).compute_level_target
            } else {
                1
            };
            let initial_baseline = previous_gray.is_none();
            let rgb = animation_frame_at(now_ms, significant, phase_step_ms);
            let (changed, changed_ratio, scene_change, _, _, gray) =
                estimate_change(&rgb, 64, 36, previous_gray.as_ref(), compute_level)
                    .expect("animation replay frames should be valid");
            frames_processed += 1;
            if !initial_baseline && changed {
                visual_changes += 1;
            }
            if should_advance_change_baseline(compute_level, changed, previous_gray.is_some()) {
                previous_gray = Some(gray);
            }

            let signal = if initial_baseline {
                "stable_frame"
            } else if scene_change {
                "scene_change"
            } else if changed {
                "pixel_change"
            } else {
                "stable_frame"
            };
            let snapshot = controller.signal(signal, Some(changed_ratio), now_ms as u64);
            if adaptive && snapshot.state == PerceptionState::High {
                high_frames += 1;
                let origin = *first_high_at_ms.get_or_insert(now_ms);
                let window = ((now_ms - origin) / 5_000.0).floor() as usize;
                if let Some(high_seen) = high_seen_by_window.get_mut(window) {
                    *high_seen = true;
                }
            }
            next_capture_ms = if adaptive {
                now_ms + 1_000.0 / snapshot.achievable_fps.max(1) as f64
            } else {
                now_ms + source_step_ms
            };
        }

        AnimationReplayResult {
            frames_processed,
            high_frames,
            visual_changes,
            high_seen_by_window,
        }
    }

    fn tracking_test_frame(sequence: u64, observed_at: f64, width: usize) -> CapturedFrame {
        CapturedFrame {
            capture_session_id: "capture-test-session".into(),
            target_application: None,
            sequence,
            observed_at,
            width,
            height: 1,
            display_geometry: CaptureDisplayGeometry {
                display_id: 1,
                origin_x: 0.0,
                origin_y: 0.0,
                width_points: width as f64,
                height_points: 1.0,
            },
            rgb: Arc::new(vec![sequence as u8; width * 3]),
        }
    }

    #[test]
    fn normalized_box_maps_through_retina_and_negative_display_origin() -> Result<(), String> {
        let geometry = CaptureDisplayGeometry {
            display_id: 7,
            origin_x: -1440.0,
            origin_y: 0.0,
            width_points: 1440.0,
            height_points: 900.0,
        };
        // Captured pixels are 2x the display's logical point dimensions.
        let point =
            map_normalized_bbox_to_global_point(geometry, 2880, 1800, [0.25, 0.5, 0.35, 0.6])?;
        assert!((point[0] - -1008.0).abs() < 1e-9);
        assert!((point[1] - 495.0).abs() < 1e-9);
        Ok(())
    }

    #[test]
    fn target_window_must_fit_one_valid_display() {
        let display = CaptureDisplayGeometry {
            display_id: 2,
            origin_x: -100.0,
            origin_y: 0.0,
            width_points: 100.0,
            height_points: 80.0,
        };
        assert!(display_fully_contains_window(
            display, -90.0, 10.0, 80.0, 60.0
        ));
        assert!(!display_fully_contains_window(
            display, -90.0, 10.0, 101.0, 60.0
        ));
        assert!(!display_fully_contains_window(
            display,
            -90.0,
            10.0,
            f64::NAN,
            60.0
        ));
    }

    #[test]
    fn capture_window_selection_requires_exact_visible_process_and_bundle_identity() {
        let target = CaptureTargetApplication {
            application_pid: 5628,
            application_bundle_id: "local.olcr.opencv.tracking.fixture".into(),
        };

        assert!(capture_window_matches_target(
            true,
            5628,
            "local.olcr.opencv.tracking.fixture",
            &target,
        ));
        assert!(!capture_window_matches_target(
            false,
            5628,
            "local.olcr.opencv.tracking.fixture",
            &target,
        ));
        assert!(!capture_window_matches_target(
            true,
            5629,
            "local.olcr.opencv.tracking.fixture",
            &target,
        ));
        assert!(!capture_window_matches_target(
            true,
            5628,
            "local.olcr.other-app",
            &target,
        ));
    }

    #[test]
    fn capture_window_selection_ignores_only_a_tiny_dominant_accessory_surface() {
        assert_eq!(
            dominant_capture_window_index(&[(66.0, 20.0), (720.0, 328.0)]),
            Ok(1)
        );
        assert_eq!(dominant_capture_window_index(&[(720.0, 328.0)]), Ok(0));
        assert_eq!(
            dominant_capture_window_index(&[(720.0, 480.0), (680.0, 460.0)]),
            Err("SCREEN_CAPTURE_TARGET_WINDOW_AMBIGUOUS")
        );
        assert_eq!(
            dominant_capture_window_index(&[(0.0, 0.0), (f64::NAN, 12.0)]),
            Err("SCREEN_CAPTURE_TARGET_WINDOW_NOT_FOUND")
        );
    }

    #[test]
    fn normalized_box_mapping_fails_closed_for_invalid_or_mismatched_geometry() {
        let geometry = CaptureDisplayGeometry {
            display_id: 1,
            origin_x: 0.0,
            origin_y: 0.0,
            width_points: 1440.0,
            height_points: 900.0,
        };
        assert_eq!(
            map_normalized_bbox_to_global_point(geometry, 1920, 1080, [0.1, 0.2, 0.3, 0.4]),
            Err("SCREEN_POINTER_DISPLAY_ASPECT_MISMATCH")
        );
        assert_eq!(
            map_normalized_bbox_to_global_point(geometry, 2880, 1800, [-0.1, 0.2, 0.3, 0.4]),
            Err("SCREEN_POINTER_BBOX_INVALID")
        );
        let bad_geometry = CaptureDisplayGeometry {
            width_points: f64::NAN,
            ..geometry
        };
        assert_eq!(
            map_normalized_bbox_to_global_point(bad_geometry, 2880, 1800, [0.1, 0.2, 0.3, 0.4]),
            Err("SCREEN_POINTER_DISPLAY_GEOMETRY_INVALID")
        );
    }

    #[test]
    fn capture_session_ids_change_across_start_stop_cycles() {
        let first = new_capture_session_id();
        let next = new_capture_session_id();
        assert!(!first.is_empty());
        assert_ne!(first, next);
    }

    #[test]
    fn tracking_frame_pairs_require_matching_dimensions_and_fresh_ordered_times() {
        let first = tracking_test_frame(1, 10.0, 2);
        assert!(validate_tracking_frames(&first, &tracking_test_frame(2, 10.5, 2)).is_ok());
        assert_eq!(
            validate_tracking_frames(&first, &tracking_test_frame(2, 13.0, 2)).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_FRAMES_STALE"
        );
        assert_eq!(
            validate_tracking_frames(&first, &tracking_test_frame(2, 10.5, 3)).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_DIMENSIONS_MISMATCH"
        );
        let other_display = CapturedFrame {
            display_geometry: CaptureDisplayGeometry {
                display_id: 2,
                ..first.display_geometry
            },
            ..tracking_test_frame(2, 10.5, 2)
        };
        assert_eq!(
            validate_tracking_frames(&first, &other_display).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_DISPLAY_MISMATCH"
        );
        let other_session = CapturedFrame {
            capture_session_id: "different-session".into(),
            ..tracking_test_frame(2, 10.5, 2)
        };
        assert_eq!(
            validate_tracking_frames(&first, &other_session).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_SESSION_MISMATCH"
        );
        let other_target = CapturedFrame {
            target_application: Some(CaptureTargetApplication {
                application_pid: 123,
                application_bundle_id: "example.app".into(),
            }),
            ..tracking_test_frame(2, 10.5, 2)
        };
        assert_eq!(
            validate_tracking_frames(&first, &other_target).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_TARGET_MISMATCH"
        );
        assert_eq!(
            validate_tracking_frames(&first, &tracking_test_frame(2, 10.0, 2)).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_FRAMES_STALE"
        );
    }

    #[test]
    fn grounding_tracking_pair_requires_the_exact_seed_sequence() {
        let seed = tracking_test_frame(41, 100.0, 2);
        let next = tracking_test_frame(46, 102.0, 2);
        assert!(validate_grounding_seed_pair(&seed, &next, 41).is_ok());
        assert_eq!(
            validate_grounding_seed_pair(&seed, &next, 42).unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_SEED_SEQUENCE_MISMATCH"
        );
        assert_eq!(
            validate_grounding_seed_pair(&seed, &tracking_test_frame(41, 100.5, 2), 41)
                .unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_NEXT_FRAME_NOT_NEWER"
        );
        assert_eq!(
            validate_grounding_seed_pair(&seed, &tracking_test_frame(46, 110.1, 2), 41)
                .unwrap_err(),
            "SCREEN_CAPTURE_TRACKING_FRAMES_STALE"
        );
        assert!(validate_grounding_seed_pair(&seed, &tracking_test_frame(44, 109.9, 2), 41).is_ok());
        assert!(
            validate_grounding_seed_pair(&seed, &tracking_test_frame(44, 104.8, 2), 41,).is_ok()
        );
    }

    #[test]
    fn pinned_grounding_seed_can_be_reused_for_tracking_and_pointer_preflight() {
        let seed = Arc::new(Mutex::new(Some(tracking_test_frame(41, 100.0, 2))));

        let tracking_seed = clone_pinned_grounding_seed(&seed).unwrap();
        let pointer_preflight_seed = clone_pinned_grounding_seed(&seed).unwrap();

        assert_eq!(tracking_seed.sequence, 41);
        assert_eq!(pointer_preflight_seed.sequence, 41);
        assert_eq!(seed.lock().unwrap().as_ref().unwrap().sequence, 41);
    }

    #[test]
    fn grounding_seed_must_be_fresh_and_have_a_valid_clock() {
        let seed = tracking_test_frame(1, 100.0, 2);
        assert!(validate_grounding_seed_freshness(&seed, 100.0 + MAX_GROUNDING_SEED_AGE_SECONDS).is_ok());
        assert_eq!(
            validate_grounding_seed_freshness(&seed, 100.0 + MAX_GROUNDING_SEED_AGE_SECONDS + 0.0001).unwrap_err(),
            "SCREEN_CAPTURE_GROUNDING_SEED_STALE"
        );
        assert_eq!(
            validate_grounding_seed_freshness(&seed, 98.0).unwrap_err(),
            "SCREEN_CAPTURE_GROUNDING_SEED_STALE"
        );
    }

    #[test]
    fn bgra_rows_convert_to_rgb_and_ignore_stride_padding() {
        let source = [3, 2, 1, 255, 6, 5, 4, 255, 0, 0, 99, 9];
        assert_eq!(bgra_to_rgb(&source, 2, 1, 12).unwrap(), [1, 2, 3, 4, 5, 6]);
    }

    #[test]
    fn bgra_conversion_rejects_invalid_dimensions_and_truncated_input() {
        assert!(bgra_to_rgb(&[], 0, 1, 0).is_none());
        assert!(bgra_to_rgb(&[], MAX_WIDTH + 1, 1, (MAX_WIDTH + 1) * 4).is_none());
        assert!(bgra_to_rgb(&[0; 7], 2, 1, 8).is_none());
    }

    #[test]
    fn base64_encoder_matches_padding_contract() {
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
    }

    #[test]
    fn processing_percentiles_use_bounded_nearest_rank_samples() {
        let samples = VecDeque::from([4.0, 1.0, 100.0, 3.0]);
        assert_eq!(processing_percentiles_ms(&samples), (3.0, 100.0));
        assert_eq!(processing_percentiles_ms(&VecDeque::new()), (0.0, 0.0));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn frame_processing_guard_counts_only_unprocessed_callbacks() {
        let skipped = AtomicU64::new(0);
        {
            let _guard = FrameProcessingGuard {
                skipped: &skipped,
                processed: false,
            };
        }
        assert_eq!(skipped.load(Ordering::Relaxed), 1);
        {
            let mut guard = FrameProcessingGuard {
                skipped: &skipped,
                processed: false,
            };
            guard.mark_processed();
        }
        assert_eq!(skipped.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn tracker_processing_samples_are_finite_and_keep_only_the_latest_bounded_window() {
        let mut samples = VecDeque::with_capacity(PROCESSING_SAMPLE_CAPACITY);
        for value in 0..=PROCESSING_SAMPLE_CAPACITY {
            record_processing_sample_ms(&mut samples, value as f64);
        }
        record_processing_sample_ms(&mut samples, f64::NAN);
        record_processing_sample_ms(&mut samples, f64::INFINITY);
        record_processing_sample_ms(&mut samples, -1.0);

        assert_eq!(samples.len(), PROCESSING_SAMPLE_CAPACITY);
        assert_eq!(samples.front().copied(), Some(1.0));
        assert_eq!(
            samples.back().copied(),
            Some(PROCESSING_SAMPLE_CAPACITY as f64)
        );
        assert_eq!(
            optional_processing_percentiles_ms(&samples),
            (Some(60.0), Some(114.0))
        );
        assert_eq!(
            optional_processing_percentiles_ms(&VecDeque::new()),
            (None, None)
        );
    }

    #[test]
    fn observed_capture_fps_uses_recent_sample_span() {
        let start = Instant::now();
        let samples = VecDeque::from([
            start,
            start + Duration::from_millis(500),
            start + Duration::from_secs(1),
        ]);
        assert_eq!(observed_capture_fps(&samples), Some(2.0));
        assert_eq!(observed_capture_fps(&VecDeque::from([start])), None);
    }

    #[cfg(not(feature = "opencv-perception"))]
    #[test]
    fn connected_regions_keep_distant_changes_separate_and_bound_the_result() {
        let mut mask = vec![false; (SENTINEL_WIDTH * SENTINEL_HEIGHT) as usize];
        for (x, y) in [(1usize, 1usize), (2, 1), (45, 30), (46, 30)] {
            mask[y * SENTINEL_WIDTH as usize + x] = true;
        }

        let regions = connected_regions(&mut mask, 640, 360);

        assert_eq!(regions.len(), 2);
        assert!(regions[0].x < 100);
        assert!(regions[1].x > 400);
        assert!(regions
            .iter()
            .all(|region| region.width > 0 && region.height > 0));
    }

    #[cfg(not(feature = "opencv-perception"))]
    #[test]
    fn connected_regions_cap_fragmented_noise() {
        let mut mask = vec![false; (SENTINEL_WIDTH * SENTINEL_HEIGHT) as usize];
        for index in 0..MAX_CHANGED_REGIONS + 4 {
            let x = index * 3;
            mask[x] = true;
        }

        assert_eq!(
            connected_regions(&mut mask, 640, 360).len(),
            MAX_CHANGED_REGIONS
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn change_estimate_uses_bounded_gray_frame_and_reports_changed_bounds() {
        let mut first = vec![0; 64 * 36 * 3];
        let (initial_changed, initial_ratio, initial_scene_change, _, _, baseline) =
            estimate_change(&first, 64, 36, None, 0).unwrap();
        assert!(initial_changed);
        assert_eq!(initial_ratio, 1.0);
        assert!(
            !initial_scene_change,
            "the initial frame is a baseline, not a scene transition"
        );

        let (unchanged, unchanged_ratio, unchanged_scene_change, region, regions, _) =
            estimate_change(&first, 64, 36, Some(&baseline), 1).unwrap();
        assert!(!unchanged);
        assert_eq!(unchanged_ratio, 0.0);
        assert!(!unchanged_scene_change);
        assert!(region.is_none());
        assert!(regions.is_empty());

        for y in 12..24 {
            for x in 20..36 {
                let offset = (y * 64 + x) * 3;
                first[offset..offset + 3].fill(255);
            }
        }
        let (changed, ratio, scene_change, region, regions, _) =
            estimate_change(&first, 64, 36, Some(&baseline), 1).unwrap();
        assert!(changed);
        assert!(!scene_change);
        assert!(ratio > 0.0);
        let region = region.expect("changed region should be bounded");
        assert!(region.width > 0 && region.height > 0);
        assert!(region.x < 64 && region.y < 36);
        assert_eq!(regions.len(), 1);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn fixed_30fps_vs_adaptive_replay_processes_the_same_synthetic_control_sequence() {
        let fixed = run_perception_replay(false);
        let adaptive = run_perception_replay(true);

        assert!(fixed
            .transient_roi_delay_ms
            .is_some_and(|delay| delay.abs() < 0.001));
        assert!(adaptive
            .transient_roi_delay_ms
            .is_some_and(|delay| delay.abs() < 0.001));
        assert!(fixed
            .persistent_roi_delay_ms
            .is_some_and(|delay| delay.abs() < 0.001));
        assert!(
            adaptive
                .persistent_roi_delay_ms
                .is_some_and(|delay| (0.0..=750.0).contains(&delay)),
            "persistent ROI must be reacquired within 750 ms, got {:?}",
            adaptive.persistent_roi_delay_ms
        );
        assert!(fixed
            .expected_effect_delay_ms
            .is_some_and(|delay| (0.0..34.0).contains(&delay)));
        assert!(adaptive
            .expected_effect_delay_ms
            .is_some_and(|delay| (0.0..34.0).contains(&delay)));
        assert!(adaptive.expected_effect_settled_active);
        assert!(adaptive.frames_processed < fixed.frames_processed);

        eprintln!(
            "APC_REPLAY fixed_30 frames={} change_pipeline_ms={:.3} transient_roi_delay_ms={:.1} persistent_roi_delay_ms={:.1} verified_effect_delay_ms={:.1}; adaptive frames={} change_pipeline_ms={:.3} transient_roi_delay_ms={:.1} persistent_roi_delay_ms={:.1} verified_effect_delay_ms={:.1}",
            fixed.frames_processed,
            fixed.total_change_pipeline_ms,
            fixed.transient_roi_delay_ms.unwrap_or(f64::NAN),
            fixed.persistent_roi_delay_ms.unwrap_or(f64::NAN),
            fixed.expected_effect_delay_ms.unwrap_or(f64::NAN),
            adaptive.frames_processed,
            adaptive.total_change_pipeline_ms,
            adaptive.transient_roi_delay_ms.unwrap_or(f64::NAN),
            adaptive.persistent_roi_delay_ms.unwrap_or(f64::NAN),
            adaptive.expected_effect_delay_ms.unwrap_or(f64::NAN),
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn adaptive_sampling_keeps_small_animation_active_and_bounds_large_animation() {
        let fixed = run_animation_replay(false, false, 400.0);
        let small = run_animation_replay(true, false, 400.0);
        let large = run_animation_replay(true, true, 400.0);
        let aliased = run_animation_replay(true, false, 250.0);

        assert_eq!(fixed.frames_processed, 361);
        assert!(small.frames_processed < fixed.frames_processed / 2);
        assert_eq!(
            small.high_frames, 0,
            "small motion must not escalate to HIGH"
        );
        assert!(large.frames_processed < fixed.frames_processed);
        assert!(
            large.high_frames > 0,
            "large motion should briefly escalate to HIGH"
        );
        assert!(large.high_frames < large.frames_processed);
        assert_eq!(
            large.high_seen_by_window,
            [true, false, true],
            "continuous large animation may use the initial five-second visual-HIGH window, must stay suppressed during the five-second cooldown, and may be reevaluated afterward"
        );
        assert_eq!(
            aliased.visual_changes, 0,
            "2 fps can alias a 1 s periodic animation"
        );
        assert_eq!(aliased.frames_processed, 25);

        eprintln!(
            "APC_ANIMATION_REPLAY fixed_30_frames={} small_animation_adaptive_frames={} small_high_frames={} large_animation_adaptive_frames={} large_high_frames={} aliased_animation_adaptive_frames={} aliased_animation_visual_changes={}",
            fixed.frames_processed,
            small.frames_processed,
            small.high_frames,
            large.frames_processed,
            large.high_frames,
            aliased.frames_processed,
            aliased.visual_changes,
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "manual synthetic full-resolution frame-pipeline benchmark"]
    fn full_resolution_bgra_to_change_pipeline_benchmark() {
        const WIDTH: usize = MAX_WIDTH;
        const HEIGHT: usize = MAX_HEIGHT;
        const STRIDE: usize = WIDTH * 4 + 64;
        const SAMPLE_COUNT: usize = PROCESSING_SAMPLE_CAPACITY;

        let fixture_frame = |active: bool| {
            let mut bgra = vec![0_u8; STRIDE * HEIGHT];
            if active {
                for y in 400..460 {
                    let start = y * STRIDE + 600 * 4;
                    for pixel in bgra[start..start + 120 * 4].chunks_exact_mut(4) {
                        pixel.copy_from_slice(&[255, 255, 255, 255]);
                    }
                }
            }
            bgra
        };

        for compute_level_target in [0_u8, 1, 2] {
            let baseline_source = fixture_frame(false);
            let baseline_rgb = bgra_to_rgb(&baseline_source, WIDTH, HEIGHT, STRIDE)
                .expect("fixture BGRA layout should be valid");
            let baseline_gray = estimate_change(
                &baseline_rgb,
                WIDTH as u32,
                HEIGHT as u32,
                None,
                compute_level_target,
            )
            .expect("baseline frame should be valid")
            .5;
            let mut previous_gray = Some(baseline_gray);

            let mut samples = VecDeque::with_capacity(SAMPLE_COUNT);
            let mut region_frames = 0_usize;
            for index in 0..SAMPLE_COUNT {
                let source = fixture_frame(index % 2 == 1);
                let started = Instant::now();
                let rgb = bgra_to_rgb(&source, WIDTH, HEIGHT, STRIDE)
                    .expect("fixture BGRA layout should be valid");
                let (changed, _, _, _, changed_regions, gray) = estimate_change(
                    &rgb,
                    WIDTH as u32,
                    HEIGHT as u32,
                    previous_gray.as_ref(),
                    compute_level_target,
                )
                .expect("full-resolution fixture should be valid");
                record_processing_sample_ms(
                    &mut samples,
                    started.elapsed().as_secs_f64() * 1_000.0,
                );
                region_frames += usize::from(!changed_regions.is_empty());
                if should_advance_change_baseline(
                    compute_level_target,
                    changed,
                    previous_gray.is_some(),
                ) {
                    previous_gray = Some(gray);
                }
            }
            #[cfg(feature = "opencv-perception")]
            assert_eq!(
                region_frames,
                if compute_level_target == 0 {
                    0
                } else {
                    SAMPLE_COUNT - 1
                },
                "OpenCV compute levels should gate connected-component extraction"
            );
            #[cfg(not(feature = "opencv-perception"))]
            assert_eq!(
                region_frames,
                SAMPLE_COUNT - 1,
                "the feature-off baseline keeps its existing region extraction"
            );
            let (p50_ms, p95_ms) = processing_percentiles_ms(&samples);
            println!(
                "FULL_RESOLUTION_FRAME_PIPELINE width={WIDTH} height={HEIGHT} stride={STRIDE} compute_level={compute_level_target} samples={SAMPLE_COUNT} p50_ms={p50_ms:.3} p95_ms={p95_ms:.3} frames_with_regions={region_frames}"
            );
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn change_estimate_flags_large_transitions_as_scene_changes() {
        let baseline_rgb = vec![0; 64 * 36 * 3];
        let (_, _, _, _, _, baseline) = estimate_change(&baseline_rgb, 64, 36, None, 0).unwrap();
        let mut changed_rgb = baseline_rgb;
        for pixel in changed_rgb.chunks_exact_mut(3).take((64 * 36 * 40) / 100) {
            pixel.fill(255);
        }

        let (changed, ratio, scene_change, _, _, _) =
            estimate_change(&changed_rgb, 64, 36, Some(&baseline), 1).unwrap();

        assert!(changed);
        assert!(ratio >= SCENE_CHANGE_RATIO);
        assert!(scene_change);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn change_estimate_rejects_malformed_or_oversized_frames() {
        assert!(estimate_change(&[], 0, 10, None, 0).is_none());
        assert!(estimate_change(&[0; 3], 2, 2, None, 0).is_none());
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_l0_defers_components_and_keeps_the_change_baseline_for_l1() {
        let baseline_rgb = vec![0; 64 * 36 * 3];
        let (_, _, _, _, _, baseline) = estimate_change(&baseline_rgb, 64, 36, None, 0).unwrap();
        let mut changed_rgb = baseline_rgb;
        for y in 12..24 {
            for x in 20..36 {
                let offset = (y * 64 + x) * 3;
                changed_rgb[offset..offset + 3].fill(255);
            }
        }

        let (changed_at_l0, ratio_at_l0, scene_at_l0, region_at_l0, regions_at_l0, _) =
            estimate_change(&changed_rgb, 64, 36, Some(&baseline), 0).unwrap();
        assert!(changed_at_l0);
        assert!(ratio_at_l0 > 0.0);
        assert!(!scene_at_l0);
        assert!(region_at_l0.is_none());
        assert!(regions_at_l0.is_empty());
        assert!(!should_advance_change_baseline(0, changed_at_l0, true));

        let (changed_at_l1, _, _, region_at_l1, regions_at_l1, _) =
            estimate_change(&changed_rgb, 64, 36, Some(&baseline), 1).unwrap();
        assert!(changed_at_l1);
        assert!(region_at_l1.is_some());
        assert_eq!(regions_at_l1.len(), 1);
        assert!(should_advance_change_baseline(1, changed_at_l1, true));
        assert!(should_advance_change_baseline(0, true, false));
        assert!(should_advance_change_baseline(0, false, true));
    }
}
