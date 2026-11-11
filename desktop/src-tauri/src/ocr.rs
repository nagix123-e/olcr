//! Bounded native text perception backed by Apple's Vision framework.

use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_IMAGE_BYTES: usize = 5_000_000;
const MAX_IMAGE_DIMENSION: u32 = 8_192;
const MAX_IMAGE_PIXELS: u64 = 50_000_000;
const MAX_ROI_IMAGE_BYTES: usize = 1_500_000;
const MAX_TEXT_OBSERVATIONS: usize = 160;
const MAX_TEXT_CHARS: usize = 256;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OcrTextElement {
    pub text: String,
    /// Normalized [x1, y1, x2, y2], image origin at top-left.
    pub bbox_normalized: [f32; 4],
    pub confidence: f32,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OcrObservation {
    pub source: &'static str,
    pub observation_id: String,
    pub observed_at: f64,
    pub coordinate_space: &'static str,
    pub width: u32,
    pub height: u32,
    pub elements: Vec<OcrTextElement>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OcrGroundedRoi {
    pub data_url: String,
    pub width: u32,
    pub height: u32,
    /// Crop rectangle in the original image's top-left normalized space.
    pub source_bbox_normalized: [f32; 4],
    pub source_bbox_pixels: [u32; 4],
}

pub fn crop_ocr_grounded_roi(
    data_url: &str,
    text_bbox: [f32; 4],
) -> Result<OcrGroundedRoi, String> {
    use base64::Engine;
    use image::{ImageFormat, ImageReader};
    use std::io::{Cursor, Write};

    let bytes = decode_image_data_url(data_url)?;
    let image = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| "OCR_IMAGE_DECODE_FAILED")?
        .decode()
        .map_err(|_| "OCR_IMAGE_DECODE_FAILED")?;
    let width = image.width();
    let height = image.height();
    if width == 0
        || height == 0
        || width > MAX_IMAGE_DIMENSION
        || height > MAX_IMAGE_DIMENSION
        || u64::from(width) * u64::from(height) > MAX_IMAGE_PIXELS
    {
        return Err("OCR_IMAGE_DIMENSIONS_UNSUPPORTED".into());
    }
    if text_bbox
        .iter()
        .any(|value| !value.is_finite() || !(0.0..=1.0).contains(value))
        || text_bbox[0] >= text_bbox[2]
        || text_bbox[1] >= text_bbox[3]
    {
        return Err("OCR_ROI_BBOX_INVALID".into());
    }

    let left = (text_bbox[0] * width as f32).floor() as i64;
    let top = (text_bbox[1] * height as f32).floor() as i64;
    let right = (text_bbox[2] * width as f32).ceil() as i64;
    let bottom = (text_bbox[3] * height as f32).ceil() as i64;
    let pad_x = ((width as f32 * 0.08).round() as i64).max(48);
    let pad_y = ((height as f32 * 0.06).round() as i64).max(32);
    let x1 = (left - pad_x).clamp(0, width as i64 - 1) as u32;
    let y1 = (top - pad_y).clamp(0, height as i64 - 1) as u32;
    let x2 = (right + pad_x).clamp(x1 as i64 + 1, width as i64) as u32;
    let y2 = (bottom + pad_y).clamp(y1 as i64 + 1, height as i64) as u32;
    let crop_width = x2 - x1;
    let crop_height = y2 - y1;
    if crop_width < 64
        || crop_height < 48
        || u64::from(crop_width) * u64::from(crop_height) * 100
            > u64::from(width) * u64::from(height) * 65
    {
        return Err("OCR_ROI_NOT_BOUNDED".into());
    }

    let crop = image.crop_imm(x1, y1, crop_width, crop_height);
    let mut encoded = Cursor::new(Vec::new());
    crop.write_to(&mut encoded, ImageFormat::Jpeg)
        .map_err(|_| "OCR_ROI_ENCODE_FAILED")?;
    encoded.flush().map_err(|_| "OCR_ROI_ENCODE_FAILED")?;
    let encoded = encoded.into_inner();
    if encoded.len() > MAX_ROI_IMAGE_BYTES {
        return Err("OCR_ROI_TOO_LARGE".into());
    }
    let source_bbox_pixels = [x1, y1, x2, y2];
    let source_bbox_normalized = [
        x1 as f32 / width as f32,
        y1 as f32 / height as f32,
        x2 as f32 / width as f32,
        y2 as f32 / height as f32,
    ];
    Ok(OcrGroundedRoi {
        data_url: format!(
            "data:image/jpeg;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(encoded)
        ),
        width: crop_width,
        height: crop_height,
        source_bbox_normalized,
        source_bbox_pixels,
    })
}

fn new_observation_id() -> String {
    use rand::{distributions::Alphanumeric, Rng};
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(24)
        .map(char::from)
        .collect()
}

pub fn decode_image_data_url(data_url: &str) -> Result<Vec<u8>, String> {
    use base64::Engine;

    if data_url.len() > MAX_IMAGE_BYTES.saturating_mul(4) / 3 + 128 {
        return Err("OCR_IMAGE_TOO_LARGE".into());
    }
    let (header, encoded) = data_url
        .split_once(',')
        .ok_or("OCR_IMAGE_DATA_URL_INVALID")?;
    if header != "data:image/png;base64" && header != "data:image/jpeg;base64" {
        return Err("OCR_IMAGE_TYPE_UNSUPPORTED".into());
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| "OCR_IMAGE_BASE64_INVALID")?;
    if bytes.is_empty() || bytes.len() > MAX_IMAGE_BYTES {
        return Err("OCR_IMAGE_TOO_LARGE".into());
    }
    Ok(bytes)
}

#[cfg(target_os = "macos")]
pub fn recognize_image_bytes(bytes: &[u8]) -> Result<OcrObservation, String> {
    use image::{ImageFormat, ImageReader};
    use objc2::AnyThread;
    use objc2_foundation::{NSArray, NSData, NSDictionary};
    use objc2_vision::{
        VNImageRequestHandler, VNRecognizeTextRequest, VNRequest, VNRequestTextRecognitionLevel,
    };
    use std::io::Cursor;

    if bytes.is_empty() || bytes.len() > MAX_IMAGE_BYTES {
        return Err("OCR_IMAGE_TOO_LARGE".into());
    }
    let reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|_| "OCR_IMAGE_DECODE_FAILED")?;
    let format = reader.format().ok_or("OCR_IMAGE_FORMAT_UNKNOWN")?;
    if !matches!(format, ImageFormat::Png | ImageFormat::Jpeg) {
        return Err("OCR_IMAGE_TYPE_UNSUPPORTED".into());
    }
    let (width, height) = reader
        .into_dimensions()
        .map_err(|_| "OCR_IMAGE_DECODE_FAILED")?;
    if width == 0
        || height == 0
        || width > MAX_IMAGE_DIMENSION
        || height > MAX_IMAGE_DIMENSION
        || u64::from(width) * u64::from(height) > MAX_IMAGE_PIXELS
    {
        return Err("OCR_IMAGE_DIMENSIONS_UNSUPPORTED".into());
    }

    let request = VNRecognizeTextRequest::new();
    request.setRecognitionLevel(VNRequestTextRecognitionLevel::Accurate);
    request.setUsesLanguageCorrection(false);
    request.setAutomaticallyDetectsLanguage(true);

    let data = NSData::with_bytes(bytes);
    let options = NSDictionary::new();
    let handler = VNImageRequestHandler::initWithData_options(
        VNImageRequestHandler::alloc(),
        &data,
        &options,
    );
    let request_as_base: objc2::rc::Retained<VNRequest> = request.clone().into_super().into_super();
    let requests = NSArray::from_retained_slice(&[request_as_base]);
    handler
        .performRequests_error(&requests)
        .map_err(|_| "OCR_VISION_REQUEST_FAILED")?;

    let mut elements = Vec::new();
    if let Some(results) = request.results() {
        for observation in results.iter().take(MAX_TEXT_OBSERVATIONS) {
            let Some(candidate) = observation.topCandidates(1).iter().next() else {
                continue;
            };
            let text = candidate.string().to_string().trim().to_owned();
            if text.is_empty() {
                continue;
            }
            let text: String = text.chars().take(MAX_TEXT_CHARS).collect();
            // Vision boxes use a lower-left origin; the product contract uses top-left.
            let rect = unsafe { observation.boundingBox() };
            let x1 = rect.origin.x.clamp(0.0, 1.0) as f32;
            let y1 = (1.0 - rect.origin.y - rect.size.height).clamp(0.0, 1.0) as f32;
            let x2 = (rect.origin.x + rect.size.width).clamp(0.0, 1.0) as f32;
            let y2 = (1.0 - rect.origin.y).clamp(0.0, 1.0) as f32;
            if x2 <= x1 || y2 <= y1 {
                continue;
            }
            elements.push(OcrTextElement {
                text,
                bbox_normalized: [x1, y1, x2, y2],
                confidence: candidate.confidence().clamp(0.0, 1.0),
            });
        }
    }

    Ok(OcrObservation {
        source: "APPLE_VISION_OCR",
        observation_id: new_observation_id(),
        observed_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "OCR_CLOCK_UNAVAILABLE")?
            .as_secs_f64(),
        coordinate_space: "normalized_image_top_left",
        width,
        height,
        elements,
    })
}

