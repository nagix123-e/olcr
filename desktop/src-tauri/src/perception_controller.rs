//! Bounded event-driven perception policy, separate from platform capture rate.

use rand::Rng;
use serde::Serialize;
use std::sync::OnceLock;
use std::time::Instant;

const SIGNIFICANT_PIXEL_RATIO: f64 = 0.02;
const AX_ACTIVE_MS: u64 = 3_000;
const HIGH_HOLD_MS: u64 = 2_500;
const VISUAL_HIGH_MAX_MS: u64 = 5_000;
const VISUAL_HIGH_COOLDOWN_MS: u64 = 5_000;
// Three unchanged samples can all land inside one animation phase at HIGH
// cadence; require a sustained quiet span before resetting the visual cap.
const VISUAL_STABLE_RESET_MS: u64 = 1_500;
const TRACKING_ACTIVE_LEASE_MS: u64 = 5_000;
const BURST_MS: u64 = 1_000;
const BURST_COOLDOWN_MS: u64 = 8_000;
const STABLE_FRAMES_TO_IDLE: u8 = 3;

static PROCESS_EPOCH: OnceLock<Instant> = OnceLock::new();
const MAX_ADVISORY_WAKEUPS: u64 = i64::MAX as u64;

fn increment_wakeup_count(value: &mut u64) {
    *value = value.saturating_add(1).min(MAX_ADVISORY_WAKEUPS);
}

pub fn monotonic_now_ms() -> u64 {
    PROCESS_EPOCH
        .get_or_init(Instant::now)
        .elapsed()
        .as_millis() as u64
}

