//! Bounded Apple Vision tracking for a caller-supplied, grounded screen region.
//!
//! This is a frame-pair primitive, not an autonomous detector: callers must
//! provide the initial top-left normalized box from trusted AX/UI-Venus data.

use serde::Serialize;
#[cfg(target_os = "macos")]
use std::time::Instant;

const MAX_IMAGE_BYTES: usize = 5_000_000;
const MAX_IMAGE_DIMENSION: u32 = 8_192;
const MAX_IMAGE_PIXELS: u64 = 50_000_000;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VisualTrackObservation {
    pub source: &'static str,
    /// Normalized [x1, y1, x2, y2], image origin at top-left.
    pub bbox_normalized: [f32; 4],
    pub confidence: f32,
    pub processing_ms: f64,
}

/// Report the tracker selected by this binary's compile-time feature set.
pub const fn tracking_backend_name() -> &'static str {
    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    {
        "OPENCV_LUCAS_KANADE"
    }
    #[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
    {
        "APPLE_VISION_OBJECT_TRACKING"
    }
    #[cfg(not(target_os = "macos"))]
    {
        "UNAVAILABLE"
    }
}

fn image_dimensions(bytes: &[u8]) -> Result<(u32, u32), String> {
    use image::{ImageFormat, ImageReader};
    use std::io::Cursor;

    if bytes.is_empty() || bytes.len() > MAX_IMAGE_BYTES {
        return Err("VISION_TRACK_IMAGE_TOO_LARGE".into());
    }
    let reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| "VISION_TRACK_IMAGE_DECODE_FAILED")?;
    if !matches!(reader.format(), Some(ImageFormat::Png | ImageFormat::Jpeg)) {
        return Err("VISION_TRACK_IMAGE_TYPE_UNSUPPORTED".into());
    }
    let (width, height) = reader
        .into_dimensions()
        .map_err(|_| "VISION_TRACK_IMAGE_DECODE_FAILED")?;
    if width == 0
        || height == 0
        || width > MAX_IMAGE_DIMENSION
        || height > MAX_IMAGE_DIMENSION
        || u64::from(width) * u64::from(height) > MAX_IMAGE_PIXELS
    {
        return Err("VISION_TRACK_IMAGE_DIMENSIONS_UNSUPPORTED".into());
    }
    Ok((width, height))
}

