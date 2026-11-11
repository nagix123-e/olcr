"""Stateless symptom interviews, separate from chat history and model routing.

No clinical ranking is generated locally. The enabled provider supplies medical
content; this experimental integration still requires clinical acceptance review.
"""
from __future__ import annotations

import os
from typing import Annotated, Literal, NamedTuple, Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, TypeAdapter, field_validator

from .symptom_provider import SymptomProviderError, configured_provider

Choice = Literal['present', 'absent', 'unknown']
ConceptId = Annotated[str, Field(pattern=r'^(s|p)_\d+$', max_length=40)]
MAX_QUESTIONS = 6
REFERENCE = 'https://www.opensymptom.com/'


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def private_handler(request: Request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default errors echo invalid input, including health details.
                response = JSONResponse({'detail': '症状相談の入力を確認してください。'}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return private_handler


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Patient(PrivateModel):
    consent: StrictBool
    age: Annotated[StrictInt, Field(ge=18, le=64)]
    sex: Literal['female', 'male']
    country: Literal['JP', 'other']
    emergency: Choice
    high_risk: StrictBool
    interview_id: UUID
    language: Literal['ja', 'en'] = 'ja'

    @field_validator('consent')
    @classmethod
    def consent_required(cls, value):
        if not value:
            raise ValueError('Consent required')
        return value


class SearchInput(Patient):
    phrase: Annotated[str, Field(min_length=1, max_length=120)]


class Evidence(PrivateModel):
    id: ConceptId
    choice_id: Choice
    source: Literal['initial'] | None = None


class AssessmentInput(Patient):
    complaint: Literal['headache', 'other'] = 'other'
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=100)]
    question_count: Annotated[StrictInt, Field(ge=0, le=MAX_QUESTIONS)]
    finish: StrictBool = False

    @field_validator('evidence')
    @classmethod
    def unique_evidence(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError('Duplicate observations')
        return value


class SearchItem(BaseModel):
    id: ConceptId
    label: Annotated[str, Field(min_length=1, max_length=500)]


class QuestionItem(BaseModel):
    id: ConceptId
    name: Annotated[str, Field(min_length=1, max_length=1000)]


class Question(BaseModel):
    type: Literal['single', 'group_single', 'group_multiple']
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    items: Annotated[list[QuestionItem], Field(min_length=1, max_length=30)]


class Condition(BaseModel):
    id: Annotated[str, Field(pattern=r'^c_\d+$')]
    name: str = ''
    common_name: str = ''
    probability: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Diagnosis(BaseModel):
    should_stop: StrictBool
    has_emergency_evidence: StrictBool
    conditions: Annotated[list[Condition], Field(max_length=100)]
    question: Question | None = None


LEVELS = {'emergency_ambulance': 4, 'emergency': 3, 'consultation_24': 2,
          'consultation': 1, 'self_care': 0}
SPECIALTIES = {'sp_1': '総合診療科', 'sp_2': '内科', 'sp_17': '脳神経内科',
               'sp_14': '耳鼻咽喉科', 'sp_7': '眼科', 'sp_9': '皮膚科', 'sp_12': '循環器内科'}


def result(status: str, *, urgency='undetermined', advice='', **fields):
    return {'status': status, 'urgency': urgency, 'advice': advice, 'question': None,
            'possible_causes': [], 'department': None, 'self_care': [],
            'reference_url': REFERENCE, 'provider': 'Infermedica', **fields}


def urgent(country: str):
    return result('urgent', urgency='emergency', advice=(
        '問診を続けず、すぐに救急医療へ相談してください。日本では119へ連絡できます。'
        if country == 'JP' else '問診を続けず、現地の救急番号または救急医療へすぐに連絡してください。'))


def preflight(value: Patient):
    if value.emergency == 'present':
        return urgent(value.country)
    if value.emergency == 'unknown':
        return result('insufficient', advice='緊急性を判断できません。症状が強い・急に悪化した場合は救急医療へ、それ以外も医療機関に相談してください。')
    if value.high_risk:
        return result('out_of_scope', advice='この試験機能の対象外です。妊娠中・産後や免疫抑制状態では、担当の医療機関に直接相談してください。')
    return None


class ConsultationHandlers(NamedTuple):
    status: Callable[[], dict]
    search: Callable[[SearchInput], dict]
    assess: Callable[[AssessmentInput], dict]


def consultation_handlers(get_settings, get_provider=configured_provider):

    def availability():
        enabled = os.environ.get('OLCR_SYMPTOM_ENABLED') == '1'
        external = bool(get_settings().external_access_enabled)
        provider = get_provider() if enabled and external else None
        return enabled, external, provider

    def ready():
        enabled, external, provider = availability()
        if not external:
            raise HTTPException(403, '外部アクセスが無効です。')
        if not enabled or provider is None:
            raise HTTPException(503, '症状相談は未設定です。日本語対応のInfermedica契約と接続設定が必要です。')
        return provider

    def call(provider, endpoint, payload, value):
        try:
            return provider.request(endpoint, payload, interview_id=str(value.interview_id), language=value.language)
        except SymptomProviderError:
            raise HTTPException(502, '評価サービスを利用できません。接続設定・契約の対応範囲を確認してください。') from None

    def status():
        enabled, external, provider = availability()
        return {'enabled': enabled, 'external_access': external, 'configured': provider is not None,
                'available': enabled and external and provider is not None, 'provider': 'Infermedica',
                'reference_url': REFERENCE, 'max_questions': MAX_QUESTIONS}

    def search(value: SearchInput):
        if early := preflight(value):
            return {**early, 'items': []}
        provider = ready()
        data = call(provider, 'search', {'phrase': value.phrase, 'age.value': value.age,
                    'sex': value.sex, 'types': 'symptom,risk_factor', 'include_pro': 'false'}, value)
        try:
            items = TypeAdapter(list[SearchItem]).validate_python(data)
        except ValueError:
            raise HTTPException(502, '症状検索の応答を確認できませんでした。') from None
        return {'items': [item.model_dump() for item in items[:12]]}

    def assess(value: AssessmentInput):
        if early := preflight(value):
            return early
        provider = ready()
        if not any(e.id.startswith('s_') and e.choice_id == 'present' and e.source == 'initial'
                   for e in value.evidence):
            return result('insufficient', advice='現在ある症状を確認して選択してください。')
        payload = {'sex': value.sex, 'age': {'value': value.age},
                   'evidence': [e.model_dump(exclude_none=True) for e in value.evidence]}
        raw = call(provider, 'diagnosis', payload, value)
        # Recognize emergency evidence even when the rest of a response is malformed.
        if isinstance(raw, dict) and raw.get('has_emergency_evidence') is True:
            return urgent(value.country)
        triage = call(provider, 'triage', payload, value)
        if not isinstance(triage, dict) or triage.get('triage_level') not in LEVELS:
            raise HTTPException(502, '受診の緊急度を確認できませんでした。')
        urgency = triage['triage_level']
        serious = triage.get('serious', [])
        if not isinstance(serious, list) or any(not isinstance(item, dict) for item in serious):
            raise HTTPException(502, '受診の緊急度を確認できませんでした。')
        for item in serious:
            level = item.get('seriousness')
            if level not in {'serious', 'emergency', 'emergency_ambulance'}:
                raise HTTPException(502, '受診の緊急度を確認できませんでした。')
            mapped = 'consultation_24' if level == 'serious' else level
            if mapped in LEVELS and LEVELS[mapped] > LEVELS[urgency]:
                urgency = mapped
        if LEVELS[urgency] >= 3:
            return urgent(value.country)
        if triage.get('root_cause') == 'diagnosis_unknown':
            return result('insufficient', advice='評価サービスで原因や緊急度を判断できません。医療機関に相談してください。')
        try:
            diagnosis = Diagnosis.model_validate(raw)
        except ValueError:
            raise HTTPException(502, '評価サービスの応答を確認できませんでした。') from None
        if not diagnosis.should_stop:
            if value.finish or value.question_count >= MAX_QUESTIONS or not diagnosis.question:
                return result('insufficient', urgency=urgency,
                              advice='情報が不足しているため候補を絞れません。医療機関に相談してください。')
            return result('questioning', urgency=urgency, question=diagnosis.question.model_dump(),
                          advice='候補を絞るため、追加で確認します。')
        if not diagnosis.conditions:
            return result('insufficient', urgency=urgency, advice='原因の候補を特定できません。医療機関に相談してください。')
        department = '総合診療科・内科（専門科の推奨は取得できませんでした）'
        try:
            specialist = call(provider, 'recommend_specialist', payload, value)
            recommendation = specialist.get('recommended_specialist') if isinstance(specialist, dict) else None
            if isinstance(recommendation, dict):
                department = SPECIALTIES.get(recommendation.get('id')) or str(recommendation.get('name') or department)[:200]
        except HTTPException:
            pass  # Optional licensed endpoint; never discard an already valid triage result.
        candidates = sorted(diagnosis.conditions, key=lambda c: c.probability, reverse=True)[:5]
        # General headache measures adapted from NHS /symptoms/headaches/.
        # Never show these as an alternative to urgent assessment or medication advice.
        self_care = []
        if urgency == 'self_care' and value.complaint == 'headache':
            self_care = ['無理をせず、リラックスして過ごしてください。',
                         '水分不足を避けてください。医師から水分制限を指示されている場合は、その指示に従ってください。',
                         '画面を長時間見続けることや飲酒を避け、食事を抜かないようにしてください。']
        return result('complete', urgency=urgency, department=department,
                      self_care=self_care,
                      advice='入力した症状と評価モデルとの一致度による参考順です。確定診断ではありません。',
                      possible_causes=[{'id': c.id, 'name': (c.common_name or c.name)[:500], 'rank': i + 1}
                                       for i, c in enumerate(candidates)])

    return ConsultationHandlers(status, search, assess)


def consultation_router(get_settings, get_provider=configured_provider):
    operations = consultation_handlers(get_settings, get_provider)
    router = APIRouter(prefix='/api/symptom-consultations', route_class=PrivateRoute)
    router.add_api_route('/status', operations.status, methods=['GET'])
    router.add_api_route('/search', operations.search, methods=['POST'])
    router.add_api_route('/assess', operations.assess, methods=['POST'])
    return router
