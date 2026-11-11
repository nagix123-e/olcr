"""Choose planning depth from the user's definition, independently of execution size."""
from __future__ import annotations

import re
from typing import Any

from .coding_tasks import _current_task_text, verification_requested_by_user


def coherent_static_application(goal: str, requirements: dict[str, Any], task_size: str) -> bool:
    """Keep a bounded offline app's producer/consumer API in one generation."""
    text=_current_task_text(goal)
    return bool(task_size!='LARGE' and requirements.get('new_project_intent')
        and requirements.get('mutation_mode')!='FIX'
        and all(re.search(r'(?<![A-Za-z0-9_])'+name+r'(?![A-Za-z0-9_])',text,re.I)
                for name in ('HTML','CSS','JavaScript'))
        and re.search(r'外部ライブラリ.{0,80}(?:使わ|不要)|\b(?:offline|no external libraries|without external libraries)\b',text,re.I)
        and not requirements.get('dependency_requirements'))


def planning_policy(goal: str, requirements: dict[str, Any], task_size: str) -> dict[str, Any]:
    # Only original human instructions and subsequent human answers contribute.
    text = _current_task_text(goal) + '\n' + '\n'.join(requirements.get('definition_answers') or [])
    product = bool(re.search(r'アプリ|サイト|サービス|システム|\b(?:app(?:lication)?|website|platform|system)\b', text, re.I))
    broad = product or bool(re.search(r'いい感じ|全体を改善|全体を整理|リファクタリングして|make it better|improve everything', text, re.I))
    target = bool(re.search(r'\b[\w./-]+\.(?:html|css|[cm]?js|tsx?|jsx|py|rs|go|swift|json)\b', text, re.I))
    behavior_patterns = {
        'input': r'入力|\binput\b',
        'output': r'表示|一覧|通知|\b(?:display|list|notify)\b',
        'selection': r'選[ぶ択]|抽選|並[びべ]|\b(?:select|choose|random|sort)\b',
        'persistence': r'保存|履歴|\b(?:save|history|persist)\b',
        'search': r'検索|\bsearch\b',
        'mutation': r'追加|削除|変更|\b(?:add|delete|remove|update)\b',
        'account': r'認証|ログイン|\b(?:login|auth)\b',
        'transaction': r'計算|購入|決済|\b(?:calculate|checkout|payment)\b',
    }
    behaviors = [name for name, pattern in behavior_patterns.items() if re.search(pattern, text, re.I)]
    stack = bool(requirements.get('required_stack') or re.search(
        r'HTML|CSS|JavaScript|TypeScript|React|Vue|Swift|Python|Rust|Node|SQLite|PostgreSQL', text, re.I))
    scope = target or bool(re.search(r'このプロジェクト|このアプリ|ローカル|オフライン|外部.*(?:使わ|不要)|新規|既存|空の|対象外|のみ'
                                    r'|\b(?:local|offline|existing|empty|without|only|scope)\b', text, re.I))
    acceptance = bool(requirements.get('required_verification') or re.search(
        r'テスト|node:test|完了条件|期待|エラー|空.*場合|直近\s*\d|\b(?:tests?|acceptance|expected|error)\b', text, re.I))
    delegated = bool(re.search(r'お[ま任]かせ|任せ[るま]|おすすめで|適宜決め|詳細.*決めて'
                               r'|\b(?:use (?:your|sensible) defaults|you decide|choose the details)\b', text, re.I))
    dimensions = {'behavior': bool(behaviors), 'stack': stack, 'scope': scope, 'acceptance': acceptance}
    # A focused existing-code repair can obtain its missing facts by inspection.
    # Product-wide requests need more definition as their implementation grows.
    repair = requirements.get('mutation_mode') == 'FIX' and (target or requirements.get('visual_repair'))
    sufficient = (repair or not broad or
                  (len(behaviors) >= 1 and sum(dimensions.values()) >= 2 and task_size != 'LARGE') or
                  (len(behaviors) >= 3 and sum(dimensions.values()) >= 3))
    questions = []
    if not sufficient and not delegated:
        if not behaviors:
            questions.append('主な画面と、ユーザーができるようにしたい操作を教えてください。')
        elif len(behaviors) < 3 and task_size == 'LARGE':
            questions.append('今回実装する主要機能と、後回しにする機能を教えてください。')
        if not scope:
            questions.append('今回の対象範囲を教えてください（新規か既存か、ローカルだけか、サーバーや外部連携も含むか）。')
        if task_size == 'LARGE' and not acceptance:
            questions.append('完成と判断する動作や、確認したい条件を教えてください。')
    return {'mode': 'CLARIFY' if questions else 'LEAN' if sufficient else 'DELEGATED_DESIGN',
            'coherent_static_application':coherent_static_application(goal,requirements,task_size),
            'definition_sufficient': bool(sufficient), 'task_size': task_size,
            'definition_dimensions': dimensions, 'behavior_count': len(behaviors),
            'questions': questions[:3]}