#[cfg(not(target_os = "macos"))]
pub fn recognize_image_bytes(_bytes: &[u8]) -> Result<OcrObservation, String> {
    Err("OCR_UNSUPPORTED_PLATFORM".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_only_bounded_png_and_jpeg_data_urls() {
        assert_eq!(
            decode_image_data_url("data:image/gif;base64,R0lGODlh").unwrap_err(),
            "OCR_IMAGE_TYPE_UNSUPPORTED"
        );
        assert_eq!(
            decode_image_data_url("not-a-data-url").unwrap_err(),
            "OCR_IMAGE_DATA_URL_INVALID"
        );
        assert_eq!(
            decode_image_data_url("data:image/png;base64,%%%!!!").unwrap_err(),
            "OCR_IMAGE_BASE64_INVALID"
        );
    }

    #[test]
    fn crops_a_padded_bounded_roi_from_ocr_geometry() {
        use base64::Engine;

        let bytes = include_bytes!(
            "../../../benchmark_results/model_readiness/gui_set/ui_large_button.png"
        );
        let data_url = format!(
            "data:image/png;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(bytes)
        );
        let roi = crop_ocr_grounded_roi(&data_url, [0.45, 0.35, 0.60, 0.55]).unwrap();
        assert!(roi.width >= 64 && roi.height >= 48);
        assert!(roi.width < 1280 && roi.height < 720);
        assert!(roi.data_url.starts_with("data:image/jpeg;base64,"));
        let [x1, y1, x2, y2] = roi.source_bbox_normalized;
        assert!(0.0 <= x1 && x1 < x2 && x2 <= 1.0);
        assert!(0.0 <= y1 && y1 < y2 && y2 <= 1.0);
    }

    #[test]
    fn rejects_invalid_or_overlarge_ocr_roi_geometry() {
        use base64::Engine;

        let bytes = include_bytes!(
            "../../../benchmark_results/model_readiness/gui_set/ui_large_button.png"
        );
        let data_url = format!(
            "data:image/png;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(bytes)
        );
        assert_eq!(
            crop_ocr_grounded_roi(&data_url, [0.8, 0.2, 0.4, 0.5]).unwrap_err(),
            "OCR_ROI_BBOX_INVALID"
        );
        assert_eq!(
            crop_ocr_grounded_roi(&data_url, [0.0, 0.0, 1.0, 1.0]).unwrap_err(),
            "OCR_ROI_NOT_BOUNDED"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn reads_gui_fixture_text_with_bounded_normalized_boxes() {
        let observation = recognize_image_bytes(include_bytes!(
            "../../../benchmark_results/model_readiness/gui_set/ui_large_button.png"
        ))
        .unwrap();
        assert_eq!(observation.source, "APPLE_VISION_OCR");
        assert!(observation
            .elements
            .iter()
            .any(|element| element.text == "Save"));
        assert!(observation.elements.len() <= MAX_TEXT_OBSERVATIONS);
        assert!(observation.elements.iter().all(|element| {
            let [x1, y1, x2, y2] = element.bbox_normalized;
            0.0 <= x1
                && x1 < x2
                && x2 <= 1.0
                && 0.0 <= y1
                && y1 < y2
                && y2 <= 1.0
                && element.text.chars().count() <= MAX_TEXT_CHARS
        }));
    }
}
