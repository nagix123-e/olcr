import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import olcr_api.app as app
from olcr_api.config import Settings
from olcr_api.ollama import OllamaProvider
from olcr_api.coding_telemetry import CodingTelemetry
from olcr_api.model_telemetry import model_telemetry
from olcr_api.coding_benchmark import finalize_live_artifact, prepare_fixture
from olcr_api.runtime import Runtime, VISION_SCHEMA_INSTRUCTION, _discard_untrusted_bbox_geometry, _discard_untrusted_relationships, _image_dimensions, _map_roi_visual_geometry, _normalize_ui_venus_bbox_convention, _prepare_visual_candidate, _explicit_text_read_intent, _record_ocr_evidence, _validated_ocr_roi, _validated_ocr_text, validate_visual_context
import tempfile
from pathlib import Path


class _Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.value).encode()


class RuntimeObservabilityTests(unittest.TestCase):
    @staticmethod
    def _runtime(settings, model, db=None, retrieval=None):
        if retrieval is None:
            retrieval = SimpleNamespace(files=SimpleNamespace(guard=SimpleNamespace()))
        return Runtime(settings, db or object(), retrieval, model)

    def test_hash_bound_failed_ocr_attempt_is_counted_without_accepting_ocr_content(self):
        before = model_telemetry.snapshot()["counters"]
        image_hash = "a" * 64
        _record_ocr_evidence({"ocr_attempt": {"source": "APPLE_VISION_OCR", "status": "error",
                                               "duration_ms": 7.25, "image_sha256": image_hash}}, image_hash)
        _record_ocr_evidence({"ocr_attempt": {"source": "APPLE_VISION_OCR", "status": "error",
                                               "duration_ms": 9.0, "image_sha256": "b" * 64}}, image_hash)
        after = model_telemetry.snapshot()["counters"]
        self.assertEqual(1, after["ocr_calls"] - before.get("ocr_calls", 0))
        self.assertEqual(1, after["ocr_call_errors"] - before.get("ocr_call_errors", 0))
        self.assertEqual(7.25, after["ocr_ms"] - before.get("ocr_ms", 0))

    @staticmethod
    def _png_header(width, height):
        raw=bytearray(24)
        raw[:8]=b"\x89PNG\r\n\x1a\n"
        raw[16:20]=width.to_bytes(4,"big")
        raw[20:24]=height.to_bytes(4,"big")
        return bytes(raw)

    def test_vision_prompt_uses_the_same_bbox_field_as_the_validator(self):
        self.assertIn("field name is exactly bbox", VISION_SCHEMA_INSTRUCTION)
        self.assertIn("normalized to 0..1", VISION_SCHEMA_INSTRUCTION)
        self.assertNotIn("bbox_normalized belongs", VISION_SCHEMA_INSTRUCTION)

    def test_ui_venus_bbox_0_1000_convention_is_normalized_without_repairing_other_values(self):
        value = {"elements": [
            {"bbox": [200, 100, 500, 600]},
            {"bbox": [0.2, 0.1, 0.5, 0.6]},
            {"bbox": [0, 0, 1200, 900]},
        ]}
        result, count = _normalize_ui_venus_bbox_convention(value)
        self.assertEqual(1, count)
        self.assertEqual([0.2, 0.1, 0.5, 0.6], result["elements"][0]["bbox"])
        self.assertEqual([0.2, 0.1, 0.5, 0.6], result["elements"][1]["bbox"])
        self.assertEqual([0, 0, 1200, 900], result["elements"][2]["bbox"])

    def test_gui_grounding_reuses_ui_venus_role_and_requires_explicit_confident_geometry(self):
        import base64
        import hashlib

        raw = self._png_header(1280, 720)
        visual = {"elements": [{"id": "save-1", "label": "Save", "bbox": [100, 200, 300, 400],
                                "confidence": 0.91}],
                  "text": ["Save"], "relationships": [], "anomalies": [],
                  "confidence": 0.88, "uncertainty": []}

        class FakeModel:
            def __init__(self): self.calls = []
            def vision(self, image_bytes, mime_type, prompt, model):
                self.calls.append((image_bytes, mime_type, prompt, model))
                return {"text": json.dumps(visual), "model": "ui_venus_mlx_6bit",
                        "model_role": "gui_grounding", "model_runtime": "test", "latency_ms": 2}

        model = FakeModel()
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), model)
        image = {"data_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
                 "image_sha256": hashlib.sha256(raw).hexdigest(), "width": 1280, "height": 720,
                 "ocr_observation": {"source": "APPLE_VISION_OCR", "observationId": "ocr-grounding",
                     "imageSha256": hashlib.sha256(raw).hexdigest()}, "ocr_duration_ms": 6.5}
        before_counters = model_telemetry.snapshot()["counters"]
        before_reasons = model_telemetry.snapshot()["vision_calls_by_reason"]

        result = runtime.ground_gui_target("Find the Save button", image)

        self.assertEqual("GROUNDED", result["status"])
        self.assertEqual("save-1", result["candidate"]["id"])
        self.assertEqual([0.1, 0.2, 0.3, 0.4], result["candidate"]["bbox"])
        self.assertEqual("role:gui_grounding", model.calls[0][3])
        self.assertEqual(raw, model.calls[0][0])
        self.assertIn("Perception only", model.calls[0][2])
        self.assertIn("Never return a bare coordinate array", model.calls[0][2])
        self.assertIn('"elements":[{"id":"target-1"', model.calls[0][2])
        visual["elements"][0].pop("confidence")
        incomplete = runtime.ground_gui_target("Find the Save button", image)
        self.assertEqual("INCONCLUSIVE", incomplete["status"])
        self.assertEqual("GUI_GROUNDING_LOW_CONFIDENCE", incomplete["reason"])
        visual["elements"][0]["confidence"] = 0.91
        visual["elements"][0]["bbox"] = [False, 0.2, 0.3, 0.4]
        invalid = runtime.ground_gui_target("Find the Save button", image)
        self.assertEqual("INCONCLUSIVE", invalid["status"])
        self.assertEqual("GUI_GROUNDING_CANDIDATE_INVALID", invalid["reason"])
        after_counters = model_telemetry.snapshot()["counters"]
        after_reasons = model_telemetry.snapshot()["vision_calls_by_reason"]
        self.assertEqual(3, after_counters["vision_calls"] - before_counters.get("vision_calls", 0))
        self.assertEqual(3, after_reasons["GUI_GROUNDING"] - before_reasons["GUI_GROUNDING"])
        self.assertEqual(3 * 1280 * 720,
            after_counters["vision_pixels_total"] - before_counters.get("vision_pixels_total", 0))
        self.assertEqual(3, after_counters["ocr_calls"] - before_counters.get("ocr_calls", 0))
        self.assertEqual(19.5, after_counters["ocr_ms"] - before_counters.get("ocr_ms", 0))

    def test_gui_grounding_malformed_model_geometry_fails_closed_with_structured_inconclusive(self):
        import base64
        import hashlib

        raw = self._png_header(1280, 720)

        class BareBoxModel:
            def vision(self, *_args):
                # UI-Venus returned this shape in a real Runtime-path fixture
                # probe. It contains no label, schema, or confidence evidence.
                return {"text": "[810, 77, 947, 140]", "model": "ui_venus_mlx_6bit",
                        "model_role": "gui_grounding", "model_runtime": "dedicated_mlx",
                        "model_revision": "fixture-revision", "latency_ms": 3721.2}

        image = {"data_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
                 "image_sha256": hashlib.sha256(raw).hexdigest(), "width": 1280, "height": 720}
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), BareBoxModel())

        result = runtime.ground_gui_target("Find the Save button", image)

        self.assertEqual("INCONCLUSIVE", result["status"])
        self.assertIsNone(result["candidate"])
        self.assertEqual("GUI_GROUNDING_RESPONSE_INVALID", result["reason"])
        self.assertEqual("invalid_response", result["model_call"]["status"])
        self.assertEqual("GUI_GROUNDING_RESPONSE_INVALID", result["model_call"]["error"])
        self.assertEqual("full_screen", result["model_call"]["vision_scope"])
        self.assertEqual(1280 * 720, result["model_call"]["pixel_count"])
        self.assertEqual(["invalid_model_response"], result["visual_context"]["uncertainty"])

    def test_gui_grounding_reports_no_unique_candidate_reason(self):
        import base64
        import hashlib

        raw = self._png_header(1280, 720)

        class NoCandidateModel:
            def vision(self, *_args):
                return {"text": json.dumps({"elements": [], "text": [], "relationships": [],
                                             "anomalies": [], "confidence": 0.9,
                                             "uncertainty": []}),
                        "model": "ui_venus_mlx_6bit", "latency_ms": 1}

        image = {"data_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
                 "image_sha256": hashlib.sha256(raw).hexdigest(), "width": 1280, "height": 720}
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), NoCandidateModel())
        result = runtime.ground_gui_target("Find an icon", image)
        self.assertEqual("INCONCLUSIVE", result["status"])
        self.assertIsNone(result["candidate"])
        self.assertEqual("GUI_GROUNDING_TARGET_NOT_UNIQUE", result["reason"])

    def test_gui_grounding_warmup_uses_the_existing_gui_vision_role(self):
        calls = []

        class WarmupModel:
            def vision(self, image_bytes, mime_type, prompt, model):
                calls.append((image_bytes, mime_type, prompt, model))
                return {"text": "A blue patch.", "model": "ui_venus_mlx_6bit",
                        "model_role": "gui_grounding", "model_runtime": "test", "latency_ms": 5}

        retrieval = SimpleNamespace(files=SimpleNamespace(guard=SimpleNamespace()))
        runtime = Runtime(Settings(main_model="brain", gui_vision_model="role:gui_grounding",
                                  vector_enabled=False), object(), retrieval, WarmupModel())
        result = runtime.warm_gui_grounding()
        self.assertEqual("READY", result["status"])
        self.assertEqual("GUI_GROUNDING_WARMUP", result["model_call"]["stage"])
        self.assertEqual("role:gui_grounding", calls[0][3])
        self.assertEqual("image/png", calls[0][1])
        self.assertEqual((64, 64), _image_dimensions(calls[0][0]))
        self.assertIn("warm-up only", calls[0][2])

    def test_gui_grounding_uses_validated_ocr_roi_and_maps_box_to_capture_frame(self):
        import base64
        import hashlib

        original = self._png_header(200, 150)
        roi = self._png_header(100, 70)
        original_hash = hashlib.sha256(original).hexdigest()
        observation = {"source": "APPLE_VISION_OCR", "observationId": "ocr-ground-1",
                       "coordinateSpace": "normalized_image_top_left", "imageSha256": original_hash,
                       "width": 200, "height": 150,
                       "elements": [{"text": "Save", "bboxNormalized": [0.3, 0.3, 0.5, 0.5],
                                     "confidence": 0.99}]}
        image = {"data_url": "data:image/png;base64," + base64.b64encode(original).decode(),
                 "image_sha256": original_hash, "width": 200, "height": 150,
                 "ocr_observation": observation,
                 "ocr_roi": {"source": "APPLE_VISION_OCR", "observation_id": "ocr-ground-1",
                             "image_sha256": original_hash, "target_text": "Save", "target_confidence": 0.99,
                             "source_bbox_pixels": [20, 20, 120, 90],
                             "source_bbox_normalized": [0.1, 20 / 150, 0.6, 0.6],
                             "width": 100, "height": 70,
                             "data_url": "data:image/png;base64," + base64.b64encode(roi).decode()}}
        visual = {"elements": [{"id": "save", "label": "Save", "bbox": [200, 100, 500, 600],
                                "confidence": 0.99}],
                  "text": ["Save"], "relationships": [], "anomalies": [],
                  "confidence": 0.99, "uncertainty": []}

        class FakeModel:
            def __init__(self): self.calls = []
            def vision(self, image_bytes, mime_type, prompt, model):
                self.calls.append((image_bytes, mime_type, prompt, model))
                return {"text": json.dumps(visual), "model": model, "model_runtime": "test", "latency_ms": 1}

        model = FakeModel()
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), model)
        result = runtime.ground_gui_target("Find the Save button", image)

        self.assertEqual("GROUNDED", result["status"])
        self.assertEqual(roi, model.calls[0][0])
        self.assertIn("relative to this crop", model.calls[0][2])
        self.assertIn("full required VisualContext JSON with both overall and element confidence", model.calls[0][2])
        self.assertEqual("roi", result["model_call"]["vision_scope"])
        self.assertEqual(7000, result["model_call"]["pixel_count"])
        for actual, expected in zip(result["candidate"]["bbox"], [0.2, 0.18, 0.35, 0.4133333333]):
            self.assertAlmostEqual(expected, actual, places=7)

    def test_inconclusive_ocr_roi_grounding_escalates_once_to_full_screen(self):
        import base64
        import hashlib

        original = self._png_header(200, 150)
        roi = self._png_header(100, 70)
        original_hash = hashlib.sha256(original).hexdigest()
        image = {"data_url": "data:image/png;base64," + base64.b64encode(original).decode(),
            "image_sha256": original_hash, "width": 200, "height": 150,
            "ocr_observation": {"source": "APPLE_VISION_OCR", "observationId": "ocr-fallback",
                "coordinateSpace": "normalized_image_top_left", "imageSha256": original_hash,
                "width": 200, "height": 150,
                "elements": [{"text": "Save", "bboxNormalized": [0.3, 0.3, 0.5, 0.5],
                              "confidence": 0.99}]},
            "ocr_roi": {"source": "APPLE_VISION_OCR", "observation_id": "ocr-fallback",
                "image_sha256": original_hash, "target_text": "Save", "target_confidence": 0.99,
                "source_bbox_pixels": [20, 20, 120, 90],
                "source_bbox_normalized": [0.1, 20 / 150, 0.6, 0.6],
                "width": 100, "height": 70,
                "data_url": "data:image/png;base64," + base64.b64encode(roi).decode()}}
        uncertain = {"elements": [{"id": "save-crop", "label": "Save", "bbox": [0.1, 0.1, 0.8, 0.8],
                                   "confidence": 0.55}],
            "text": ["Save"], "relationships": [], "anomalies": [],
            "confidence": 0.58, "uncertainty": ["crop context insufficient"]}
        grounded = {"elements": [{"id": "save-screen", "label": "Save", "bbox": [0.15, 0.2, 0.35, 0.4],
                                  "confidence": 0.91}],
            "text": ["Save"], "relationships": [], "anomalies": [],
            "confidence": 0.9, "uncertainty": []}

        class FakeModel:
            def __init__(self): self.calls = []
            def vision(self, raw, mime_type, prompt, model):
                self.calls.append((raw, mime_type, prompt, model))
                payload = uncertain if len(self.calls) == 1 else grounded
                return {"text": json.dumps(payload), "model": model,
                        "model_runtime": "test", "latency_ms": 2}

        model = FakeModel()
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), model)
        before = model_telemetry.snapshot()["counters"]

        result = runtime.ground_gui_target("Find the Save button", image)

        self.assertEqual("GROUNDED", result["status"])
        self.assertEqual("save-screen", result["candidate"]["id"])
        self.assertEqual(2, len(model.calls))
        self.assertEqual(roi, model.calls[0][0])
        self.assertIn("relative to this crop", model.calls[0][2])
        self.assertEqual(original, model.calls[1][0])
        self.assertNotIn("relative to this crop", model.calls[1][2])
        self.assertEqual("full_screen", result["model_call"]["vision_scope"])
        self.assertEqual("OCR_INSUFFICIENT", result["model_call"]["vision_escalation_reason"])
        self.assertEqual("GUI_GROUNDING_UNCERTAIN", result["model_call"]["initial_roi_failure_reason"])
        self.assertEqual(["roi", "full_screen"],
                         [call["vision_scope"] for call in result["model_calls"]])
        after = model_telemetry.snapshot()["counters"]
        self.assertEqual(2, after["vision_calls"] - before.get("vision_calls", 0))
        self.assertEqual(37_000, after["vision_pixels_total"] - before.get("vision_pixels_total", 0))

    def test_roi_telemetry_reads_png_dimensions_without_a_new_image_library(self):
        raw = bytearray(24)
        raw[:8] = b"\x89PNG\r\n\x1a\n"
        raw[16:20] = (1920).to_bytes(4, "big")
        raw[20:24] = (1080).to_bytes(4, "big")
        self.assertEqual((1920, 1080), _image_dimensions(bytes(raw)))
        self.assertIsNone(_image_dimensions(b"not an image"))

    def test_fast_ocr_is_limited_to_explicit_text_only_requests(self):
        self.assertTrue(_explicit_text_read_intent("Read the text in this image"))
        self.assertTrue(_explicit_text_read_intent("この画像の文字を読んで"))
        self.assertFalse(_explicit_text_read_intent("Click Save in this image"))
        self.assertFalse(_explicit_text_read_intent("画像の保存ボタンを押して"))
        self.assertFalse(_explicit_text_read_intent("Describe this screenshot"))

    def test_ocr_evidence_is_schema_and_geometry_bounded(self):
        element = {"text": "Save", "bboxNormalized": [0.1, 0.2, 0.3, 0.4], "confidence": 0.98}
        observation = {"source": "APPLE_VISION_OCR", "observationId": "abc123",
                       "coordinateSpace": "normalized_image_top_left", "imageSha256": "a" * 64, "width": 1280,
                       "height": 720, "elements": [element]}
        self.assertEqual(["Save"], _validated_ocr_text({"ocr_observation": observation}, "a" * 64))
        self.assertIsNone(_validated_ocr_text({"ocr_observation": observation}, "b" * 64))
        invalid = {**observation, "elements": [{**element, "bboxNormalized": [0, 0, 1000, 1000]}]}
        self.assertIsNone(_validated_ocr_text({"ocr_observation": invalid}))
        self.assertIsNone(_validated_ocr_text({"ocr_observation": {**observation, "source": "UI_VENUS"}}))

    def test_ocr_grounded_roi_requires_matching_image_identity_target_and_crop_dimensions(self):
        import base64
        import hashlib

        original=self._png_header(200,150)
        original_hash=hashlib.sha256(original).hexdigest()
        roi_bytes=self._png_header(100,70)
        observation={"source":"APPLE_VISION_OCR","observationId":"ocr-1",
            "coordinateSpace":"normalized_image_top_left","imageSha256":original_hash,
            "width":200,"height":150,"elements":[{"text":"Save",
                "bboxNormalized":[0.3,0.3,0.5,0.5],"confidence":0.99}]}
        image={"ocr_observation":observation,"ocr_roi":{"source":"APPLE_VISION_OCR",
            "observation_id":"ocr-1","image_sha256":original_hash,"target_text":"Save",
            "target_confidence":0.99,"source_bbox_pixels":[20,20,120,90],
            "source_bbox_normalized":[0.1,0.1333333333,0.6,0.6],"width":100,"height":70,
            "data_url":"data:image/png;base64,"+base64.b64encode(roi_bytes).decode()}}

        accepted=_validated_ocr_roi(image,original)
        self.assertIsNotNone(accepted)
        self.assertEqual([20,20,120,90],accepted["source_bbox_pixels"])

        image["ocr_roi"]={**image["ocr_roi"],"width":99}
        self.assertIsNone(_validated_ocr_roi(image,original))

    def test_roi_normalized_bbox_maps_back_to_original_image(self):
        visual={"elements":[{"id":"save","bbox":[0.2,0.1,0.5,0.6]},
                             {"id":"unknown","label":"no geometry"}]}

        mapped=_map_roi_visual_geometry(visual,[0.1,0.2,0.5,0.8])

        for actual,expected in zip(mapped["elements"][0]["bbox"],[0.18,0.26,0.3,0.56]):
            self.assertAlmostEqual(expected,actual,places=8)
        self.assertNotIn("bbox",mapped["elements"][1])

    def test_gui_vision_runs_from_validated_ocr_roi_and_maps_boxes_to_full_image(self):
        import base64
        import hashlib

        original=self._png_header(200,150)
        original_hash=hashlib.sha256(original).hexdigest()
        roi_bytes=self._png_header(100,70)
        observation={"source":"APPLE_VISION_OCR","observationId":"ocr-live-fixture",
            "coordinateSpace":"normalized_image_top_left","imageSha256":original_hash,
            "width":200,"height":150,"elements":[{"text":"Save",
                "bboxNormalized":[0.3,0.3,0.5,0.5],"confidence":0.99}]}
        image={"data_url":"data:image/png;base64,"+base64.b64encode(original).decode(),
            "mime_type":"image/png","ocr_observation":observation,
            "ocr_roi":{"source":"APPLE_VISION_OCR","observation_id":"ocr-live-fixture",
                "image_sha256":original_hash,"target_text":"Save","target_confidence":0.99,
                "source_bbox_pixels":[20,20,120,90],
                "source_bbox_normalized":[0.1,0.1333333333,0.6,0.6],"width":100,"height":70,
                "data_url":"data:image/png;base64,"+base64.b64encode(roi_bytes).decode()}}
        # UI-Venus fixture responses use its observed 0..1000 box convention;
        # Runtime must normalize that known role before validating/remapping.
        visual={"elements":[{"id":"save","label":"Save","bbox":[200,100,500,600],"confidence":0.99}],
            "text":["Save"],"relationships":[],"anomalies":[],"confidence":0.99,"uncertainty":[]}

        class FakeModel:
            def __init__(self): self.calls=[]; self.generated_messages=None
            def vision(self,raw,mime_type,prompt,model):
                self.calls.append((raw,mime_type,prompt,model))
                return {"text":json.dumps(visual),"model":model,"model_runtime":"test","latency_ms":1}
            def generate(self,messages,model,**_kwargs):
                self.generated_messages=messages
                return {"text":"Found Save.","model":model,"model_runtime":"test","latency_ms":1}
        class FakeRetrieval:
            semantic_telemetry={};semantic_normalizer=None;semantic_evaluator=None;vectors=None
            files=SimpleNamespace(guard=SimpleNamespace())
            def retrieve(self,*_args): return [],[]
        class FakeDb:
            def save_task(self,_task): pass

        model=FakeModel()
        runtime=self._runtime(Settings(main_model="brain",vector_enabled=False),model,FakeDb(),FakeRetrieval())
        task,answer=runtime.execute_image("Click Save in this screenshot",image)

        self.assertEqual("completed",task.state.value,task.error)
        self.assertEqual("Found Save.",answer)
        self.assertEqual(1,len(model.calls))
        self.assertEqual(roi_bytes,model.calls[0][0])
        self.assertEqual("role:gui_grounding",model.calls[0][3])
        self.assertIn("relative to this crop",model.calls[0][2])
        self.assertEqual("roi",task.model_calls[0]["vision_scope"])
        self.assertEqual(7000,task.model_calls[0]["pixel_count"])
        packet=model.generated_messages[0]["content"]
        evidence=packet.split("[CURRENT_ATTACHMENT_VISION_EVIDENCE]\n",1)[1].split(
            "\n[VISUAL_RESPONSE_GUARDRAILS]",1)[0]
        grounded=json.loads(evidence)
        for actual,expected in zip(grounded["elements"][0]["bbox"],[0.2,0.18,0.35,0.4133333333]):
            self.assertAlmostEqual(expected,actual,places=7)

        invalid_image={**image,"ocr_roi":{**image["ocr_roi"],"width":99}}
        fallback_model=FakeModel()
        fallback_runtime=self._runtime(Settings(main_model="brain",vector_enabled=False),fallback_model,FakeDb(),FakeRetrieval())
        fallback_task,_=fallback_runtime.execute_image("Click Save in this screenshot",invalid_image)
        self.assertEqual("completed",fallback_task.state.value,fallback_task.error)
        self.assertEqual(original,fallback_model.calls[0][0])
        self.assertEqual("full_screen",fallback_task.model_calls[0]["vision_scope"])

        failed_ocr_image={"data_url":"data:image/png;base64,"+base64.b64encode(original).decode(),
            "mime_type":"image/png","ocr_attempt":{"source":"APPLE_VISION_OCR","status":"error",
                "duration_ms":8.5,"image_sha256":original_hash}}
        failed_ocr_model=FakeModel()
        failed_ocr_runtime=self._runtime(Settings(main_model="brain",vector_enabled=False),failed_ocr_model,FakeDb(),FakeRetrieval())
        before=model_telemetry.snapshot()["counters"]
        failed_ocr_task,_=failed_ocr_runtime.execute_image("Click Save in this screenshot",failed_ocr_image)
        after=model_telemetry.snapshot()["counters"]
        self.assertEqual("completed",failed_ocr_task.state.value,failed_ocr_task.error)
        self.assertEqual(1,len(failed_ocr_model.calls))
        self.assertEqual(original,failed_ocr_model.calls[0][0])
        self.assertEqual("role:gui_grounding",failed_ocr_model.calls[0][3])
        self.assertEqual("full_screen",failed_ocr_task.model_calls[0]["vision_scope"])
        self.assertNotIn("relative to this crop",failed_ocr_model.calls[0][2])
        self.assertEqual(1,after["ocr_calls"]-before.get("ocr_calls",0))
        self.assertEqual(1,after["ocr_call_errors"]-before.get("ocr_call_errors",0))
        self.assertEqual(8.5,after["ocr_ms"]-before.get("ocr_ms",0))

    def test_valid_text_only_ocr_returns_without_calling_vision_model(self):
        class FakeDb:
            def save_task(self, _task):
                pass
        class NoVisionModel:
            def vision(self, *_args):
                raise AssertionError("OCR fast path should not invoke UI-Venus")
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), NoVisionModel(), FakeDb())
        image = {"data_url": "data:image/png;base64,eA==", "mime_type": "image/png",
                 "ocr_observation": {"source": "APPLE_VISION_OCR", "observationId": "test-1",
                    "coordinateSpace": "normalized_image_top_left", "imageSha256": __import__("hashlib").sha256(b"x").hexdigest(), "width": 10, "height": 10,
                    "elements": [{"text": "Save", "bboxNormalized": [0.1, 0.2, 0.8, 0.4], "confidence": 0.99}]}}
        task, answer = runtime.execute_image("Read the text in this image", image)
        self.assertEqual("completed", task.state.value)
        self.assertEqual("Visible text detected in the attached image:\n- Save", answer)

    def test_vision_payload_bounds_generation_and_keeps_native_image(self):
        captured = {}
        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data.decode()))
            return _Response({"message": {"content": "{}"}})
        provider = OllamaProvider("http://ollama")
        with patch("olcr_api.ollama.request.urlopen", side_effect=fake_urlopen):
            provider.vision(b"fixture", "image/png", "Return JSON only", "qwen3-vl:2b-instruct")
        self.assertEqual(1024, captured["options"]["num_predict"])
        self.assertEqual(0.0, captured["options"]["temperature"])
        self.assertEqual(["Zml4dHVyZQ=="], captured["messages"][0]["images"])

    def test_invalid_optional_bbox_is_discarded_and_routes_as_uncertain(self):
        visual = {"elements": [{"id": "a", "label": "ambiguous control", "bbox": [120, 40, 180, 90]}],
                  "text": ["Save", "Close"], "relationships": [], "anomalies": [],
                  "confidence": 0.9, "uncertainty": []}
        visual, discarded = _discard_untrusted_bbox_geometry(visual)
        self.assertEqual(1, discarded)
        self.assertNotIn("bbox", visual["elements"][0])
        self.assertEqual(["Save", "Close"], visual["text"])
        self.assertEqual(0.64, visual["confidence"])
        self.assertTrue(visual["uncertainty"])
        self.assertEqual(visual, validate_visual_context(json.dumps(visual)))

    def test_invalid_bbox_shape_is_not_silently_repaired(self):
        visual = {"elements": [{"id": "a", "bbox": [0, 0, 1]}]}
        visual, discarded = _discard_untrusted_bbox_geometry(visual)
        self.assertEqual(0, discarded)
        with self.assertRaisesRegex(RuntimeError, "invalid bbox"):
            validate_visual_context(json.dumps(visual))

    def test_noncanonical_relationship_is_dropped_without_losing_visual_evidence(self):
        visual = {"elements": [{"id": "save", "label": "Save"}], "text": ["Save", "Close"],
                  "relationships": [{"source": "save", "target": "close", "relationship": "near"}],
                  "anomalies": [], "confidence": 0.8, "uncertainty": []}
        visual, discarded = _discard_untrusted_relationships(visual)
        self.assertEqual(1, discarded)
        self.assertEqual([], visual["relationships"])
        self.assertEqual(["Save", "Close"], visual["text"])
        self.assertTrue(visual["uncertainty"])
        self.assertEqual(visual, validate_visual_context(json.dumps(visual)))

    def test_runtime_sanitizes_retry_relations_and_stays_with_a_registered_vision_role(self):
        retry_visual = {
            "elements": [{"id": "circle-a", "label": "left circle"}, {"id": "circle-b", "label": "right circle"}],
            "text": [],
            "relationships": [{"from": "circle-a", "to": "circle-b", "type": "larger_than", "label": "unclear", "confidence": 0.8}],
            "anomalies": [], "confidence": 0.9, "uncertainty": [],
        }
        class FakeModel:
            def __init__(self):
                self.vision_models = []
                self.generated_messages = None
                self.vision_responses = [
                    "The first response is not JSON.",
                    json.dumps(retry_visual),
                ]

            def vision(self, _raw, _mime_type, _prompt, model):
                self.vision_models.append(model)
                return {"text": self.vision_responses.pop(0), "model": model, "model_runtime": "test", "latency_ms": 1}

            def generate(self, messages, model, **_kwargs):
                self.generated_messages = messages
                return {"text": "The relative size is uncertain.", "model": model, "model_runtime": "test", "latency_ms": 1}

        class FakeRetrieval:
            semantic_telemetry = {}
            semantic_normalizer = None
            semantic_evaluator = None
            vectors = None
            files=SimpleNamespace(guard=SimpleNamespace())

            def retrieve(self, *_args):
                return [], []

        class FakeDb:
            def __init__(self):
                self.saved = []

            def save_task(self, task):
                self.saved.append(task)

        model = FakeModel()
        db = FakeDb()
        runtime = self._runtime(Settings(main_model="brain", vector_enabled=False), model, db, FakeRetrieval())
        task, answer = runtime.execute_image(
            "Describe this ambiguous illustration and state uncertainty.",
            {"data_url": "data:image/png;base64,eA==", "mime_type": "image/png"},
        )

        self.assertEqual("completed", task.state.value, task.error)
        self.assertEqual("The relative size is uncertain.", answer)
        self.assertEqual(["role:general_image", "role:general_image"], model.vision_models)
        self.assertNotIn("role:vision_fallback", model.vision_models)
        self.assertEqual(["VISION_CALL", "VISION_FORMAT_RETRY", "MAIN_LANGUAGE_MODEL"],
                         [call.get("stage") for call in task.model_calls])
        self.assertEqual("full_screen", task.model_calls[0]["vision_scope"])
        self.assertEqual("SEMANTIC_REVALIDATION", task.model_calls[1]["vision_escalation_reason"])
        self.assertIsNone(task.model_calls[0]["pixel_count"])
        self.assertEqual([task], db.saved)
        packet = model.generated_messages[0]["content"]
        evidence = packet.split("[CURRENT_ATTACHMENT_VISION_EVIDENCE]\n", 1)[1].split("\n[VISUAL_RESPONSE_GUARDRAILS]", 1)[0]
        visual = json.loads(evidence)
        self.assertEqual([], visual["relationships"])
        self.assertTrue(any("relations were discarded" in item for item in visual["uncertainty"]))
        guardrails = packet.split("[VISUAL_RESPONSE_GUARDRAILS]\n", 1)[1].split(
            "\n[ACTIVE_PROJECT_AND_CONVERSATION_CONTEXT]", 1
        )[0]
        self.assertIn("Do not invent exact pixel dimensions", guardrails)
        self.assertIn("normalized bounding boxes are not pixel measurements", guardrails)
        self.assertIn("preserve that uncertainty", guardrails)

    def test_visual_candidate_discards_real_world_model_relation_shapes(self):
        for relation in (
            {"from": "a", "to": "b", "type": "left_of", "label": "uncertain"},
            {"confidence": 0.4, "label": "maybe near"},
        ):
            with self.subTest(relation=relation):
                visual, diagnostics = _prepare_visual_candidate({
                    "elements": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                    "text": [], "relationships": [relation], "anomalies": [], "confidence": 0.8, "uncertainty": [],
                })
                self.assertEqual(1, diagnostics["discarded_relation_count"])
                self.assertEqual([], visual["relationships"])
                self.assertTrue(visual["uncertainty"])

    def test_coding_request_payload_is_unchanged_and_options_are_recorded(self):
        captured = {}
        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data.decode()))
            return _Response({"message": {"content": "ok"}, "prompt_eval_count": 2, "eval_count": 3})
        provider = OllamaProvider("http://ollama")
        with patch("olcr_api.ollama.request.urlopen", side_effect=fake_urlopen):
            result = provider.generate([{"role": "user", "content": "x"}], "qwen3.5:9b", think=False,
                                       format={"type": "object"})
        self.assertEqual({"model", "messages", "stream", "think", "format"}, set(captured))
        self.assertNotIn("options", captured)
        self.assertNotIn("keep_alive", captured)
        self.assertEqual("OMITTED", result["request_options"]["num_ctx"])
        self.assertFalse(result["request_options"]["think"])
        self.assertEqual("EXPLICIT_JSON_SCHEMA", result["request_options"]["format"])

    def test_runtime_snapshot_is_allowlisted_and_truthful(self):
        snapshot = OllamaProvider.summarize_runtime_observation(
            version={"version": "0.32.15"},
            ps={"models": [{"name": "qwen3.5:9b", "context_length": 32768, "size": 10, "size_vram": 9,
                            "expires_at": "never", "details": {"format": "gguf", "quantization_level": "Q4_K_M", "parameter_size": "9.7B"}}]},
            show={"details": {"format": "gguf", "quantization_level": "Q4_K_M", "parameter_size": "9.7B"},
                  "model_info": {"qwen35.context_length": 262144},
                  "parameters": "temperature 1\ntop_p 0.95\ntop_k 20"},
            target_model="qwen3.5:9b")
        self.assertEqual("0.32.15", snapshot["ollama_version"])
        self.assertTrue(snapshot["target_loaded"])
        self.assertEqual(32768, snapshot["model_context_active"])
        self.assertEqual(262144, snapshot["model_metadata"]["context_capacity"])
        self.assertEqual(0.95, snapshot["model_metadata"]["parameter_defaults"]["top_p"])
        self.assertEqual("UNKNOWN", snapshot["daemon_keep_alive"])
        self.assertNotIn("environment", snapshot)

    def test_telemetry_keeps_request_options_and_provenance_additively(self):
        telemetry = CodingTelemetry("task", "qwen3.5:9b")
        call = telemetry.add_call(
            phase_id="p1", role="PLANNER", model="qwen3.5:9b",
            messages=[{"role": "user", "content": "x"}],
            result={"prompt_tokens": 1, "completion_tokens": 1,
                    "request_options": {"num_ctx": "OMITTED"}},
            model_provenance={"effective_model": "qwen3.5:9b", "source": "DEFAULT_MAIN_MODEL"},
            started=1, finished=1.1, structured=True, thinking=False, success=True)
        record = telemetry.finish("PASS")
        self.assertEqual("OMITTED", call["request_options"]["num_ctx"])
        self.assertEqual("DEFAULT_MAIN_MODEL", record["model_provenance"]["PLANNER"]["source"])
        self.assertEqual("YES", record["request_options_observed"])

    def test_legacy_application_setting_provenance_explains_effective_model(self):
        with patch.object(app.db, "load_application_settings", return_value={"main_model": "qwen3:14b"}), \
             patch.object(app.settings, "main_model", "qwen3.5:9b"):
            value = app._coding_model_provenance("qwen3.5:9b", "PLANNER")
        self.assertEqual("qwen3.5:9b", value["effective_model"])
        self.assertEqual("APPLICATION_SETTINGS_MAIN_MODEL_LEGACY_NORMALIZED_TO_DEFAULT_MAIN_MODEL", value["source"])
        self.assertEqual("qwen3:14b", value["application_main_model"])

    def test_runtime_endpoint_falls_back_to_unknown_without_provider_support(self):
        class Fake:
            pass
        with patch.object(app.runtime, "model", Fake()):
            value = app.runtime_observability()
        self.assertEqual("UNKNOWN", value["status"])
        self.assertEqual("UNKNOWN", value["processor_placement"])
        self.assertIn("model_provenance", value)

    def test_live_artifact_preserves_runtime_observation_additively(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            contract = prepare_fixture("small", directory)
            artifact = finalize_live_artifact(
                directory, contract=contract, task_id="task", telemetry={"model_name": "qwen3.5:9b", "model_call_count": 1, "task_finished_at": 2},
                task_status="COMPLETED", verify_status="PASS", changed_files=["src/math.py"],
                runtime_model_observation={"status": "OK", "target_model": "qwen3.5:9b"})
            self.assertEqual("OK", artifact["runtime_model_observation"]["status"])
            Path(contract["root"]).exists() and __import__("shutil").rmtree(contract["root"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
