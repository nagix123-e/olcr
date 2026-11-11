"""Text-only symptom interviews on OLCR's ordinary chat message path."""
from __future__ import annotations

import copy
import json
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .symptom_consultation import AssessmentInput, SearchInput, MAX_QUESTIONS, urgent

STATE_BLOCK = 'symptom_consultation_state'
SEEDS = [('頭痛|頭(?:が)?痛|headache', '頭痛'), ('腹痛|お腹(?:が)?痛', '腹痛'),
         ('胸痛|胸(?:が)?痛', '胸痛'), ('発熱|熱(?:が)?ある', '発熱'), ('咳', '咳'),
         ('めまい', 'めまい'), ('吐き気', '吐き気'), ('息苦し|息切れ', '息切れ')]
NON_MEDICAL = re.compile(r'コード|実装|API|設計|テスト|小説|翻訳|天気|株価|プログラム|code|implement', re.I)
SAFETY = ('突然の激しい痛み、強い胸痛・息苦しさ、意識の異常、手足の麻痺などがある場合は、'
          '問診を待たず救急医療へ相談してください。')


def symptom_request(message: str) -> bool:
    if message.lstrip().startswith('/') or NON_MEDICAL.search(message):
        return False
    return any(re.search(pattern, message, re.I) for pattern, _ in SEEDS) and bool(re.search(
        r'症状相談|がある|があります|がする|がします|が痛|頭痛が続|今.{0,60}(?:痛|熱|咳)|'
        r'何(?:が疑われ|の病気)|I (?:have|am)', message, re.I))


def _turn(response, state=None):
    return {'response': response, 'state': state}


def _clean(message):
    return re.sub(r'[\s。．.!！?？]+', '', message).lower()


def _choice(message):
    value = _clean(message)
    if value in {'はい', 'ある', 'あります', 'あり', 'yes'}:
        return 'present'
    if value in {'いいえ', 'ない', 'ありません', 'なし', 'no'}:
        return 'absent'
    if value in {'分からない', 'わからない', '不明', '判断できない', 'unknown'}:
        return 'unknown'
    return None


def render_result(result):
    if result['status'] == 'urgent':
        return result['advice']
    lines = []
    urgency = result.get('urgency')
    if urgency == 'consultation_24':
        lines.append('24時間以内に医療機関を受診してください。悪化した場合は救急医療へ相談してください。')
    if result['status'] != 'complete':
        lines.append(result.get('advice') or '情報が不足しているため候補を絞れません。医療機関に相談してください。')
        return '\n\n'.join(lines)
    lines.append('考えられる原因の候補です。入力した症状と評価モデルとの一致度による参考順で、確定診断ではありません。')
    lines.append('\n'.join(f"{i + 1}. {item['name']}" for i, item in enumerate(result['possible_causes'][:5])))
    if urgency == 'consultation':
        lines.append('医療機関への相談をおすすめします。')
    if result.get('department'):
        lines.append('相談する診療科：' + result['department'])
    if result.get('self_care'):
        lines.append('軽い対処の目安：\n' + '\n'.join('- ' + item for item in result['self_care']))
    lines.append('症状が続く・悪化する、または新しい症状が出た場合は医療機関に相談してください。')
    lines.append('評価：Infermedica。参考：[OpenSymptomの症状解説（英語）](https://www.opensymptom.com/)。')
    return '\n\n'.join(lines)