pub fn track_object_between_images(
    first_image: &[u8],
    next_image: &[u8],
    seed_bbox_normalized: [f32; 4],
) -> Result<VisualTrackObservation, String> {
    #[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
    use objc2::AnyThread;
    #[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
    use objc2_foundation::{NSArray, NSData};
    #[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
    use objc2_vision::{
        VNDetectedObjectObservation, VNObservation, VNRequest, VNSequenceRequestHandler,
        VNTrackObjectRequest,
    };

    let first_dimensions = image_dimensions(first_image)?;
    if image_dimensions(next_image)? != first_dimensions {
        return Err("VISION_TRACK_FRAME_DIMENSIONS_MISMATCH".into());
    }
    let [x1, y1, x2, y2] = seed_bbox_normalized;
    if !seed_bbox_normalized.iter().all(|value| value.is_finite())
        || x1 < 0.0
        || y1 < 0.0
        || x1 >= x2
        || y1 >= y2
        || x2 > 1.0
        || y2 > 1.0
    {
        return Err("VISION_TRACK_SEED_BOUNDS_INVALID".into());
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    {
        return track_object_with_opencv(first_image, next_image, seed_bbox_normalized);
    }
    #[cfg(all(target_os = "macos", not(feature = "opencv-perception")))]
    {
        use objc2_foundation::{NSPoint, NSRect, NSSize};

        let processing_started = Instant::now();
        let seed_box = NSRect::new(
            NSPoint::new(x1 as f64, (1.0 - y2) as f64),
            NSSize::new((x2 - x1) as f64, (y2 - y1) as f64),
        );
        let seed = unsafe { VNDetectedObjectObservation::observationWithBoundingBox(seed_box) };
        let request = unsafe {
            VNTrackObjectRequest::initWithDetectedObjectObservation(
                VNTrackObjectRequest::alloc(),
                &seed,
            )
        };
        let request_as_base: objc2::rc::Retained<VNRequest> =
            request.clone().into_super().into_super().into_super();
        let requests = NSArray::from_retained_slice(&[request_as_base]);
        // SAFETY: Vision objects are created and used synchronously on this worker.
        let handler = unsafe { VNSequenceRequestHandler::new() };
        let first = NSData::with_bytes(first_image);
        let next = NSData::with_bytes(next_image);

        // Seed the sequence on the frame whose coordinates produced the AX or
        // UI-Venus box. A sequence handler cannot infer motion if its first
        // input is already the following frame.
        // SAFETY: `requests` contains a VNTrackObjectRequest and `first` is a
        // validated PNG/JPEG frame whose dimensions match the seed box.
        unsafe { handler.performRequests_onImageData_error(&requests, &first) }
            .map_err(|_| "VISION_TRACK_FIRST_FRAME_FAILED")?;
        let first_result = unsafe { request.results() }
            .and_then(|results| results.iter().next())
            .and_then(|result: objc2::rc::Retained<VNObservation>| {
                result.downcast::<VNDetectedObjectObservation>().ok()
            })
            .ok_or("VISION_TRACKING_NO_FIRST_FRAME_RESULT")?;

        // Apple Vision continues the same tracker when its returned
        // observation is supplied as inputObservation for the next frame.
        // SAFETY: `first_result` is a VNDetectedObjectObservation returned by
        // this request's immediately preceding sequence frame.
        unsafe { request.setInputObservation(&first_result) };
        // SAFETY: `requests` still contains the same tracker and `next` has
        // matching validated dimensions; calls remain serialized on worker.
        unsafe { handler.performRequests_onImageData_error(&requests, &next) }
            .map_err(|_| "VISION_TRACK_NEXT_FRAME_FAILED")?;

        let observation = unsafe { request.results() }
            .and_then(|results| results.iter().next())
            .and_then(|result: objc2::rc::Retained<VNObservation>| {
                result.downcast::<VNDetectedObjectObservation>().ok()
            })
            .ok_or("VISION_TRACKING_NO_NEXT_FRAME_RESULT")?;
        let rect = unsafe { observation.boundingBox() };
        let left = rect.origin.x.clamp(0.0, 1.0) as f32;
        let top = (1.0 - rect.origin.y - rect.size.height).clamp(0.0, 1.0) as f32;
        let right = (rect.origin.x + rect.size.width).clamp(0.0, 1.0) as f32;
        let bottom = (1.0 - rect.origin.y).clamp(0.0, 1.0) as f32;
        if right <= left || bottom <= top {
            return Err("VISION_TRACKING_RESULT_BOUNDS_INVALID".into());
        }

        Ok(VisualTrackObservation {
            source: "APPLE_VISION_OBJECT_TRACKING",
            bbox_normalized: [left, top, right, bottom],
            confidence: unsafe { observation.confidence() }.clamp(0.0, 1.0),
            processing_ms: processing_started.elapsed().as_secs_f64() * 1_000.0,
        })
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (first_image, next_image, first_dimensions);
        Err("VISION_TRACKING_UNSUPPORTED_PLATFORM".into())
    }
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
pub struct OpenCvFlowTracker {
    previous_gray: image::GrayImage,
    bbox_normalized: [f32; 4],
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
impl OpenCvFlowTracker {
    pub fn new(rgb: &[u8], width: u32, height: u32, bbox: [f32; 4]) -> Result<Self, String> {
        validate_normalized_box(bbox)?;
        Ok(Self {
            previous_gray: gray_image_from_rgb(rgb, width, height)?,
            bbox_normalized: bbox,
        })
    }

    pub fn update(
        &mut self,
        rgb: &[u8],
        width: u32,
        height: u32,
    ) -> Result<VisualTrackObservation, String> {
        if self.previous_gray.dimensions() != (width, height) {
            return Err("OPENCV_TRACK_FRAME_DIMENSIONS_MISMATCH".into());
        }
        let started = Instant::now();
        let next_gray = gray_image_from_rgb(rgb, width, height)?;
        let (bbox, confidence) =
            track_gray_pair(&self.previous_gray, &next_gray, self.bbox_normalized)?;
        self.previous_gray = next_gray;
        self.bbox_normalized = bbox;
        Ok(VisualTrackObservation {
            source: "OPENCV_LUCAS_KANADE",
            bbox_normalized: bbox,
            confidence,
            processing_ms: started.elapsed().as_secs_f64() * 1_000.0,
        })
    }

    pub fn bbox_normalized(&self) -> [f32; 4] {
        self.bbox_normalized
    }
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn track_object_with_opencv(
    first_image: &[u8],
    next_image: &[u8],
    seed_bbox_normalized: [f32; 4],
) -> Result<VisualTrackObservation, String> {
    use std::time::Instant;
    validate_normalized_box(seed_bbox_normalized)?;
    let started = Instant::now();
    let first = image::load_from_memory(first_image)
        .map_err(|_| "OPENCV_TRACK_IMAGE_DECODE_FAILED")?
        .to_rgb8();
    let next = image::load_from_memory(next_image)
        .map_err(|_| "OPENCV_TRACK_IMAGE_DECODE_FAILED")?
        .to_rgb8();
    if first.dimensions() != next.dimensions() {
        return Err("VISION_TRACK_FRAME_DIMENSIONS_MISMATCH".into());
    }
    let first_gray = gray_image_from_rgb(first.as_raw(), first.width(), first.height())?;
    let next_gray = gray_image_from_rgb(next.as_raw(), next.width(), next.height())?;
    let (bbox, confidence) = track_gray_pair(&first_gray, &next_gray, seed_bbox_normalized)?;
    Ok(VisualTrackObservation {
        source: "OPENCV_LUCAS_KANADE",
        bbox_normalized: bbox,
        confidence,
        processing_ms: started.elapsed().as_secs_f64() * 1_000.0,
    })
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn validate_normalized_box(bbox: [f32; 4]) -> Result<(), String> {
    let [left, top, right, bottom] = bbox;
    if !bbox.iter().all(|value| value.is_finite())
        || left < 0.0
        || top < 0.0
        || left >= right
        || top >= bottom
        || right > 1.0
        || bottom > 1.0
    {
        return Err("VISION_TRACK_SEED_BOUNDS_INVALID".into());
    }
    Ok(())
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn gray_image_from_rgb(rgb: &[u8], width: u32, height: u32) -> Result<image::GrayImage, String> {
    use opencv::{
        core::{self, Mat, MatTraitConst, MatTraitConstManual},
        imgproc,
    };
    if width == 0 || height == 0 || rgb.len() != width as usize * height as usize * 3 {
        return Err("OPENCV_TRACK_FRAME_INVALID".into());
    }
    let data = Mat::from_slice(rgb).map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let rgb_mat = data
        .reshape(3, height as i32)
        .map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let mut gray = Mat::default();
    imgproc::cvt_color(
        &rgb_mat,
        &mut gray,
        imgproc::COLOR_RGB2GRAY,
        0,
        core::AlgorithmHint::ALGO_HINT_DEFAULT,
    )
    .map_err(|_| "OPENCV_TRACK_GRAYSCALE_FAILED")?;
    image::GrayImage::from_raw(
        width,
        height,
        gray.data_bytes()
            .map_err(|_| "OPENCV_TRACK_GRAY_READ_FAILED")?
            .to_vec(),
    )
    .ok_or_else(|| "OPENCV_TRACK_GRAY_DIMENSIONS_INVALID".into())
}

#[cfg(all(target_os = "macos", feature = "opencv-perception"))]
fn track_gray_pair(
    first_gray: &image::GrayImage,
    next_gray: &image::GrayImage,
    seed_bbox_normalized: [f32; 4],
) -> Result<([f32; 4], f32), String> {
    use opencv::{
        core::{self, Mat, MatTraitConst, Point2f, Size, TermCriteria, Vector},
        features, video,
    };
    validate_normalized_box(seed_bbox_normalized)?;
    if first_gray.dimensions() != next_gray.dimensions() {
        return Err("OPENCV_TRACK_FRAME_DIMENSIONS_MISMATCH".into());
    }

    let [left, top, right, bottom] = seed_bbox_normalized;
    let width = first_gray.width() as i32;
    let height = first_gray.height() as i32;
    let first_data =
        Mat::from_slice(first_gray.as_raw()).map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let first_gray_mat = first_data
        .reshape(1, height)
        .map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let next_data =
        Mat::from_slice(next_gray.as_raw()).map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let next_gray_mat = next_data
        .reshape(1, height)
        .map_err(|_| "OPENCV_TRACK_MATRIX_FAILED")?;
    let x = (left * width as f32).floor().max(0.0) as i32;
    let y = (top * height as f32).floor().max(0.0) as i32;
    let x2 = (right * width as f32).ceil().min(width as f32) as i32;
    let y2 = (bottom * height as f32).ceil().min(height as f32) as i32;
    let seed_rect = core::Rect::new(x, y, x2 - x, y2 - y);
    if seed_rect.width < 8 || seed_rect.height < 8 {
        return Err("OPENCV_TRACK_SEED_TOO_SMALL".into());
    }
    let seed_roi =
        Mat::roi(&first_gray_mat, seed_rect).map_err(|_| "OPENCV_TRACK_SEED_ROI_FAILED")?;
    let mut local_points = Vector::<Point2f>::new();
    features::good_features_to_track(
        &seed_roi,
        &mut local_points,
        100,
        0.01,
        4.0,
        &core::no_array(),
        3,
        false,
        0.04,
    )
    .map_err(|_| "OPENCV_TRACK_FEATURES_FAILED")?;
    if local_points.len() < 6 {
        return Err("OPENCV_TRACK_FEATURES_INSUFFICIENT".into());
    }
    let mut first_points = Vector::<Point2f>::new();
    for point in local_points {
        first_points.push(Point2f::new(point.x + x as f32, point.y + y as f32));
    }
    let mut next_points = Vector::<Point2f>::new();
    let mut statuses = Vector::<u8>::new();
    let mut errors = Vector::<f32>::new();
    let criteria = TermCriteria::new(
        core::TermCriteria_Type::COUNT as i32 | core::TermCriteria_Type::EPS as i32,
        30,
        0.01,
    )
    .map_err(|_| "OPENCV_TRACK_CRITERIA_FAILED")?;
    video::calc_optical_flow_pyr_lk(
        &first_gray_mat,
        &next_gray_mat,
        &first_points,
        &mut next_points,
        &mut statuses,
        &mut errors,
        Size::new(21, 21),
        3,
        criteria,
        0,
        0.0001,
    )
    .map_err(|_| "OPENCV_TRACK_FLOW_FAILED")?;
    let mut dx = Vec::new();
    let mut dy = Vec::new();
    let mut flow_errors = Vec::new();
    for index in 0..first_points.len() {
        if statuses.get(index).unwrap_or(0) == 0 {
            continue;
        }
        let first = first_points
            .get(index)
            .map_err(|_| "OPENCV_TRACK_RESULT_INVALID")?;
        let next = next_points
            .get(index)
            .map_err(|_| "OPENCV_TRACK_RESULT_INVALID")?;
        if next.x.is_finite() && next.y.is_finite() {
            dx.push(next.x - first.x);
            dy.push(next.y - first.y);
            let error = errors
                .get(index)
                .map_err(|_| "OPENCV_TRACK_RESULT_INVALID")?;
            if error.is_finite() {
                flow_errors.push(error);
            }
        }
    }
    let tracked_fraction = dx.len() as f32 / first_points.len() as f32;
    if dx.len() < 6 || tracked_fraction < 0.5 {
        return Err("OPENCV_TRACK_FEATURES_LOST".into());
    }
    if flow_errors.len() < 6 {
        return Err("OPENCV_TRACK_PHOTOMETRIC_EVIDENCE_INSUFFICIENT".into());
    }
    flow_errors.sort_by(f32::total_cmp);
    let median_flow_error = if flow_errors.len() % 2 == 0 {
        (flow_errors[flow_errors.len() / 2 - 1] + flow_errors[flow_errors.len() / 2]) / 2.0
    } else {
        flow_errors[flow_errors.len() / 2]
    };
    // OpenCV's default LK error is the average patch L1 difference. Real
    // ScreenCaptureKit frames include resampling/compression and moving glyph
    // edges, so allow a wider photometric range than the in-memory fixture;
    // fully occluded targets remain well above 80/255 and are still rejected.
    if median_flow_error > 64.0 {
        eprintln!(
            "OPENCV_TRACK_REJECT=PHOTOMETRIC_ERROR_HIGH MEDIAN_FLOW_ERROR={median_flow_error:.2} TRACKED_FRACTION={tracked_fraction:.3}"
        );
        return Err("OPENCV_TRACK_PHOTOMETRIC_ERROR_HIGH".into());
    }
    dx.sort_by(f32::total_cmp);
    dy.sort_by(f32::total_cmp);
    let median = |values: &[f32]| {
        if values.len() % 2 == 0 {
            (values[values.len() / 2 - 1] + values[values.len() / 2]) / 2.0
        } else {
            values[values.len() / 2]
        }
    };
    let shift_x = median(&dx) / width as f32;
    let shift_y = median(&dy) / height as f32;
    let width_norm = right - left;
    let height_norm = bottom - top;
    let next_left = left + shift_x;
    let next_top = top + shift_y;
    let next_right = next_left + width_norm;
    let next_bottom = next_top + height_norm;
    if next_left < 0.0 || next_top < 0.0 || next_right > 1.0 || next_bottom > 1.0 {
        return Err("OPENCV_TRACK_RESULT_OUT_OF_BOUNDS".into());
    }
    Ok((
        [next_left, next_top, next_right, next_bottom],
        tracked_fraction.clamp(0.0, 1.0),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tracking_backend_name_reports_the_compiled_implementation() {
        let expected = if cfg!(all(target_os = "macos", feature = "opencv-perception")) {
            "OPENCV_LUCAS_KANADE"
        } else if cfg!(target_os = "macos") {
            "APPLE_VISION_OBJECT_TRACKING"
        } else {
            "UNAVAILABLE"
        };
        assert_eq!(tracking_backend_name(), expected);
    }

    fn test_frame(left: u32, top: u32) -> Vec<u8> {
        use image::{ImageBuffer, ImageEncoder, Rgb};

        let width = 320;
        let height = 200;
        let mut frame = ImageBuffer::from_fn(width, height, |x, y| {
            let value = ((x * 17 + y * 31 + (x ^ y) * 7) % 80 + 80) as u8;
            Rgb([value, value, value])
        });
        for y in 0..40 {
            for x in 0..80 {
                let checker = ((x / 4 + y / 4) % 2 == 0) as u8;
                let border = x < 3 || y < 3 || x >= 77 || y >= 37;
                let value = if border {
                    245
                } else if checker == 1 {
                    30 + ((x * 11 + y * 3) % 40) as u8
                } else {
                    190 + ((x * 5 + y * 13) % 50) as u8
                };
                frame.put_pixel(left + x, top + y, Rgb([value, value, value]));
            }
        }
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(&frame, width, height, image::ExtendedColorType::Rgb8)
            .unwrap();
        bytes
    }

    fn different_sized_frame() -> Vec<u8> {
        use image::{ImageBuffer, ImageEncoder, Rgb};

        let frame = ImageBuffer::from_pixel(160, 100, Rgb([96, 96, 96]));
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(&frame, 160, 100, image::ExtendedColorType::Rgb8)
            .unwrap();
        bytes
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    fn occluded_frame(left: u32, top: u32, visible_width: u32) -> Vec<u8> {
        use image::{ImageEncoder, Rgb};

        let mut frame = image::load_from_memory(&test_frame(left, top))
            .unwrap()
            .to_rgb8();
        for y in top..top + 40 {
            for x in left + visible_width..left + 80 {
                frame.put_pixel(x, y, Rgb([128, 128, 128]));
            }
        }
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(
                frame.as_raw(),
                frame.width(),
                frame.height(),
                image::ExtendedColorType::Rgb8,
            )
            .unwrap();
        bytes
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    fn brightness_shifted_frame(left: u32, top: u32, delta: u8) -> Vec<u8> {
        use image::ImageEncoder;

        let mut frame = image::load_from_memory(&test_frame(left, top))
            .unwrap()
            .to_rgb8();
        for y in top..top + 40 {
            for x in left..left + 80 {
                let pixel = frame.get_pixel_mut(x, y);
                for channel in &mut pixel.0 {
                    *channel = channel.saturating_add(delta);
                }
            }
        }
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(
                frame.as_raw(),
                frame.width(),
                frame.height(),
                image::ExtendedColorType::Rgb8,
            )
            .unwrap();
        bytes
    }

    #[test]
    fn seeded_tracker_input_is_bounded_and_frame_pair_dimensions_must_match() {
        let first = test_frame(80, 60);
        let second = test_frame(100, 70);
        assert_eq!(image_dimensions(&first), Ok((320, 200)));
        #[cfg(not(target_os = "macos"))]
        assert_eq!(
            track_object_between_images(&first, &second, [0.25, 0.3, 0.5, 0.5]).unwrap_err(),
            "VISION_TRACKING_UNSUPPORTED_PLATFORM"
        );
        assert_eq!(
            track_object_between_images(&first, &second, [0.5, 0.5, 0.2, 0.8]).unwrap_err(),
            "VISION_TRACK_SEED_BOUNDS_INVALID"
        );
        assert_eq!(
            track_object_between_images(&first, &different_sized_frame(), [0.25, 0.3, 0.5, 0.5])
                .unwrap_err(),
            "VISION_TRACK_FRAME_DIMENSIONS_MISMATCH"
        );
        assert_eq!(
            track_object_between_images(&first, &test_frame(100, 70)[..20], [0.25, 0.3, 0.5, 0.5])
                .unwrap_err(),
            "VISION_TRACK_IMAGE_DECODE_FAILED"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn selected_tracker_tracks_a_seeded_gui_region_across_two_frames() {
        let first = test_frame(80, 60);
        let next = test_frame(100, 70);
        let warmup = track_object_between_images(&first, &next, [0.25, 0.3, 0.5, 0.5])
            .expect("selected tracker should return the moved test region");
        assert_eq!(
            warmup.source,
            if cfg!(feature = "opencv-perception") {
                "OPENCV_LUCAS_KANADE"
            } else {
                "APPLE_VISION_OBJECT_TRACKING"
            }
        );
        let mut last_observation = warmup;
        let mut samples = Vec::with_capacity(20);
        for _ in 0..20 {
            let observation = track_object_between_images(&first, &next, [0.25, 0.3, 0.5, 0.5])
                .expect("selected tracker should return the moved test region");
            let [left, top, right, bottom] = observation.bbox_normalized;
            let expected = [0.3125, 0.35, 0.5625, 0.55];
            assert!(observation.confidence >= 0.5, "observation={observation:?}");
            assert!(
                (left - expected[0]).abs() < 0.02,
                "observation={observation:?}"
            );
            assert!(
                (top - expected[1]).abs() < 0.02,
                "observation={observation:?}"
            );
            assert!(
                (right - expected[2]).abs() < 0.02,
                "observation={observation:?}"
            );
            assert!(
                (bottom - expected[3]).abs() < 0.02,
                "observation={observation:?}"
            );
            let intersection_width = (right.min(expected[2]) - left.max(expected[0])).max(0.0);
            let intersection_height = (bottom.min(expected[3]) - top.max(expected[1])).max(0.0);
            let intersection = intersection_width * intersection_height;
            let predicted_area = (right - left) * (bottom - top);
            let expected_area = (expected[2] - expected[0]) * (expected[3] - expected[1]);
            let iou = intersection / (predicted_area + expected_area - intersection);
            assert!(
                iou >= 0.85,
                "fixture IoU={iou:.3}, observation={observation:?}"
            );
            samples.push(observation.processing_ms);
            last_observation = observation;
        }
        samples.sort_by(f64::total_cmp);
        let percentile = |p: f64| {
            samples[((p * samples.len() as f64).ceil() as usize - 1).min(samples.len() - 1)]
        };
        println!(
            "{} synthetic 320x200 two-frame fixture (n={}): bbox={:?}, confidence={:.3}, API p50={:.2}ms p95={:.2}ms",
            last_observation.source,
            samples.len(),
            last_observation.bbox_normalized,
            last_observation.confidence,
            percentile(0.50),
            percentile(0.95)
        );
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_tracker_updates_a_grounded_box_across_sequential_frames_in_memory() {
        use std::time::Instant;

        let decode_rgb = |bytes: &[u8]| {
            let image = image::load_from_memory(bytes).unwrap().to_rgb8();
            (image.as_raw().clone(), image.width(), image.height())
        };
        let first = decode_rgb(&test_frame(80, 60));
        let mut tracker = OpenCvFlowTracker::new(&first.0, first.1, first.2, [0.25, 0.3, 0.5, 0.5])
            .expect("OpenCV tracker should seed the grounded fixture box");
        let mut samples = Vec::with_capacity(20);
        let mut last = None;
        for step in 1..=20 {
            let next = decode_rgb(&test_frame(80 + step * 2, 60 + step));
            let started = Instant::now();
            let observation = tracker
                .update(&next.0, next.1, next.2)
                .expect("in-memory frame should retain enough features");
            samples.push(started.elapsed().as_secs_f64() * 1_000.0);
            assert!(observation.confidence >= 0.5, "{observation:?}");
            last = Some(observation);
        }
        samples.sort_by(f64::total_cmp);
        let observation = last.expect("fixture has tracking steps");
        let expected = [0.375, 0.4, 0.625, 0.6];
        for (actual, expected) in observation.bbox_normalized.iter().zip(expected) {
            assert!(
                (actual - expected).abs() < 0.02,
                "bbox={:?}",
                observation.bbox_normalized
            );
        }
        let [left, top, right, bottom] = observation.bbox_normalized;
        let intersection_width = (right.min(expected[2]) - left.max(expected[0])).max(0.0);
        let intersection_height = (bottom.min(expected[3]) - top.max(expected[1])).max(0.0);
        let intersection = intersection_width * intersection_height;
        let iou = intersection
            / ((right - left) * (bottom - top)
                + (expected[2] - expected[0]) * (expected[3] - expected[1])
                - intersection);
        assert!(iou >= 0.85, "IoU={iou:.3}, observation={observation:?}");
        let percentile = |p: f64| {
            samples[((p * samples.len() as f64).ceil() as usize - 1).min(samples.len() - 1)]
        };
        println!(
            "OpenCV continuous synthetic 320x200 tracker (n={}): bbox={:?}, confidence={:.3}, p50={:.2}ms p95={:.2}ms, IoU={iou:.3}",
            samples.len(), observation.bbox_normalized, observation.confidence,
            percentile(0.50), percentile(0.95)
        );
        assert_eq!(tracker.bbox_normalized(), observation.bbox_normalized);
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_tracker_bounds_long_horizon_drift_across_a_hover_transition() {
        let seed = image::load_from_memory(&test_frame(80, 60))
            .unwrap()
            .to_rgb8();
        let mut tracker = OpenCvFlowTracker::new(
            seed.as_raw(),
            seed.width(),
            seed.height(),
            [0.25, 0.3, 0.5, 0.5],
        )
        .expect("test button should seed enough texture");

        let mut maximum_edge_error = 0.0f32;
        for step in 1..=45 {
            let left = 80 + step;
            let top = 60 + step;
            // Model a transient hover tint, then its removal, while the same
            // grounded control continues moving through the capture stream.
            let frame_bytes = if step == 23 {
                brightness_shifted_frame(left, top, 18)
            } else {
                test_frame(left, top)
            };
            let frame = image::load_from_memory(&frame_bytes).unwrap().to_rgb8();
            let observation = tracker
                .update(frame.as_raw(), frame.width(), frame.height())
                .unwrap_or_else(|error| panic!("track lost at frame {step}: {error}"));
            let expected = [
                left as f32 / 320.0,
                top as f32 / 200.0,
                (left + 80) as f32 / 320.0,
                (top + 40) as f32 / 200.0,
            ];
            let maximum_error = observation
                .bbox_normalized
                .iter()
                .zip(expected)
                .map(|(actual, expected)| (actual - expected).abs())
                .fold(0.0f32, f32::max);
            maximum_edge_error = maximum_edge_error.max(maximum_error);
            assert!(
                observation.confidence >= 0.5,
                "frame {step}: {observation:?}"
            );
            assert!(
                maximum_error < 0.025,
                "frame {step}: edge error {maximum_error:.4}, {observation:?}"
            );
        }

        assert!(maximum_edge_error < 0.025);
        let expected_final = [0.390625, 0.525, 0.640625, 0.725];
        for (actual, expected) in tracker.bbox_normalized().iter().zip(expected_final) {
            assert!(
                (actual - expected).abs() < 0.025,
                "{:?}",
                tracker.bbox_normalized()
            );
        }
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_tracker_fails_closed_when_grounded_control_is_fully_occluded() {
        let seed = test_frame(80, 60);
        let next = occluded_frame(80, 60, 0);
        let seed = image::load_from_memory(&seed).unwrap().to_rgb8();
        let next = image::load_from_memory(&next).unwrap().to_rgb8();
        let mut tracker = OpenCvFlowTracker::new(
            seed.as_raw(),
            seed.width(),
            seed.height(),
            [0.25, 0.3, 0.5, 0.5],
        )
        .expect("test button should seed enough texture");

        let result = tracker.update(next.as_raw(), next.width(), next.height());

        assert_eq!(result.unwrap_err(), "OPENCV_TRACK_PHOTOMETRIC_ERROR_HIGH");
        assert_eq!(
            tracker.bbox_normalized(),
            [0.25, 0.3, 0.5, 0.5],
            "a rejected frame must not advance the track state"
        );
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_tracker_keeps_a_grounded_control_through_a_bounded_brightness_change() {
        let seed = image::load_from_memory(&test_frame(80, 60))
            .unwrap()
            .to_rgb8();
        let next = image::load_from_memory(&brightness_shifted_frame(80, 60, 18))
            .unwrap()
            .to_rgb8();
        let mut tracker = OpenCvFlowTracker::new(
            seed.as_raw(),
            seed.width(),
            seed.height(),
            [0.25, 0.3, 0.5, 0.5],
        )
        .expect("test button should seed enough texture");

        let observation = tracker
            .update(next.as_raw(), next.width(), next.height())
            .expect("a bounded hover-like brightness shift should retain the track");

        assert!(observation.confidence >= 0.5, "{observation:?}");
        for (actual, expected) in observation
            .bbox_normalized
            .iter()
            .zip([0.25, 0.3, 0.5, 0.5])
        {
            assert!((actual - expected).abs() < 0.02, "{observation:?}");
        }
    }

    #[cfg(all(target_os = "macos", feature = "opencv-perception"))]
    #[test]
    fn opencv_tracker_fails_closed_when_most_of_the_grounded_control_is_occluded() {
        let seed = image::load_from_memory(&test_frame(80, 60))
            .unwrap()
            .to_rgb8();
        let next = image::load_from_memory(&occluded_frame(80, 60, 20))
            .unwrap()
            .to_rgb8();
        let mut tracker = OpenCvFlowTracker::new(
            seed.as_raw(),
            seed.width(),
            seed.height(),
            [0.25, 0.3, 0.5, 0.5],
        )
        .expect("test button should seed enough texture");

        let result = tracker.update(next.as_raw(), next.width(), next.height());

        let error = result.as_ref().unwrap_err();
        assert!(
            matches!(
                error.as_str(),
                "OPENCV_TRACK_PHOTOMETRIC_ERROR_HIGH"
                    | "OPENCV_TRACK_FEATURES_LOST"
                    | "OPENCV_TRACK_PHOTOMETRIC_EVIDENCE_INSUFFICIENT"
            ),
            "partial occlusion should stop tracking if evidence is insufficient: {error}"
        );
        assert_eq!(
            tracker.bbox_normalized(),
            [0.25, 0.3, 0.5, 0.5],
            "a rejected frame must not advance the track state"
        );
    }
}