def planning_instruction(policy: dict[str, Any]) -> str:
    if policy['mode'] == 'LEAN':
        return ('\n[PLANNING_DEPTH: SPECIFICATION_TRANSLATION]\n'
                'The human definition is sufficient for this implementation size. Translate it directly into a short executable graph; '
                'do not rediscover requirements, expand the product, create a separate design session, or add planning/review/report phases. '
                'Use concrete deliverables and observable done criteria, rather than restating the entire request in one phase. '
                'Preserve user-defined UI, logic, and test-code boundaries. All supplied behaviors and constraints remain binding. '
                'Any explicit plan-before-approval instruction still applies.\n')
    return ('\n[PLANNING_DEPTH: DELEGATED_DESIGN]\n'
            'The user explicitly delegated unspecified details. Choose reasonable defaults, record important assumptions, '
            'and produce coherent deliverable steps without asking the user to repeat that delegation.\n')


def lean_plan_prompt(goal: str, requirements: dict[str, Any], task_size: str) -> str:
    """Avoid duplicate graph generation and design expansion for a supplied specification."""
    coherent=coherent_static_application(goal,requirements,task_size)
    verification_requested=verification_requested_by_user(goal,requirements)
    count = '1-2' if coherent else '2-4' if task_size == 'LARGE' else '1-3'
    focused = ('This is a focused FIX: inspect the existing target and make the smallest patch; no refactor or new architecture. '
               if requirements.get('mutation_mode') == 'FIX' else '')
    integration=('This is a bounded standalone offline app. Put its production UI and shared logic in ONE coherent implementation phase, '
                 'keeping their requested separate source files. Define their browser and Node loading strategy together. '
                 'Create test files in that same implementation phase only when requested as deliverables, using the actual produced API. '
                 'Do not generate the logic, its tests and its UI consumer as three independently designed APIs. '
                 if coherent else '')
    verification=('The human explicitly requests verification. Include only the requested checks in the smallest coherent phase; do not add unrelated acceptance suites. '
                  if verification_requested else
                  'DEFAULT_VERIFICATION=OFF. Do not create a test/build/typecheck/browser phase or routine checks. Use verify:[] for implementation phases. Feature behavior is an implementation requirement, not a request to run tests. Create test files only when requested as deliverables, and do not run them unless requested. ')
    return ('Return JSON only for a short read-only execution plan translating the human definition. ' + focused + integration +
            f'Use {count} coherent deliverable phases. Declare separate UI and logic files when requested; their work may share a coherent phase. '
            + verification +
            'Create requested test files in the same implementation phase as the source they exercise unless the human explicitly requests a separate test phase; a separate test file is fine. '
            'Do not add planning, review, report, or verification-only phases unless the human explicitly requests verification. '
            'Do not add inspection, planning, review, report, commit or push phases. The host handles Git review separately. '
            'Use concrete observable done criteria covering every specified behavior and constraint. '
            'Do not output tasks: the host derives the task graph from phase dependencies. '
            'Output {schema_version:1,original_goal:<exact goal>,scope:{allowed:[relative paths],forbidden:[strings]},assumptions:[], '
            'phases:[{id,goal,status:"pending",done:[strings],verify:[strings],dependencies:[phase IDs],risks:[], '
            'execution_mode:"IMPLEMENTATION|IMPLEMENTATION_AND_VERIFICATION|VERIFICATION_ONLY"}], '
            'file_manifest:[{path,action:"create|modify|delete",role,required:true,owner_phase}],max_retries_per_phase:2,requires_user_approval:true}. '
            'The approval field is schema metadata; the host honors explicit human approval requirements. '
            'List every mutable file in file_manifest; use create only for absent files, modify/delete only for existing files in the supplied workspace snapshot. '
            'Preserve canonical stack and authorized mutation scope. Never guess existing paths or promote read-only evidence into mutable targets. '
            'VERIFICATION_ONLY must not create files or install dependencies. For installation, set requires_dependency_installation:true; '
            'set package_json_config_mutation:true only for requested non-install configuration changes. '
            'For bounded read-only verification, supply exact argv in verification_command and complete verification_targets; '
            'omit executable commands if unknown. No shell wrappers, network calls, pipes or write flags. '
            'Honor explicit phase-failure stop instructions via checkpoint_on_failure:true. Goal: ' + goal)