class SymptomChat:
    def __init__(self, operations):
        self.operations = operations

    def turn(self, message: str, previous: dict | None = None):
        if message.lstrip().startswith('/') or NON_MEDICAL.search(message):
            return None
        if previous and re.search(r'(?:外部送信|送信|外部アクセス)(?:を)?(?:しない|やめ|止め|禁止)', message):
            return _turn('外部送信を行わず、症状相談を終了しました。')
        if _clean(message) in {'中止', 'やめる', '相談をやめる', 'キャンセル', '通常の会話に戻る'} and previous:
            return _turn('症状相談を終了しました。')
        state = copy.deepcopy(previous) if previous else None
        if state and _clean(message) in {'緊急症状がある', '緊急の症状がある', '救急車が必要'}:
            return _turn(urgent(state['patient'].get('country', 'other'))['advice'])
        if state and state.get('expires_at', 0) <= time.time():
            return _turn('症状相談の期限が切れました。症状をもう一度教えてください。' + SAFETY)
        if not state:
            if not symptom_request(message):
                return None
            if not self.operations.status().get('available'):
                return _turn('症状評価サービスは未設定、または外部アクセスが無効です。原因の順位付けには日本語対応のInfermedica契約とOLCR側の接続設定が必要です。\n\n' + SAFETY)
            seed = next(label for pattern, label in SEEDS if re.search(pattern, message, re.I))
            state = {'stage': 'emergency', 'seed': seed, 'interview_id': str(uuid4()),
                     'expires_at': time.time() + 1800, 'evidence': [], 'question_count': 0,
                     'patient': {'language': 'ja'}}
            # No provider call has been made; status() only checks configuration.
            return _turn('原因の候補を絞るため、まず緊急性を確認します。\n\n' + SAFETY
                         + '\n\nこのような症状はありますか？「ある」「ない」「分からない」で教えてください。', state)
        try:
            return self._continue(message, state)
        except (HTTPException, ValidationError):
            return _turn('評価サービスを利用できませんでした。接続設定・契約の対応範囲を確認してください。'
                         '「再試行」でやり直せます。' + SAFETY, state)

    def _continue(self, message, state):
        stage, patient = state['stage'], state['patient']
        if stage == 'emergency':
            choice = _choice(message)
            if choice == 'present':
                return _turn(urgent(patient.get('country', 'other'))['advice'])
            if choice == 'unknown':
                return _turn('ここでは緊急性を判断できません。医療機関に相談してください。' + SAFETY)
            if choice != 'absent':
                return _turn('先ほどの緊急症状について、「ある」「ない」「分からない」のいずれかで教えてください。', state)
            patient['emergency'] = 'absent'
            state['stage'] = 'intake'
            return _turn('年齢、評価に用いる性別、現在いる国と、妊娠中・産後・免疫抑制状態に該当するか教えてください。'
                         '\n例：30歳、女性、日本、該当なし。評価サービスは18〜64歳・女性/男性の区分に対応します。', state)
        if stage == 'intake':
            age = re.search(r'(?<!\d)(\d{1,3})\s*[歳才]', message)
            if age or message.strip().isdigit():
                patient['age'] = int(age[1] if age else message.strip())
            if '女性' in message or re.search(r'\bfemale\b', message, re.I):
                patient['sex'] = 'female'
            elif '男性' in message or re.search(r'\bmale\b', message, re.I):
                patient['sex'] = 'male'
            if re.search(r'日本以外|海外|米国|アメリカ|カナダ|英国|中国|韓国', message):
                patient['country'] = 'other'
            elif '日本' in message:
                patient['country'] = 'JP'
            if re.search(r'該当なし|該当しない|いずれもない', message):
                patient['high_risk'] = False
            elif re.search(r'該当する|妊娠中です|産後です|免疫抑制状態です', message):
                patient['high_risk'] = True
            if ('age' in patient and not 18 <= patient['age'] <= 64) or patient.get('high_risk'):
                return _turn('この試験機能の対象外です。担当の医療機関に直接相談してください。')
            missing = [name for key, name in [('age', '年齢（例：30歳）'), ('sex', '評価に用いる性別（女性/男性）'),
                        ('country', '現在いる国'), ('high_risk', '妊娠中・産後・免疫抑制状態の該当有無（該当なし/該当する）')] if key not in patient]
            if missing:
                return _turn('追加で教えてください：' + '、'.join(missing) + '。', state)
            state['stage'] = 'consent'
            return _turn('症状名・選択した症状と回答・年齢・性別をInfermedicaに送信して評価します。'
                         '送信先での取扱いは提供元の契約に従います。送信に同意する場合は「同意します」、'
                         'しない場合は「同意しない」と返信してください。', state)
        if stage == 'consent':
            if _clean(message) in {'同意しない', '同意しません', 'いいえ', 'no'}:
                return _turn('外部送信を行わず、症状相談を終了しました。')
            retry = _clean(message) == '再試行' and patient.get('consent') is True
            if not retry and _clean(message) not in {'同意します', '同意する', '同意', 'はい', 'yes'}:
                return _turn('外部送信に「同意します」または「同意しない」で回答してください。', state)
            patient['consent'] = True
            data = self.operations.search(SearchInput(**patient, interview_id=state['interview_id'], phrase=state['seed']))
            state['items'] = data.get('items', [])
            if not state['items']:
                return _turn('一致する症状が見つからず、原因を絞れません。医療機関に相談してください。')
            state['stage'] = 'select'
            return _turn('現在ある症状に近い項目を番号で教えてください。複数ある場合は「1、2」のように返信できます。'
                         '\n' + '\n'.join(f"{i+1}. {item['label']}" for i, item in enumerate(state['items']))
                         + '\n該当する項目がなければ「なし」と教えてください。', state)
        if stage == 'select':
            if _clean(message) == 'なし':
                return _turn('症状を特定できないため、候補を絞れません。医療機関に相談してください。')
            numbers = self._numbers(message, len(state['items']))
            if not numbers:
                return _turn('現在ある症状の項目番号を教えてください（例：1、2）。中止もできます。', state)
            state['evidence'] = [{'id': state['items'][number-1]['id'], 'choice_id': 'present', 'source': 'initial'} for number in numbers]
            state['stage'] = 'assess'
        elif stage == 'question':
            question = state['question']
            incoming = self._answers(message, question)
            if incoming is None:
                return _turn(self._question_text(question), state)
            # Mutually exclusive groups must not retain a formerly selected sibling.
            items = {item['id'] for item in question['items']}
            old = {item['id']: item for item in state['evidence']}
            for entry in incoming:
                if old.get(entry['id'], {}).get('source') == 'initial':
                    entry['source'] = 'initial'
            if question['type'] == 'group_single':
                old = {key: value for key, value in old.items() if key not in items}
            old.update({item['id']: item for item in incoming})
            state['evidence'] = list(old.values())
            state['question_count'] += 1
            state['stage'] = 'assess'
        elif stage != 'assess':
            return _turn('症状をもう一度教えてください。')
        result = self.operations.assess(AssessmentInput(**patient, interview_id=state['interview_id'],
            complaint='headache' if state['seed'] == '頭痛' else 'other', evidence=state['evidence'],
            question_count=state['question_count']))
        if result['status'] == 'questioning' and result.get('question'):
            if state['question_count'] >= MAX_QUESTIONS:
                return _turn('追加の確認を重ねても情報が不足しているため候補を絞れません。医療機関に相談してください。')
            state['question'] = result['question']
            state['stage'] = 'question'
            warning = ('24時間以内に医療機関を受診してください。\n\n' if result['urgency'] == 'consultation_24' else '')
            return _turn(warning + self._question_text(result['question']), state)
        return _turn(render_result(result))

    @staticmethod
    def _numbers(message, maximum):
        if not re.fullmatch(r'\s*\d+(?:[\s,、，]+\d+)*\s*', message):
            return []
        numbers = list(dict.fromkeys(int(value) for value in re.findall(r'\d+', message)))
        return numbers if numbers and all(1 <= number <= maximum for number in numbers) else []

    def _answers(self, message, question):
        items = question['items']
        if question['type'] == 'single':
            choice = _choice(message)
            return [{'id': items[0]['id'], 'choice_id': choice}] if choice else None
        if question['type'] == 'group_single':
            if _choice(message) == 'unknown':
                return [{'id': item['id'], 'choice_id': 'unknown'} for item in items]
            numbers = self._numbers(message, len(items))
            return [{'id': items[numbers[0]-1]['id'], 'choice_id': 'present'}] if len(numbers) == 1 else None
        if _choice(message) == 'unknown':
            return [{'id': item['id'], 'choice_id': 'unknown'} for item in items]
        pairs = re.findall(r'(\d+)\s*[:：]?\s*(分からない|わからない|不明|いいえ|はい)', message)
        if len(pairs) != len(items) or {int(n) for n, _ in pairs} != set(range(1, len(items)+1)):
            return None
        return [{'id': items[int(n)-1]['id'], 'choice_id': _choice(answer)} for n, answer in pairs]

    @staticmethod
    def _question_text(question):
        if question['type'] == 'single':
            return question['text'] + '\n「はい」「いいえ」「分からない」で教えてください。'
        instructions = ('1つの項目番号、または「分からない」で教えてください。'
                        if question['type'] == 'group_single' else '各項目を「1はい、2いいえ、3不明」のように教えてください。')
        return question['text'] + '\n' + '\n'.join(f"{i+1}. {item['name']}" for i, item in enumerate(question['items'])) + '\n' + instructions


