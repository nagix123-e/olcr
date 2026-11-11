"""Memory-aware, role-specific model recommendations for the landing flow.

The shipped model registry remains the immutable 32GB baseline. This module
only describes opt-in profile choices and guards them with a conservative
unified-memory reserve before exposing them as recommended defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import platform
from pathlib import Path
import subprocess
from typing import Any


PROFILE_PATH = Path(__file__).with_name('runtime_recipes') / 'recommended-model-profiles.json'


@dataclass(frozen=True)
class HardwareProfile:
    memory_gb: int | None
    chip: str | None
    architecture: str
    source: str

    def public_dict(self) -> dict[str, Any]:
        return {'memory_gb': self.memory_gb, 'chip': self.chip,
                'architecture': self.architecture, 'source': self.source}


def detect_hardware() -> HardwareProfile:
    """Read Apple unified memory and chip name without failing app startup."""
    architecture = platform.machine() or 'unknown'
    memory_gb = None
    chip = None
    if platform.system() == 'Darwin':
        try:
            value = subprocess.run(['sysctl', '-n', 'hw.memsize'], check=True,
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            # sysctl reports physical bytes; Apple memory tiers are conventionally
            # advertised in GiB (16/32/64/128), so use the binary unit here.
            memory_gb = max(1, round(int(value) / (1024 ** 3)))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            value = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], check=True,
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            chip = value or None
        except (OSError, subprocess.SubprocessError):
            pass
    return HardwareProfile(memory_gb, chip, architecture,
                           'macOS sysctl' if memory_gb is not None or chip else 'unknown')


def tier_for_memory(memory_gb: int | None) -> str | None:
    if not isinstance(memory_gb, int) or memory_gb <= 0:
        return None
    for tier in (16, 32, 64, 128, 192):
        if memory_gb <= tier:
            return str(tier)
    return '256'


def _data() -> dict[str, Any]:
    try:
        value = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('MODEL_PROFILE_CATALOG_INVALID') from exc
    if not isinstance(value, dict) or not isinstance(value.get('models'), dict) or not isinstance(value.get('tiers'), list):
        raise RuntimeError('MODEL_PROFILE_CATALOG_INVALID')
    return value


class ModelProfileCatalog:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or _data()
        self.models = self.data['models']
        self.tiers = {str(row['id']): row for row in self.data['tiers'] if isinstance(row, dict) and row.get('id')}

    def resolve(self, profile_id: str) -> dict[str, str]:
        profile = self.tiers.get(str(profile_id))
        if not profile:
            raise ValueError('MODEL_PROFILE_UNKNOWN')
        return dict(profile['roles'])

    def fits(self, model_id: str, memory_gb: int | None, tier_id: str) -> bool:
        if memory_gb is None:
            return False
        model = self.models.get(model_id) or next(
            (row for row in self.models.values() if row.get('brain_id') == model_id), None)
        if not model:
            return False
        policy = self.data['memory_policy']
        reserve = int(policy['reserved_system_gb'].get(tier_id, 32))
        overhead = int(policy.get('runtime_overhead_gb', 4))
        weight = float(model.get('weight_gb', 0))
        return weight > 0 and weight + overhead + reserve <= memory_gb

    def for_device(self, memory_gb: int | None, chip: str | None = None,
                   installed_ids: set[str] | None = None, *, current: dict[str, Any] | None = None,
                   hardware: HardwareProfile | None = None) -> dict[str, Any]:
        installed_ids = installed_ids or set()
        selected_tier = tier_for_memory(memory_gb)
        if selected_tier is None:
            selected_tier = '32'
        profile = self.tiers[selected_tier]
        options: dict[str, list[dict[str, Any]]] = {}
        default_models = {**profile['brain'], **profile['roles'], **profile.get('audio', {})}
        for role, model_id in default_models.items():
            options.setdefault(role, [])
            compatible_ids = self._compatible_models(role, model_id)
            for candidate_id in compatible_ids:
                candidate = self.models.get(candidate_id) or next(
                    (row for row in self.models.values() if row.get('brain_id') == candidate_id), None)
                if candidate is None:
                    # Legacy manifest-backed assets are added by ModelDownloads.
                    continue
                download_id = candidate['id']
                fits = self.fits(candidate_id, memory_gb, selected_tier)
                verified = candidate.get('verified') is True
                options[role].append({**candidate, 'id': candidate_id, 'download_id': download_id,
                                      'downloaded': download_id in installed_ids,
                                      'fits_memory': fits, 'recommended': candidate_id == model_id,
                                      'available': verified and fits,
                                      'experimental': candidate.get('experimental') is True})
        proposed = {
            'profile_id': selected_tier,
            'brain': dict(profile['brain']),
            'roles': dict(profile['roles']),
            'audio': dict(profile.get('audio', {})),
        }
        if current:
            proposed['current'] = current
        detected = hardware or HardwareProfile(memory_gb, chip, platform.machine() or 'unknown', 'manual')
        return {
            'hardware': detected.public_dict(),
            'recommended_profile': selected_tier,
            'tiers': [{'id': key, 'label': value['label'], 'max_memory_gb': value.get('max_memory_gb')}
                      for key, value in self.tiers.items()],
            'profile': proposed,
            'options': options,
            'models': {model_id: {**model,
                                  'downloaded': model_id in installed_ids,
                                  'fits_memory': self.fits(model_id, memory_gb, selected_tier),
                                  'available': model.get('verified') is True and self.fits(model_id, memory_gb, selected_tier)}
                       for model_id, model in self.models.items()},
            'memory_policy': self.data['memory_policy'],
        }

    def _compatible_models(self, role: str, recommended: str) -> list[str]:
        if role in {'brain', 'coder', 'short_coding', 'general_inference', 'long_implementation', 'normal_chat'}:
            return [model['brain_id'] for model in self.models.values() if model.get('brain_id')]
        if role == 'translation':
            return [key for key in self.models if key.startswith('plamo_3_translate_')]
        if role in {'transcription_ja', 'transcription_en'}:
            return [role, *[key for key in self.models if key.startswith('whisper_')]]
        if role == 'read_aloud':
            return [recommended]
        return [recommended]


def normalize_selection(profile_id: str, role_models: dict[str, str] | None,
                        audio_models: dict[str, str] | None = None,
                        memory_gb: int | None = None,
                        installed_brain_sizes: dict[str, int] | None = None) -> dict[str, Any]:
    catalog = ModelProfileCatalog()
    profile = catalog.tiers.get(str(profile_id))
    if profile is None:
        raise ValueError('MODEL_PROFILE_UNKNOWN')
    selected = {'profile_id': str(profile_id), 'brain': dict(profile['brain']),
                'roles': dict(profile['roles']), 'audio': dict(profile.get('audio', {}))}
    if role_models is not None:
        if not isinstance(role_models, dict):
            raise ValueError('MODEL_PROFILE_SELECTION_INVALID')
        allowed = set(selected['brain']) | set(selected['roles'])
        if set(role_models) - allowed:
            raise ValueError('MODEL_PROFILE_ROLE_UNKNOWN')
        for role, model_id in role_models.items():
            if not isinstance(model_id, str) or not model_id:
                raise ValueError('MODEL_PROFILE_MODEL_INVALID')
            default_group = 'brain' if role in selected['brain'] else 'roles'
            is_curated_default = model_id == selected[default_group].get(role)
            # A tier may intentionally use a legacy manifest-backed default
            # that is downloaded outside this catalog. Keep that preset valid;
            # non-default choices still need a compatible or installed entry.
            options = catalog._compatible_models(role, selected['brain'].get(role) or selected['roles'].get(role) or '')
            known_installed = isinstance(installed_brain_sizes, dict) and model_id in installed_brain_sizes
            if model_id not in options and not known_installed and not is_curated_default:
                raise ValueError('MODEL_PROFILE_MODEL_INCOMPATIBLE')
            if (model_id in catalog.models or any(row.get('brain_id') == model_id for row in catalog.models.values())) and not catalog.fits(model_id, memory_gb, str(profile_id)):
                raise ValueError('MODEL_PROFILE_MODEL_DOES_NOT_FIT_MEMORY')
            if known_installed and model_id not in options:
                size = int(installed_brain_sizes[model_id] or 0)
                reserve = int(catalog.data['memory_policy']['reserved_system_gb'].get(str(profile_id), 32))
                overhead = int(catalog.data['memory_policy'].get('runtime_overhead_gb', 4))
                if memory_gb is None or size <= 0 or size / (1024 ** 3) + reserve + overhead > memory_gb:
                    raise ValueError('MODEL_PROFILE_MODEL_DOES_NOT_FIT_MEMORY')
            selected['brain' if role in selected['brain'] else 'roles'][role] = model_id
    if audio_models is not None:
        if not isinstance(audio_models, dict) or set(audio_models) - {'transcription_ja', 'transcription_en', 'read_aloud'}:
            raise ValueError('MODEL_PROFILE_AUDIO_INVALID')
        for role, model_id in audio_models.items():
            if not isinstance(model_id, str):
                raise ValueError('MODEL_PROFILE_AUDIO_INVALID')
            options = catalog._compatible_models(role, selected['audio'].get(role, ''))
            is_curated_default = model_id == selected['audio'].get(role)
            if model_id not in options and not is_curated_default:
                raise ValueError('MODEL_PROFILE_AUDIO_INCOMPATIBLE')
            if model_id in catalog.models and not catalog.fits(model_id, memory_gb, str(profile_id)):
                raise ValueError('MODEL_PROFILE_MODEL_DOES_NOT_FIT_MEMORY')
            selected['audio'][role] = model_id
    return selected