def separate_ui_logic_requested(goal: str) -> bool:
    return bool(re.search(r'(?:画面|UI).{0,40}(?:ロジック|logic).{0,30}(?:分け|分離|separat)'
                          r'|\bseparat\w*.{0,40}(?:UI|screen).{0,40}logic', goal, re.I | re.S))


def validate_definition_steps(plan: Any, goal: str) -> list[str]:
    if not isinstance(plan, dict):
        return []
    phases=[phase for phase in plan.get('phases') or [] if isinstance(phase,dict)]
    entries=[entry for entry in plan.get('file_manifest') or [] if isinstance(entry,dict)]
    explicit_test_phase=bool(re.search(
        r'(?:separate|distinct).{0,40}(?:test|tests)|(?:test|tests).{0,40}(?:separate|distinct)\s+phase|'
        r'テスト.{0,24}(?:別(?:の)?工程|別(?:の)?フェーズ|分けて実装)',goal,re.I))

    def is_test_artifact(path: str) -> bool:
        normalized=path.replace('\\','/')
        name=normalized.rsplit('/',1)[-1].lower()
        return (any(part.lower() in {'test','tests','__tests__'} for part in normalized.split('/'))
                or name.startswith('test_') or name.endswith('_test.py')
                or bool(re.search(r'\.(?:test|spec)\.[^.]+$',name)))

    if not explicit_test_phase:
        files_by_phase={phase.get('id'):[] for phase in phases}
        for entry in entries:
            owner=entry.get('owner_phase')
            if owner in files_by_phase and isinstance(entry.get('path'),str):
                files_by_phase[owner].append(entry['path'])
        implementation=[phase for phase in phases if phase.get('execution_mode')!='VERIFICATION_ONLY'
                        and phase.get('kind')!='ORCHESTRATOR_BLUEPRINT']
        producer_phases={phase.get('id') for phase in implementation
                         if any(not is_test_artifact(path) for path in files_by_phase.get(phase.get('id'),[]))}
        for phase in implementation:
            owned=files_by_phase.get(phase.get('id'),[])
            has_tests=any(is_test_artifact(path) for path in owned)
            has_source=any(not is_test_artifact(path) for path in owned)
            if has_tests and not has_source and producer_phases-{phase.get('id')}:
                return ['Create test code in the same implementation phase as the source it exercises; do not add a standalone test-only implementation phase. A separate test file is permitted.']

    if not separate_ui_logic_requested(goal):
        return []
    # Separating screen and logic is a source boundary, not an instruction to
    # add orchestration phases. A concise plan can declare that boundary in
    # its file manifest or concrete scope paths.
    declared={str(entry.get('path') or ''):str(entry.get('role') or '') for entry in entries}
    for path in (plan.get('scope') or {}).get('allowed') or []:
        if isinstance(path,str):declared.setdefault(path,'')
    ui_files={path for path,role in declared.items() if re.search(r'\.(?:html?|css)$',path,re.I)
              or re.search(r'画面|\bUI\b|interface|view',role,re.I)}
    logic_files={path for path,role in declared.items()
        if not re.search(r'(?:^|[/.])(?:test|tests|spec)(?:[/.])|(?:^|/)test_|\.(?:test|spec)\.',path,re.I)
        and (re.search(r'ロジック|logic|algorithm|抽選|domain',role,re.I)
             or re.search(r'\.(?:[cm]?js|tsx?)$',path,re.I)
             and (not role or re.search(r'logic|algorithm|lottery|roulette|core|engine|domain|model',path,re.I)))}
    if any(left!=right for left in ui_files for right in logic_files):
        return []
    implementation = [phase for phase in plan.get('phases') or [] if isinstance(phase, dict)
                      and phase.get('execution_mode') != 'VERIFICATION_ONLY'
                      and phase.get('kind') != 'ORCHESTRATOR_BLUEPRINT']
    ui = [phase.get('id') for phase in implementation if re.search(r'画面|\bUI\b|HTML|CSS|layout|interface|フォーム', str(phase.get('goal') or ''), re.I)]
    logic = [phase.get('id') for phase in implementation if re.search(r'ロジック|logic|algorithm|抽選処理|抽選関数|純粋関数|状態管理', str(phase.get('goal') or ''), re.I)
             and not re.search(r'テスト|\btests?\b', str(phase.get('goal') or ''), re.I)]
    if len(implementation) < 2 or not any(left != right for left in ui for right in logic):
        return ['Declare separate UI and logic files or distinct deliverable phases for the requested source boundary; keep test-code creation mutation-capable. Do not add scope.']
    return []