def respond_in_chat(db, value, resolve_context, service: SymptomChat):
    if getattr(value, 'resume_control', None) or getattr(value, 'resume_task_id', None):
        return None
    existing = db.conversation(value.conversation_id) if value.conversation_id else None
    if existing and existing.get('custom_agent'):
        return None
    messages = (existing or {}).get('messages', [])
    source_id = value.message_id or str(uuid4())
    duplicate = None
    # Duplicate client deliveries must not create another turn or repeat remote calls.
    for item in messages:
        if item.get('role') == 'assistant' and any(block.get('type') == STATE_BLOCK
                and block.get('source_message_id') == source_id for block in item.get('blocks') or []):
            duplicate = item
            break
    last = messages[-1] if messages else {}
    state = next((block.get('state') for block in last.get('blocks') or [] if block.get('type') == STATE_BLOCK), None)
    if not duplicate and not state and not symptom_request(value.message):
        return None
    # Validate conversation/project identity before interpreting persisted state.
    conversation_id, _, _ = resolve_context(value)
    if duplicate:
        return {'conversation_id': conversation_id, 'response': duplicate['content'], 'blocks': [], 'sources': []}
    turn = service.turn(value.message, state)
    if turn is None:
        return None
    if not any(item.get('id') == source_id for item in messages):
        db.add_message(conversation_id, 'user', value.message, time.time(), source_id)
    db.add_message(conversation_id, 'assistant', turn['response'], time.time(), str(uuid4()),
                   blocks=[{'type': STATE_BLOCK, 'source_message_id': source_id, 'state': turn['state']}])
    return {'conversation_id': conversation_id, 'response': turn['response'], 'blocks': [], 'sources': []}


def chat_events(result):
    for event in ({'type': 'meta', 'conversation_id': result['conversation_id'], 'task_id': None},
                  {'type': 'chunk', 'text': result['response']},
                  {'type': 'done', 'conversation_id': result['conversation_id'], 'response_owner': 'SYMPTOM_CONSULTATION'}):
        yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
