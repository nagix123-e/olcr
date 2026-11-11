"""NDJSON worker loaded only from the optional hands-free environment."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
import wave

from handsfree_gestures import GestureInput

root = Path(sys.argv[1])
refs = []
recognizer = None
gestures = GestureInput()
last_frame = 0


def audio_samples(encoded):
    with wave.open(io.BytesIO(base64.b64decode(encoded)), 'rb') as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768


def trim(samples):
    # Keep margins around voiced frames; do not train a second wake model.
    active = np.flatnonzero(np.abs(samples) > .015)
    if len(active) < 100:
        raise ValueError('HANDSFREE_SAMPLE_SILENT')
    samples = samples[max(0, active[0] - 1600):min(len(samples), active[-1] + 1601)]
    return np.pad(samples, (0, max(0, 12400 - len(samples))))


def profile():
    try: return json.loads((root / 'profile.json').read_text())
    except FileNotFoundError: return {'label': 'OLCR', 'count': 0, 'threshold': .1}


def save_profile(value):
    path = root / 'profile.json'
    temporary = root / 'profile.partial'
    temporary.write_text(json.dumps(value, ensure_ascii=False)); temporary.chmod(0o600)
    temporary.replace(path)


def load_refs():
    global refs
    refs = []
    for path in sorted((root / 'references').glob('*.wav')):
        try: refs.append(extract_embedding_features(path=str(path)))
        except Exception: continue
    current = profile()
    if current['count'] != len(refs):
        current['count'] = len(refs); save_profile(current)


def handle(value):
    global np, cv2, mp, recognizer, extract_embedding_features, distance, last_frame
    op = value['op']
    if op == 'init':
        import numpy as np
        import cv2
        import mediapipe as mp
        from lwake.features import extract_embedding_features, dtw_cosine_normalized_distance
        distance = dtw_cosine_normalized_distance
        load_refs()
        # Prime local-wake once; this is a local model bundled in its wheel.
        extract_embedding_features(y=np.zeros(16000, dtype=np.float32))
        options = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(root / 'gesture_recognizer.task')),
            running_mode=mp.tasks.vision.RunningMode.VIDEO, num_hands=1,
            min_hand_detection_confidence=.65, min_tracking_confidence=.65)
        recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)
        return {'ready': True}
    if op == 'enroll':
        current = profile()
        label = value['label'].strip()
        if refs and current['label'] != label: raise ValueError('HANDSFREE_DELETE_SAMPLES_BEFORE_RENAME')
        current['label'] = label
        if len(refs) >= 6: raise ValueError('HANDSFREE_SAMPLE_LIMIT')
        samples = trim(audio_samples(value['audio']))
        features = extract_embedding_features(y=samples)
        directory = root / 'references'; directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / f'{time.time_ns()}.wav'
        temporary = path.with_suffix('.partial')
        with wave.open(str(temporary), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes((samples * 32767).astype('<i2').tobytes())
        temporary.chmod(0o600); temporary.replace(path); refs.append(features)
        pairs = [distance(a, b) for index, a in enumerate(refs) for b in refs[index + 1:]]
        threshold = max(.025, min(.15, max(pairs) * 1.4 + .015)) if pairs else .1
        current.update(count=len(refs), threshold=float(threshold)); save_profile(current)
        return {'profile': current}
    if op == 'delete':
        for path in (root / 'references').glob('*.wav'): path.unlink()
        refs.clear(); save_profile({'label': value.get('label', 'OLCR').strip()[:40] or 'OLCR', 'count': 0, 'threshold': .1})
        return {'profile': profile()}
    if op == 'reset': return {'events': gestures.reset()}
    if op == 'wake':
        if len(refs) < 3: return {'detected': False, 'enrollment_required': True}
        samples = audio_samples(value['audio'])
        if float(np.sqrt(np.mean(samples * samples))) < .003: return {'detected': False}
        try: samples = trim(samples)
        except ValueError: return {'detected': False}
        features = extract_embedding_features(y=samples)
        distances = [float(distance(features, reference)) for reference in refs]
        best = min(distances)
        return {'detected': best < profile()['threshold'], 'distance': best}
    if op == 'gesture':
        frame = cv2.imdecode(np.frombuffer(base64.b64decode(value['image']), dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] > 1080 or frame.shape[1] > 1920:
            raise ValueError('HANDSFREE_IMAGE_INVALID')
        frame = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
        timestamp = max(last_frame + 1, int(time.monotonic() * 1000)); last_frame = timestamp
        result = recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=frame), timestamp)
        landmarks = [[p.x, p.y, p.z] for p in result.hand_landmarks[0]] if result.hand_landmarks else []
        category = result.gestures[0][0] if result.gestures and result.gestures[0] else None
        gesture = category.category_name if category and category.score >= .6 else ''
        events = gestures.update(landmarks, gesture, time.monotonic())
        return {'events': events, 'hand': bool(landmarks), 'gesture': gesture, 'landmarks': landmarks,
                'captured_at': value.get('captured_at')}
    raise ValueError('HANDSFREE_REQUEST_INVALID')


for line in sys.stdin:
    try:
        # ML libraries must never corrupt the stdout protocol.
        with contextlib.redirect_stdout(sys.stderr): result = handle(json.loads(line))
    except Exception as exc:
        result = {'error': str(exc) if str(exc).startswith('HANDSFREE_') else 'HANDSFREE_RECOGNITION_FAILED_' + type(exc).__name__}
    print(json.dumps(result), flush=True)