pub fn supports_signal(name: &str) -> bool {
    matches!(
        name,
        "ax_change"
            | "scroll"
            | "pixel_change"
            | "scene_change"
            | "action_click"
            | "action_trigger"
            | "expected_effect"
            | "grounded_track_active"
            | "grounded_track_stopped"
            | "stable_frame"
            | "minor_motion"
    )
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PerceptionState {
    #[default]
    Idle,
    Active,
    High,
    Burst,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PerceptionStateDurations {
    pub idle_ms: u64,
    pub active_ms: u64,
    pub high_ms: u64,
    pub burst_ms: u64,
}

impl PerceptionStateDurations {
    fn add(&mut self, state: PerceptionState, elapsed_ms: u64) {
        let duration = match state {
            PerceptionState::Idle => &mut self.idle_ms,
            PerceptionState::Active => &mut self.active_ms,
            PerceptionState::High => &mut self.high_ms,
            PerceptionState::Burst => &mut self.burst_ms,
        };
        *duration = duration.saturating_add(elapsed_ms);
    }
}

impl Default for PerceptionStateDurations {
    fn default() -> Self {
        Self {
            idle_ms: 0,
            active_ms: 0,
            high_ms: 0,
            burst_ms: 0,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PerceptionSnapshot {
    pub controller_id: String,
    pub revision: u64,
    pub state: PerceptionState,
    pub reason: &'static str,
    pub target_fps: u32,
    pub capture_fps_ceiling: u32,
    pub achievable_fps: u32,
    pub compute_level_target: u8,
    pub burst_available: bool,
    pub state_durations_ms: PerceptionStateDurations,
    /// Known external AX/action/scroll signals that woke policy evaluation.
    pub event_wakeups: u64,
    /// Pixel-derived change/scene signals that woke policy evaluation.
    pub pixel_change_wakeups: u64,
}

#[derive(Clone, Debug)]
pub struct PerceptionRates {
    pub idle_fps: u32,
    pub active_fps: u32,
    pub high_fps: u32,
    pub burst_fps: u32,
    pub capture_fps_ceiling: u32,
}

impl Default for PerceptionRates {
    fn default() -> Self {
        Self {
            idle_fps: 2,
            active_fps: 8,
            high_fps: 20,
            burst_fps: 30,
            // macOS 14+ can update the existing SCStream configuration at
            // runtime; earlier supported systems remain fixed at 2 fps.
            capture_fps_ceiling: crate::screen_capture::capture_fps_ceiling(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct PerceptionController {
    controller_id: String,
    revision: u64,
    rates: PerceptionRates,
    state: PerceptionState,
    reason: &'static str,
    active_until_ms: u64,
    // Explicit interaction signals (for example, a known scroll) are not
    // subject to the visual-motion suppression below.
    high_until_ms: u64,
    visual_high_until_ms: u64,
    visual_high_started_ms: Option<u64>,
    visual_high_suppressed_until_ms: u64,
    tracking_until_ms: u64,
    burst_until_ms: u64,
    burst_cooldown_until_ms: u64,
    stable_frames: u8,
    visual_stable_since_ms: Option<u64>,
    state_started_at_ms: Option<u64>,
    state_durations_ms: PerceptionStateDurations,
    event_wakeups: u64,
    pixel_change_wakeups: u64,
}

impl Default for PerceptionController {
    fn default() -> Self {
        Self::new(PerceptionRates::default())
    }
}

impl PerceptionController {
    pub fn new(rates: PerceptionRates) -> Self {
        Self {
            controller_id: format!("{:016x}", rand::thread_rng().gen::<u64>()),
            revision: 0,
            rates,
            state: PerceptionState::Idle,
            reason: "stable_scene",
            active_until_ms: 0,
            high_until_ms: 0,
            visual_high_until_ms: 0,
            visual_high_started_ms: None,
            visual_high_suppressed_until_ms: 0,
            tracking_until_ms: 0,
            burst_until_ms: 0,
            burst_cooldown_until_ms: 0,
            stable_frames: 0,
            visual_stable_since_ms: None,
            state_started_at_ms: None,
            state_durations_ms: PerceptionStateDurations::default(),
            event_wakeups: 0,
            pixel_change_wakeups: 0,
        }
    }

    pub fn signal(
        &mut self,
        name: &str,
        pixel_change_ratio: Option<f64>,
        now_ms: u64,
    ) -> PerceptionSnapshot {
        if name != "stable_frame" {
            self.visual_stable_since_ms = None;
        }
        match name {
            "pixel_change" | "scene_change" => {
                increment_wakeup_count(&mut self.pixel_change_wakeups);
            }
            "ax_change"
            | "scroll"
            | "action_click"
            | "action_trigger"
            | "expected_effect"
            | "grounded_track_active" => {
                increment_wakeup_count(&mut self.event_wakeups);
            }
            _ => {}
        }
        match name {
            "ax_change" => {
                self.active_until_ms = now_ms.saturating_add(AX_ACTIVE_MS);
                self.stable_frames = 0;
                self.reason = "accessibility_change";
            }
            "scroll" => {
                self.high_until_ms = now_ms.saturating_add(HIGH_HOLD_MS);
                self.stable_frames = 0;
                self.reason = "scroll";
            }
            "pixel_change" => {
                let ratio = pixel_change_ratio.unwrap_or(0.0);
                if ratio >= SIGNIFICANT_PIXEL_RATIO {
                    self.signal_visual_high(now_ms, "significant_pixel_motion");
                } else if ratio > 0.0 {
                    self.active_until_ms = now_ms.saturating_add(AX_ACTIVE_MS);
                    self.stable_frames = 0;
                    if now_ms >= self.high_until_ms && now_ms >= self.visual_high_until_ms {
                        self.reason = "pixel_activity";
                    }
                }
            }
            "scene_change" => {
                self.signal_visual_high(now_ms, "scene_change");
            }
            "action_click" | "action_trigger" => {
                if now_ms >= self.burst_cooldown_until_ms {
                    self.burst_until_ms = now_ms.saturating_add(BURST_MS);
                    self.burst_cooldown_until_ms = now_ms.saturating_add(BURST_COOLDOWN_MS);
                    self.reason = if name == "action_trigger" {
                        "action_trigger"
                    } else {
                        "action_click"
                    };
                }
                self.stable_frames = 0;
            }
            "expected_effect" => {
                self.burst_until_ms = 0;
                self.high_until_ms = 0;
                self.visual_high_until_ms = 0;
                self.visual_high_started_ms = None;
                self.visual_high_suppressed_until_ms = 0;
                self.active_until_ms = now_ms.saturating_add(AX_ACTIVE_MS);
                self.stable_frames = 0;
                self.reason = "expected_effect_observed";
            }
            "grounded_track_active" => {
                self.tracking_until_ms = now_ms.saturating_add(TRACKING_ACTIVE_LEASE_MS);
                self.stable_frames = 0;
                self.reason = "grounded_track_active";
            }
            "grounded_track_stopped" => {
                self.tracking_until_ms = 0;
                self.stable_frames = 0;
                if self.reason == "grounded_track_active" {
                    self.reason = "grounded_track_stopped";
                }
            }
            "stable_frame" => {
                let stable_since = *self.visual_stable_since_ms.get_or_insert(now_ms);
                self.stable_frames = self.stable_frames.saturating_add(1);
                if self.stable_frames >= STABLE_FRAMES_TO_IDLE {
                    self.active_until_ms = 0;
                    self.high_until_ms = 0;
                    self.visual_high_until_ms = 0;
                    self.reason = "stable_scene";
                }
                if now_ms.saturating_sub(stable_since) >= VISUAL_STABLE_RESET_MS {
                    self.visual_high_started_ms = None;
                    self.visual_high_suppressed_until_ms = 0;
                }
            }
            // Small changes and unknown signals must not prolong HIGH/BURST.
            "minor_motion" => {}
            _ => self.reason = "ignored_unknown_signal",
        }
        self.refresh(now_ms)
    }

    fn signal_visual_high(&mut self, now_ms: u64, reason: &'static str) {
        self.stable_frames = 0;
        if now_ms < self.visual_high_suppressed_until_ms {
            self.active_until_ms = now_ms.saturating_add(AX_ACTIVE_MS);
            self.reason = "pixel_activity";
            return;
        }

        let started_ms = *self.visual_high_started_ms.get_or_insert(now_ms);
        let max_until_ms = started_ms.saturating_add(VISUAL_HIGH_MAX_MS);
        if now_ms >= max_until_ms {
            self.visual_high_until_ms = 0;
            self.visual_high_started_ms = None;
            self.visual_high_suppressed_until_ms = now_ms.saturating_add(VISUAL_HIGH_COOLDOWN_MS);
            self.active_until_ms = now_ms.saturating_add(AX_ACTIVE_MS);
            self.reason = "pixel_activity";
            return;
        }

        self.visual_high_until_ms = now_ms.saturating_add(HIGH_HOLD_MS).min(max_until_ms);
        self.reason = reason;
    }

    pub fn snapshot(&mut self, now_ms: u64) -> PerceptionSnapshot {
        self.refresh(now_ms)
    }

    fn refresh(&mut self, now_ms: u64) -> PerceptionSnapshot {
        let previous = (self.state, self.reason);
        self.state = if now_ms < self.burst_until_ms {
            PerceptionState::Burst
        } else if now_ms < self.high_until_ms
            || now_ms < self.visual_high_until_ms
            || now_ms < self.tracking_until_ms
        {
            if now_ms < self.tracking_until_ms {
                self.reason = "grounded_track_active";
            }
            PerceptionState::High
        } else if now_ms < self.active_until_ms {
            if self.reason == "significant_pixel_motion" {
                self.reason = "pixel_activity";
            }
            PerceptionState::Active
        } else {
            self.reason = "stable_scene";
            PerceptionState::Idle
        };
        if previous != (self.state, self.reason) {
            self.revision = self.revision.saturating_add(1);
        }
        match (self.state_started_at_ms, previous.0 == self.state) {
            (None, _) => self.state_started_at_ms = Some(now_ms),
            (Some(started_at), false) => {
                self.state_durations_ms
                    .add(previous.0, now_ms.saturating_sub(started_at));
                self.state_started_at_ms = Some(now_ms);
            }
            (Some(_), true) => {}
        }
        let mut state_durations_ms = self.state_durations_ms.clone();
        if let Some(started_at) = self.state_started_at_ms {
            state_durations_ms.add(self.state, now_ms.saturating_sub(started_at));
        }
        let (target_fps, compute_level_target) = match self.state {
            PerceptionState::Idle => (self.rates.idle_fps, 0),
            PerceptionState::Active => (self.rates.active_fps, 1),
            PerceptionState::High => (self.rates.high_fps, 2),
            PerceptionState::Burst => (self.rates.burst_fps, 3),
        };
        PerceptionSnapshot {
            controller_id: self.controller_id.clone(),
            revision: self.revision,
            state: self.state,
            reason: self.reason,
            target_fps,
            capture_fps_ceiling: self.rates.capture_fps_ceiling,
            achievable_fps: target_fps.min(self.rates.capture_fps_ceiling),
            compute_level_target,
            burst_available: now_ms >= self.burst_cooldown_until_ms,
            state_durations_ms,
            event_wakeups: self.event_wakeups,
            pixel_change_wakeups: self.pixel_change_wakeups,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshots_report_bounded_cumulative_time_in_each_policy_state() {
        let mut controller = PerceptionController::default();
        let idle = controller.snapshot(0);
        assert_eq!(idle.state_durations_ms.idle_ms, 0);

        let active = controller.signal("ax_change", None, 100);
        assert_eq!(active.state_durations_ms.idle_ms, 100);
        assert_eq!(active.state_durations_ms.active_ms, 0);

        let active_sample = controller.snapshot(600);
        assert_eq!(active_sample.state_durations_ms.idle_ms, 100);
        assert_eq!(active_sample.state_durations_ms.active_ms, 500);

        let high = controller.signal("scroll", None, 900);
        assert_eq!(high.state_durations_ms.active_ms, 800);
        assert_eq!(high.state_durations_ms.high_ms, 0);

        let high_sample = controller.snapshot(1_900);
        assert_eq!(high_sample.state_durations_ms.high_ms, 1_000);
    }

    #[test]
    fn snapshots_count_known_event_and_pixel_wakeups_separately() {
        let mut controller = PerceptionController::default();
        let first = controller.signal("ax_change", None, 10);
        assert_eq!(first.event_wakeups, 1);
        assert_eq!(first.pixel_change_wakeups, 0);

        let pixel = controller.signal("pixel_change", Some(0.03), 20);
        assert_eq!(pixel.event_wakeups, 1);
        assert_eq!(pixel.pixel_change_wakeups, 1);

        let scene = controller.signal("scene_change", None, 30);
        assert_eq!(scene.event_wakeups, 1);
        assert_eq!(scene.pixel_change_wakeups, 2);

        let ignored = controller.signal("stable_frame", None, 40);
        assert_eq!(ignored.event_wakeups, 1);
        assert_eq!(ignored.pixel_change_wakeups, 2);
    }

    #[test]
    fn wakeup_counts_saturate_at_the_advisory_api_integer_limit() {
        let mut controller = PerceptionController::default();
        controller.event_wakeups = MAX_ADVISORY_WAKEUPS;
        controller.pixel_change_wakeups = MAX_ADVISORY_WAKEUPS;

        let snapshot = controller.signal("ax_change", None, 10);
        assert_eq!(snapshot.event_wakeups, MAX_ADVISORY_WAKEUPS);

        let snapshot = controller.signal("pixel_change", Some(0.03), 20);
        assert_eq!(snapshot.pixel_change_wakeups, MAX_ADVISORY_WAKEUPS);
    }

    #[test]
    fn event_signals_escalate_and_respect_hold_times() {
        let mut controller = PerceptionController::default();
        let active = controller.signal("ax_change", None, 100);
        assert_eq!(active.state, PerceptionState::Active);
        assert_eq!(active.target_fps, 8);
        assert_eq!(
            active.capture_fps_ceiling,
            crate::screen_capture::capture_fps_ceiling()
        );
        assert_eq!(active.achievable_fps, 8.min(active.capture_fps_ceiling));

        let high = controller.signal("scroll", None, 200);
        assert_eq!(high.state, PerceptionState::High);
        assert_eq!(high.target_fps, 20);
        assert_eq!(high.achievable_fps, 20.min(high.capture_fps_ceiling));
        assert_eq!(controller.snapshot(2_699).state, PerceptionState::High);
        assert_eq!(controller.snapshot(2_700).state, PerceptionState::Active);
        assert_eq!(controller.snapshot(3_100).state, PerceptionState::Idle);
    }

    #[test]
    fn scene_change_escalates_high_and_stays_separate_from_pixel_thresholds() {
        let mut controller = PerceptionController::default();
        let snapshot = controller.signal("scene_change", None, 100);

        assert_eq!(snapshot.state, PerceptionState::High);
        assert_eq!(snapshot.reason, "scene_change");
        assert_eq!(snapshot.compute_level_target, 2);
        assert_eq!(
            snapshot.achievable_fps,
            20.min(snapshot.capture_fps_ceiling)
        );
    }

    #[test]
    fn small_pixel_change_enters_active_without_extending_high() {
        let mut controller = PerceptionController::default();
        let active = controller.signal("pixel_change", Some(0.001), 100);
        assert_eq!(active.state, PerceptionState::Active);
        assert_eq!(active.compute_level_target, 1);
        assert_eq!(active.reason, "pixel_activity");

        let high = controller.signal("pixel_change", Some(0.03), 200);
        assert_eq!(high.state, PerceptionState::High);
        let minor = controller.signal("pixel_change", Some(0.001), 500);
        assert_eq!(minor.state, PerceptionState::High);
        assert_eq!(minor.reason, "significant_pixel_motion");

        let after_high_hold = controller.snapshot(2_800);
        assert_eq!(after_high_hold.state, PerceptionState::Active);
        assert_eq!(after_high_hold.compute_level_target, 1);
        assert_eq!(after_high_hold.reason, "pixel_activity");
    }

    #[test]
    fn minor_animation_does_not_keep_high_state_alive() {
        let mut controller = PerceptionController::default();
        assert_eq!(
            controller.signal("pixel_change", Some(0.05), 10).state,
            PerceptionState::High
        );
        assert_eq!(
            controller.signal("pixel_change", Some(0.01), 2_000).state,
            PerceptionState::High
        );
        assert_eq!(
            controller.signal("minor_motion", Some(0.01), 2_500).state,
            PerceptionState::High
        );
        let after_high_hold = controller.snapshot(2_510);
        assert_eq!(after_high_hold.state, PerceptionState::Active);
        assert_eq!(after_high_hold.reason, "pixel_activity");
        assert_eq!(controller.snapshot(5_001).state, PerceptionState::Idle);
    }

    #[test]
    fn repeated_visual_motion_is_bounded_but_explicit_scroll_still_enters_high() {
        let mut controller = PerceptionController::default();
        assert_eq!(
            controller.signal("scene_change", None, 0).state,
            PerceptionState::High
        );

        // Large changes arrive every 500 ms, as they could during an
        // animation. They may extend the initial HIGH window, but only to
        // the five-second visual-motion cap.
        for now_ms in (500..5_000).step_by(500) {
            assert_eq!(
                controller.signal("pixel_change", Some(0.05), now_ms).state,
                PerceptionState::High,
                "visual motion should remain HIGH only within its bounded window at {now_ms}ms"
            );
        }
        assert_eq!(
            controller.signal("scene_change", None, 5_000).state,
            PerceptionState::Active
        );
        for now_ms in (5_500..10_000).step_by(500) {
            assert_eq!(
                controller.signal("scene_change", None, now_ms).state,
                PerceptionState::Active,
                "repeated visual motion must not re-enter HIGH during cooldown at {now_ms}ms"
            );
        }

        // A known user/action scroll is an explicit activity signal and must
        // still request HIGH while pixel-only motion is suppressed.
        let scroll = controller.signal("scroll", None, 7_500);
        assert_eq!(scroll.state, PerceptionState::High);
        assert_eq!(scroll.reason, "scroll");
        assert_eq!(scroll.target_fps, 20);
    }

    #[test]
    fn stable_frames_settle_state_without_bypassing_visual_high_cooldown() {
        let mut controller = PerceptionController::default();
        controller.signal("scene_change", None, 0);
        let capped = controller.signal("scene_change", None, VISUAL_HIGH_MAX_MS);
        assert_eq!(capped.state, PerceptionState::Active);

        controller.signal("stable_frame", None, 5_100);
        controller.signal("stable_frame", None, 5_200);
        assert_eq!(
            controller.signal("stable_frame", None, 5_300).state,
            PerceptionState::Idle
        );

        let next_transition = controller.signal("scene_change", None, 5_400);
        assert_eq!(next_transition.state, PerceptionState::Active);
        assert_eq!(next_transition.reason, "pixel_activity");

        let after_cooldown = controller.signal("scene_change", None, 10_000);
        assert_eq!(after_cooldown.state, PerceptionState::High);
        assert_eq!(after_cooldown.reason, "scene_change");
    }

    #[test]
    fn sustained_visual_stability_allows_a_new_transition_before_cooldown_expires() {
        let mut controller = PerceptionController::default();
        controller.signal("scene_change", None, 0);
        controller.signal("scene_change", None, VISUAL_HIGH_MAX_MS);

        let early_transition = controller.signal("scene_change", None, 6_000);
        assert_eq!(early_transition.state, PerceptionState::Active);

        controller.signal("stable_frame", None, 6_100);
        controller.signal("stable_frame", None, 7_599);
        let reset = controller.signal("stable_frame", None, 7_600);
        assert_eq!(reset.state, PerceptionState::Idle);

        let next_transition = controller.signal("scene_change", None, 7_700);
        assert_eq!(next_transition.state, PerceptionState::High);
        assert_eq!(next_transition.reason, "scene_change");
    }

    #[test]
    fn grounded_tracking_holds_l2_and_expires_or_stops_cleanly() {
        let mut controller = PerceptionController::default();
        assert!(supports_signal("grounded_track_active"));
        assert!(supports_signal("grounded_track_stopped"));

        let started = controller.signal("grounded_track_active", None, 100);
        assert_eq!(started.state, PerceptionState::High);
        assert_eq!(started.reason, "grounded_track_active");
        assert_eq!(started.target_fps, 20);
        assert_eq!(started.compute_level_target, 2);

        let heartbeat = controller.signal("grounded_track_active", None, 4_000);
        assert_eq!(heartbeat.state, PerceptionState::High);
        assert_eq!(controller.snapshot(8_999).state, PerceptionState::High);
        assert_eq!(controller.snapshot(9_001).state, PerceptionState::Idle);

        controller.signal("grounded_track_active", None, 10_000);
        let stopped = controller.signal("grounded_track_stopped", None, 10_100);
        assert_eq!(stopped.state, PerceptionState::Idle);
        assert_eq!(stopped.compute_level_target, 0);
    }

    #[test]
    fn click_burst_is_short_and_rate_limited() {
        let mut controller = PerceptionController::default();
        let burst = controller.signal("action_click", None, 1_000);
        assert_eq!(burst.state, PerceptionState::Burst);
        assert_eq!(burst.target_fps, 30);
        assert_eq!(burst.achievable_fps, 30.min(burst.capture_fps_ceiling));
        assert!(!burst.burst_available);

        assert_eq!(
            controller.signal("action_click", None, 2_100).state,
            PerceptionState::Idle
        );
        assert_eq!(controller.snapshot(9_000).burst_available, true);
    }

    #[test]
    fn stable_frames_do_not_abort_click_burst() {
        let mut controller = PerceptionController::default();
        controller.signal("action_click", None, 1_000);

        for now_ms in [1_010, 1_020, 1_030] {
            assert_eq!(
                controller.signal("stable_frame", None, now_ms).state,
                PerceptionState::Burst
            );
        }
        assert_eq!(controller.snapshot(1_999).state, PerceptionState::Burst);
        assert_eq!(controller.snapshot(2_000).state, PerceptionState::Idle);
    }

    #[test]
    fn action_trigger_enters_burst_and_verified_effect_settles_to_active() {
        let mut controller = PerceptionController::default();
        let burst = controller.signal("action_trigger", None, 1_000);
        assert_eq!(burst.state, PerceptionState::Burst);
        assert_eq!(burst.reason, "action_trigger");
        let settled = controller.signal("expected_effect", None, 1_100);
        assert_eq!(settled.state, PerceptionState::Active);
        assert_eq!(settled.reason, "expected_effect_observed");
    }

    #[test]
    fn stable_frames_return_the_controller_to_idle_and_effect_settles_burst() {
        let mut controller = PerceptionController::default();
        controller.signal("action_click", None, 0);
        let settled = controller.signal("expected_effect", None, 100);
        assert_eq!(settled.state, PerceptionState::Active);
        assert_eq!(
            controller.signal("stable_frame", None, 200).state,
            PerceptionState::Active
        );
        controller.signal("stable_frame", None, 300);
        assert_eq!(
            controller.signal("stable_frame", None, 400).state,
            PerceptionState::Idle
        );
    }
}
