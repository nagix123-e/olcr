"""Executable acceptance contracts for local, dependency-free web projects."""
from __future__ import annotations

import copy
import ast
import json
import re
import subprocess
import os
import signal
import shlex
import time
from pathlib import Path
from typing import Any

from .node_mcp_runtime import runtime_root, verification_browser_executable
from .verification_diagnostics import is_project_test_path

MAX_OBSERVATION_ACTIONS = 32


def verification_source_excerpts(sources: dict[str,str], limit: int=16000) -> dict[str,str]:
    """Bound model context without treating an excerpt as a complete file."""
    marker='\n[OLCR_SOURCE_EXCERPT: middle omitted; omitted code is not evidence of absence]\n'
    result={}
    for path,source in sources.items():
        if len(source)<=limit:
            result[path]=source
        else:
            available=max(0,limit-len(marker))
            head=available//2
            tail=available-head
            result[path]=source[:head]+marker+(source[-tail:] if tail else '')
    return result


def retained_semantic_review_ids(previous: dict | None, checks: list[dict]) -> list[str]:
    """A criterion's review covers its complete multiset of check bodies."""
    previous=previous or {}
    def bodies(items, identifier):
        return sorted(json.dumps(check,sort_keys=True) for check in items
            if identifier in (check.get('ids') or []))
    old=previous.get('checks') or []
    return sorted(identifier for identifier in previous.get('_olcr_semantically_reviewed_ids') or []
        if bodies(old,identifier) and bodies(old,identifier)==bodies(checks,identifier))


def scope_verification_candidate(candidate: dict | None, requested: dict[str,str]) -> tuple[dict | None, list[str]]:
    """Keep only checks for this authoring batch; extra model output is never accepted as coverage."""
    if not isinstance(candidate,dict) or not isinstance(candidate.get('checks'),list):
        return candidate, []
    scoped=[]
    discarded=set()
    for check in candidate['checks']:
        if not isinstance(check,dict) or not isinstance(check.get('ids'),list):
            scoped.append(check)
            continue
        selected=[identifier for identifier in check['ids'] if identifier in requested]
        discarded.update(identifier for identifier in check['ids'] if identifier not in requested)
        if selected:
            scoped.append({**check,'ids':selected})
    result={**candidate,'checks':scoped}
    return result, sorted(discarded)


def verification_budget_stop_reason(budget: dict, kind: str, author_limit: int,
                                  model_limit: int, review_limit: int) -> str:
    """Report the boundary preventing another verifier model call."""
    if budget.get('exhaustion'):
        return budget['exhaustion']
    if budget.get('model_calls',0)>=model_limit:
        return 'total model-call limit'
    if kind=='author' and budget.get('author_calls',0)>=author_limit:
        return 'check-authoring limit'
    if kind=='review' and budget.get('review_calls',0)>=review_limit:
        return 'independent-review limit'
    if kind=='author' and (budget.get('review_calls',0)>=review_limit
            or budget.get('model_calls',0)+2>model_limit):
        return 'independent-review reserve'
    return ''


class VerificationExecutorError(RuntimeError):
    """The host verifier cannot run; no application defect was observed."""


class VerificationScenarioCapabilityGap(ValueError):
    """A required UI scenario has no source-backed setup path to execute."""
    def __init__(self, identifier: str, scenario: str, requirement: str,
                 missing: list[str], source_controls: list[dict[str, str]]):
        self.identifier=identifier
        self.scenario=scenario
        self.requirement=requirement
        self.missing=missing
        self.source_controls=source_controls
        detail=', '.join(missing) or 'a usable condition control'
        super().__init__(f'{identifier} cannot resolve setup for {scenario}: source discovery did not identify {detail}')

    def evidence(self) -> dict[str, Any]:
        return {'review':{
            'verdict':'INCONCLUSIVE',
            'reason':str(self),
            'requirement':self.requirement[:600],
            'evidence_origin':'SOURCE_CAPABILITY_SCAN',
        },'failures':[{
            'ids':[self.identifier],
            'error':str(self),
            'scenario':self.scenario,
            'missing_capabilities':self.missing,
            'source_controls':self.source_controls[:24],
        }]}


def normalized_verification_argv(command: Any) -> list[str] | None:
    """Repair one quoted direct test invocation, never interpret shell syntax."""
    if not isinstance(command,(list,tuple)) or not command or any(
            not isinstance(item,str) or not item.strip() for item in command):
        return None
    values=[item.strip() for item in command]
    if len(values)==1 and any(char.isspace() for char in values[0]) and not Path(values[0]).is_file():
        if any(char in values[0] for char in '\n\r;&|<>`$'):
            return None
        try:
            parsed=shlex.split(values[0])
        except ValueError:
            return None
        # This repairs a common model encoding error. It is not a parser for
        # arbitrary prose, shell programs, package installs or network calls.
        if not parsed or Path(parsed[0]).name.lower() not in {'node','node.exe','python','python3','pytest'}:
            return None
        values=parsed
    if len(values)>32 or any(len(item)>400 for item in values):
        return None
    return values


def verification_command_matches(actual: Any, expected: Any) -> bool:
    actual=normalized_verification_argv(actual)
    expected=normalized_verification_argv(expected)
    if not actual or not expected:
        return False
    if actual==expected:
        return True
    if actual[1:]!=expected[1:]:
        return False
    if expected[0]=='python' and Path(actual[0]).name.lower()=='python3':
        return True  # existing host repair for a missing python alias
    resources=runtime_root() if expected[0]=='node' else None
    return bool(resources and actual[0]==str(resources/'node/bin/node'))


def inherit_owned_verification(plan: dict, previous: dict, owned_phase_ids=()) -> dict:
    """Keep host executor ownership across model-authored plan replacements."""
    owned={phase['id']:phase for phase in previous.get('phases') or [] if phase.get('host_verification')}
    for identifier in owned_phase_ids:
        owned.setdefault(identifier,{})
    if not owned:
        return plan
    result=copy.deepcopy(plan)
    phases=result.setdefault('phases',[])
    def terminal_gate(candidate):
        by_id={phase['id']:phase for phase in phases}
        def depends_on(identifier,visited):
            if identifier==candidate['id']:return True
            if identifier in visited:return False
            visited.add(identifier)
            return any(depends_on(parent,visited) for parent in by_id.get(identifier,{}).get('dependencies') or [])
        return not any(depends_on(phase['id'],set()) for phase in phases
            if phase.get('execution_mode') in {'IMPLEMENTATION','IMPLEMENTATION_AND_VERIFICATION'})
    for identifier,old in owned.items():
        target=next((phase for phase in phases if (phase.get('id')==identifier
                     or identifier in phase.get('host_verification_origins',[]))
                     and phase.get('execution_mode')=='VERIFICATION_ONLY' and terminal_gate(phase)),None)
        if target is None:
            target=next((phase for phase in reversed(phases) if phase.get('execution_mode')=='VERIFICATION_ONLY'
                         and not phase.get('host_verification') and terminal_gate(phase)),None)
        if target is None:
            target=next((phase for phase in reversed(phases) if phase.get('execution_mode')=='VERIFICATION_ONLY'
                         and phase.get('host_verification') and terminal_gate(phase)),None)
        if target is None:
            occupied={phase['id'] for phase in phases}
            available=identifier
            while available in occupied:available+='_NEXT'
            target={'id':available,'goal':'Verify the implemented application against the original requirements.',
                    'status':'pending','done':['The app loads in a browser without console errors.'],
                    'verify':[],'dependencies':[],'risks':[],'execution_mode':'VERIFICATION_ONLY'}
            phases.append(target)
            if result.get('tasks'):
                task_identifier=available+'-task'
                occupied_tasks={task['task_id'] for task in result['tasks']}
                while task_identifier in occupied_tasks:task_identifier+='_NEXT'
                result['tasks'].append({'task_id':task_identifier,'phase_id':available,'goal':target['goal'],
                    'domain':'verification','depends_on':[],'required_context':[],'required_mcp':[],
                    'change_scope':[],'verification':[],'done_condition':[],'retry_state':{}})
        for field in ('done','verify'):
            target[field]=list(dict.fromkeys([*(old.get(field) or []),*(target.get(field) or [])]))
        target['dependencies']=list(dict.fromkeys([*(target.get('dependencies') or []),
            *(phase['id'] for phase in phases if phase.get('execution_mode') in {'IMPLEMENTATION','IMPLEMENTATION_AND_VERIFICATION'})]))
        repair_allowed=old.get('verification_repair_allowed',target.get('verification_repair_allowed',True))
        target.update(host_verification=True,host_verification_version=3,verification_repair_allowed=repair_allowed is True)
        target['host_verification_origins']=list(dict.fromkeys([*(target.get('host_verification_origins') or []),
            *(old.get('host_verification_origins') or []),identifier,target['id']]))
    phase_tasks={task['phase_id']:task['task_id'] for task in result.get('tasks') or []}
    for task in result.get('tasks') or []:
        phase=next((phase for phase in phases if phase['id']==task['phase_id']),None)
        if phase and phase.get('host_verification'):
            task.update(verification=phase['verify'],done_condition=phase['done'],
                        depends_on=[phase_tasks[identifier] for identifier in phase['dependencies'] if identifier in phase_tasks])
    return result


def _attach_project_test_commands(plan: dict) -> dict:
    """A produced test file must execute before its producing phase can pass."""
    result=copy.deepcopy(plan)
    manifest=result.get('file_manifest') or []
    phases={p['id']:p for p in result.get('phases') or []}
    for phase in phases.values():
        if phase.get('status') in {'pass','completed'} or phase.get('verification_command'):
            continue
        owned=[e['path'] for e in manifest if e.get('owner_phase')==phase['id'] and is_project_test_path(e.get('path',''))]
        if not owned:continue
        available={phase['id']};pending=list(phase.get('dependencies') or [])
        while pending:
            identifier=pending.pop()
            if identifier in available:continue
            available.add(identifier);pending.extend((phases.get(identifier) or {}).get('dependencies') or [])
        # Do not execute a test before application inputs from later phases exist.
        if any(e.get('owner_phase') not in available for e in manifest
               if str(e.get('path','')).endswith(('.js','.mjs','.cjs')) and not is_project_test_path(e.get('path',''))):
            continue
        phase.update(execution_mode='IMPLEMENTATION_AND_VERIFICATION',
            verification_command=['node','--test',*owned],
            verification_targets=[e['path'] for e in manifest if e.get('owner_phase') in available and e.get('path')])
        phase['verify']=['The Node.js test command passes with registered tests and no failing assertions.']
        for task in result.get('tasks') or []:
            if task.get('phase_id')==phase['id']:task['verification']=phase['verify']
    return result


def normalize_static_verification(plan: dict, workspace_root: str | None = None,
                                 verification_requested: bool = False) -> dict:
    """Add checks only after an explicit request, then defer them to their inputs.

    This is an opt-in boundary: source file types, test files, and browser
    applicability never create verification work by themselves. The file
    manifest remains the same authorization boundary once checks are requested.
    """
    if verification_requested is not True:
        return plan
    manifest = plan.get('file_manifest') or (plan.get('implementation_plan_artifact') or {}).get('file_manifest') or []
    paths = ([e.get('path', '') for e in manifest if isinstance(e, dict)] if manifest else
             [p for p in (plan.get('scope') or {}).get('allowed') or []
              if isinstance(p,str) and Path(p).suffix and not any(c in p for c in '*?\n')])
    unsupported_source = any(Path(p).suffix.lower() in {'.py','.rs','.go','.java','.kt','.swift','.c','.cpp','.cs','.rb','.php'} for p in paths)
    from .node_project_verification import is_vite_project
    vite_project = ('package.json' in paths
                    and is_vite_project(Path(workspace_root) if workspace_root else None,paths=paths)
                    and any(p.endswith(('.tsx','.jsx','.ts','.js')) for p in paths)
                    and not any(p.endswith('.vue') for p in paths))
    if (not any(p.endswith('.html') for p in paths) or not any(p.endswith(('.js','.tsx','.jsx','.ts')) for p in paths)
            or (any(p.endswith(('.tsx', '.ts', '.jsx', '.vue')) for p in paths) and not vite_project)
            or unsupported_source
            or any(p.get('requires_dependency_installation') or p.get('checkpoint_on_failure') for p in plan.get('phases') or [])):
        return plan
    if workspace_root:
        try:
            package=json.loads((Path(workspace_root)/'package.json').read_text())
            package_dependencies=set()
            if isinstance(package,dict):
                for field in ('dependencies','devDependencies','peerDependencies','optionalDependencies'):
                    values=package.get(field)
                    if isinstance(values,dict):
                        package_dependencies.update(str(name).casefold() for name in values)
            # The Node runtime itself does not make a plain HTML/JS project a
            # framework workflow. Keeping it eligible lets the host attach
            # the declared node:test files and its independent browser gate.
            if not vite_project and package_dependencies-{'node','@types/node'}:
                return plan
        except (OSError,ValueError):
            pass
    phases = plan.get('phases') or []
    final = next((p for p in reversed(phases) if p.get('execution_mode') == 'VERIFICATION_ONLY'), None)
    if (final and final.get('host_verification') and not final.get('verification_command')
            and final.get('done')==['Executable acceptance checks pass.']):
        # The host's aggregate completion gate is not a model-authored test
        # requirement. Give this owned phase a concrete, owned browser check.
        result=copy.deepcopy(plan)
        for item in [*result['phases'],*(result.get('tasks') or [])]:
            if item.get('id')==final['id']:
                item['done']=['The app loads in a browser without console errors.']
            elif item.get('phase_id')==final['id']:
                item['done_condition']=['The app loads in a browser without console errors.']
        return normalize_static_verification(result,workspace_root,verification_requested)
    if not phases or (final and (final.get('verification_command') or final.get('host_verification_version') == 3)):
        return _attach_project_test_commands(plan)
    implementation=[p for p in phases if p.get('execution_mode') in {'IMPLEMENTATION','IMPLEMENTATION_AND_VERIFICATION'}
                    and p.get('kind')!='ORCHESTRATOR_BLUEPRINT']
    if not manifest:
        # Older plans named exact writable files only in scope.allowed.
        # Recover ownership from implementation goals/done criteria, never
        # from verification references or outside that approved path set.
        # Python's Unicode \w also matches Japanese particles adjoining a
        # filename ("app.jsに"). Only filename/path continuations delimit it.
        recovered=[]
        for path in paths:
            owner=next((p['id'] for p in implementation if re.search(
                r'(?<![A-Za-z0-9_./-])'+re.escape(path)+r'(?![A-Za-z0-9_./-])',
                '\n'.join([str(p.get('goal') or ''),*(str(t) for t in p.get('done') or [])]))),None)
            if owner is None and len(implementation)==1:
                owner=implementation[0]['id']
            if owner is None:
                return plan
            exists=bool(workspace_root and (Path(workspace_root)/path).is_file())
            recovered.append({'path':path,'action':'modify' if exists or not workspace_root else 'create',
                              'owner_phase':owner,'required':True})
        manifest=recovered
    result = copy.deepcopy(plan)
    if not plan.get('file_manifest') and not (plan.get('implementation_plan_artifact') or {}).get('file_manifest'):
        result['file_manifest']=manifest
    if final is None:
        identifier = 'OLCR_ACCEPTANCE'
        occupied = {p['id'] for p in phases} | {t['task_id'] for t in plan.get('tasks') or []}
        while identifier in occupied or identifier + '-task' in occupied:
            identifier += '_NEXT'
        final = {'id':identifier, 'goal':'Verify the implemented application against the original requirements.',
                 'status':'pending', 'done':['The app loads in a browser without console errors.'], 'verify':[],
                 'dependencies':[], 'risks':[], 'execution_mode':'VERIFICATION_ONLY'}
        result['phases'].append(final)
        if result.get('tasks'):
            result['tasks'].append({'task_id':identifier+'-task','phase_id':identifier,'goal':final['goal'],
                'domain':'verification','depends_on':[],'required_context':[],'required_mcp':[],
                'change_scope':[],'verification':[],'done_condition':final['done'],'retry_state':{}})
    final = next(p for p in result['phases'] if p['id'] == final['id'])
    deferred = list(final.get('verify') or [])
    dependencies = list(final.get('dependencies') or [])
    for phase in result['phases']:
        if phase['id'] == final['id'] or phase.get('kind') == 'ORCHESTRATOR_BLUEPRINT':
            continue
        if phase.get('execution_mode') not in {'IMPLEMENTATION','IMPLEMENTATION_AND_VERIFICATION'} or phase.get('verification_command'):
            continue
        if (phase.get('verify') == ['Typed workspace writes and read-back succeed for the changed files.']
                and all(str(item).endswith(' exists') for item in phase.get('done') or [])):
            continue  # already prepared by version 2; its behavioral checks remain in final
        deferred.extend(phase.get('done') or [])
        deferred.extend(phase.get('verify') or [])
        owned = [entry['path'] for entry in manifest if isinstance(entry, dict)
                 and entry.get('owner_phase') == phase['id'] and entry.get('path')]
        if owned:
            # Creation/coverage of tests is an artifact deliverable. Executing
            # those tests is deferred; don't require it before its own files
            # and the remaining application dependencies have been created.
            phase['done'] = [path + ' exists' for path in owned]
        phase['execution_mode']='IMPLEMENTATION'
        phase['verify'] = ['Typed workspace writes and read-back succeed for the changed files.']
        phase.pop('verification_executor', None)
        phase.pop('criterion_executors', None)
        if phase['id'] not in dependencies:
            dependencies.append(phase['id'])
    final.update(verify=list(dict.fromkeys(deferred)), dependencies=dependencies,
                 host_verification=True, host_verification_version=3, verification_repair_allowed=True)
    for task in result.get('tasks') or []:
        phase = next(p for p in result['phases'] if p['id'] == task['phase_id'])
        task['verification'] = phase['verify']
        task['done_condition'] = phase['done']
        phase_tasks = {p['phase_id']: p['task_id'] for p in result.get('tasks') or []}
        task['depends_on'] = [phase_tasks[p] for p in phase['dependencies'] if p in phase_tasks]
    return _attach_project_test_commands(result)


def acceptance_criteria(phase: dict) -> dict[str, str]:
    return {f'{kind}:{i+1}': text for kind, field in [('DONE', 'done'), ('VERIFY', 'verify')]
            for i, text in enumerate(phase.get(field) or [])}


def bind_original_acceptance(plan: dict, original: str) -> dict:
    """Preserve explicit app specification bullets when a planner omitted them."""
    final = next((p for p in reversed(plan.get('phases') or []) if p.get('host_verification')), None)
    if final is None:
        return plan
    active, bullets = False, []
    for line in original.splitlines():
        if re.match(r'\s*(?:【仕様】|#{0,3}\s*(?:Requirements|Specifications?)\s*:?)\s*$', line, re.I):
            active = True
            continue
        if not active or not line.strip():
            continue
        match = re.match(r'\s*[-*]\s+(.+)', line)
        if match:
            bullets.append('In the browser, verify the original requirement: ' + match.group(1).strip())
        else:
            break
    result = copy.deepcopy(plan)
    scheduled_final = next(p for p in reversed(result.get('phases') or []) if p.get('id') == final.get('id'))
    has_per_line_input = any(re.search(
        r'one per line|one .+ per line|each line|1行に1つ|一行に一つ', bullet, re.I) for bullet in bullets)
    conflict = re.compile(r'single[- ]line input|input field[^.]{0,40}single[- ]line|single line text input|1行入力|単一行入力|一行入力', re.I)
    changed = False
    if has_per_line_input:
        for field in ('done', 'verify'):
            current = scheduled_final.get(field) or []
            filtered = [text for text in current if not conflict.search(str(text))]
            if filtered != current:
                scheduled_final[field] = filtered
                changed = True
        for task in result.get('tasks') or []:
            if task.get('phase_id') != scheduled_final.get('id'):
                continue
            current = task.get('verification') or []
            filtered = [text for text in current if not conflict.search(str(text))]
            if filtered != current:
                task['verification'] = filtered
                changed = True
    missing = [text for text in bullets if text not in scheduled_final.get('verify', [])]
    if not missing and not changed:
        return plan
    for phase in result['phases']:
        if phase['id'] == scheduled_final['id']:
            phase['verify'] = [*(phase.get('verify') or []), *missing]
    for task in result.get('tasks') or []:
        if task['phase_id'] == scheduled_final['id']:
            task['verification'] = [*(task.get('verification') or []), *missing]
    return result


def verification_schema(criteria: dict[str, str] | None = None, *, force_code: bool=False) -> dict:
    if not force_code and declarative_browser_criteria(criteria or {}):
        if layout_browser_criteria(criteria or {}):
            return verification_observation_schema(criteria or {}, allow_code=False)
        return browser_observation_schema(criteria or {})
    identifiers = {'type':'string'}
    if criteria:
        identifiers['enum'] = list(criteria)
    return {'type': 'object', 'properties': {
        'entrypoint': {'type': 'string'},
        'checks': {'type': 'array', 'minItems': max(1,len(criteria or {})), 'maxItems':max(1,len(criteria or {})), 'items': {'type': 'object', 'properties': {
            'ids': {'type': 'array', 'minItems': 1, 'maxItems':1, 'items': identifiers},
            'kind': {'type': 'string', 'enum': (['BROWSER'] if criteria and browser_criteria(criteria)==set(criteria) else ['NODE', 'BROWSER'])},
            'code': {'type': 'array', 'minItems':1, 'maxItems':32, 'items':{'type':'string','maxLength':1000}}}, 'required': ['ids', 'kind', 'code'], 'additionalProperties': False}}},
        'required': ['entrypoint', 'checks'], 'additionalProperties': False}


def normalize_verification_source(value: Any) -> Any:
    """Frame generated code as lines so JSON escapes cannot become JS tokens.

    Cached/legacy checks already contain a string; their source is preserved.
    This joins line frames only, without rewriting JavaScript literals.
    """
    if not isinstance(value,dict) or not isinstance(value.get('checks'),list):
        return value
    result=copy.deepcopy(value)
    for check in result['checks']:
        if isinstance(check,dict) and isinstance(check.get('code'),list) and all(isinstance(line,str) for line in check['code']):
            check['code']='\n'.join(check['code'])
    return result


def complete_verification_prefix(raw: str) -> dict | None:
    """Recover only fully decoded check objects from a truncated JSON frame.

    These are still subject to scope, syntax and full-coverage validation;
    partial file-operation responses are never executed this way.
    """
    header = re.match(r'\s*\{\s*"entrypoint"\s*:\s*("(?:[^"\\]|\\.)*")\s*,\s*"checks"\s*:\s*\[', raw)
    if not header:
        return None
    checks, position, decoder = [], header.end(), json.JSONDecoder()
    while position < len(raw):
        while position < len(raw) and raw[position].isspace():
            position += 1
        try:
            check, end = decoder.raw_decode(raw, position)
        except ValueError:
            break
        if not isinstance(check, dict):
            return None
        checks.append(check)
        position = end
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position >= len(raw) or raw[position] != ',':
            break
        position += 1
    return {'entrypoint': json.loads(header.group(1)), 'checks':checks} if checks else None


def browser_criteria(criteria: dict[str, str]) -> set[str]:
    return {key for key, text in criteria.items() if re.search(
        r'\bbrowser\b|\bviewport\b|\bresize\b|\bclick\b|\bsmartphones?\b'
        r'|\bmobile(?:[- ]friendly|[- ](?:UI|layout|viewport|screens?|devices?))\b|\bon mobile\b'
        r'|\bresponsive\s+(?:layout|UI|design|page|interface)\b'
        r'|\brenders?\b.{0,100}\b(?:UI|interface|page|app)\b'
        r'|\bUI\b.{0,100}\b(?:render\w*|display\w*|input|button|usable)\b'
        r'|\b(?:wires?|connects?)\b.{0,80}\bUI\b'
        r'|^\s*(?:Enter|Type|Fill|Submit)\b(?![^\n]*\b(?:terminal|shell|command|function|module|API)\b)[^\n]*\band\s+(?:click|confirm|verify)\b'
        r'|ブラウザ|ブラウザー|クリック|スマートフォン|モバイル'
        r'|(?:UI|画面).{0,60}(?:表示|描画|操作|入力|連携)', str(text), re.I)}


def verification_coverage_gaps(value: dict, criteria: dict[str, str]) -> dict[str, str]:
    covered = {key for check in value['checks'] for key in check['ids']}
    browser_covered = {key for check in value['checks'] if check['kind'] == 'BROWSER' for key in check['ids']}
    missing = (set(criteria) - covered) | (browser_criteria(criteria) - browser_covered)
    return {key: text for key, text in criteria.items() if key in missing}


def validate_verification(value: Any, criteria: dict[str, str], root: Path) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get('checks'), list) or not value['checks']:
        return ['verification checks are missing']
    errors, covered, browser_covered = [], set(), set()
    entry = value.get('entrypoint')
    if not isinstance(entry, str):
        return ['entrypoint is missing']
    if Path(entry).is_absolute() or '..' in Path(entry).parts:
        return ['entrypoint must be a relative in-workspace path']
    try:
        path = (root / entry).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file() or path.suffix != '.html':
            errors.append('entrypoint must be an existing in-workspace HTML file')
    except (OSError, ValueError):
        errors.append('entrypoint is outside or absent from workspace')
    for check in value['checks']:
        if not isinstance(check, dict):
            errors.append('invalid check'); continue
        ids, code = check.get('ids'), check.get('code')
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in criteria for i in ids)
                or check.get('kind') not in {'NODE', 'BROWSER'} or not isinstance(code, str) or not code.strip()
                or len(code) > 24000):
            errors.append('check must contain known criterion IDs, kind and bounded code'); continue
        covered.update(ids)
        if check['kind'] == 'BROWSER':
            browser_covered.update(ids)
    if covered != set(criteria):
        errors.append('checks must cover every acceptance criterion; missing IDs: ' + ', '.join(sorted(set(criteria) - covered)))
    browser_required = browser_criteria(criteria)
    if browser_required - browser_covered:
        errors.append('BROWSER checks must cover browser interaction criteria: ' + ', '.join(sorted(browser_required - browser_covered)))
    return errors


def execution_command(contract: Path, root: Path) -> list[str]:
    resources = runtime_root()
    if resources is None:
        raise VerificationExecutorError('VERIFICATION_NODE_RUNTIME_UNAVAILABLE')
    runner = Path(__file__).with_name('coding_verification_runner.cjs')
    command = [str(resources / 'node/bin/node'), str(runner), str(contract), str(root), str(resources)]
    value = json.loads(contract.read_text(encoding='utf8'))
    if any(check.get('kind') == 'BROWSER' for check in value.get('checks') or []):
        browser = verification_browser_executable(resources, prepare=True)
        if browser is None:
            raise VerificationExecutorError('VERIFICATION_BROWSER_RUNTIME_UNAVAILABLE')
        command.append(str(browser))
    return command


def verification_syntax_errors(value: dict) -> list[str]:
    resources = runtime_root()
    if resources is None:
        return ['VERIFICATION_NODE_RUNTIME_UNAVAILABLE']
    source = '\n'.join(f'async function check_{i}(assert,__olcr_context) {{\n{{\n'
                       'const {workspace,loadModule,readFile,runTests,page,baseURL,require,expect}=__olcr_context;\n'
                       f'{c["code"]}\n}}\n}}'
                       for i, c in enumerate(value['checks']))
    result = subprocess.run([str(resources / 'node/bin/node'), '--check'], input=source,
                            text=True, capture_output=True, timeout=5)
    return [] if result.returncode == 0 else ['Verification JavaScript syntax error: ' + result.stderr[-2000:]]


def standard_verification_checks(criteria: dict[str, str]) -> list[dict]:
    """Use owned executors for unambiguous standard checks, without LLM code."""
    tests, browser, named_tests, network = [], [], [], []
    project_checks = []
    for identifier, text in criteria.items():
        # This recognizes a mechanical criterion, not an executable command.
        # The owned project executor supplies the evidence after an isolated
        # dependency installation and a real Vite/TypeScript build.
        project = re.fullmatch(r'npm (install|run build) (?:completes?|passes)(?: without errors)?[.]?', text, re.I)
        if project:
            field = 'install_passed' if project[1].lower() == 'install' else 'build_passed'
            project_checks.append({'ids':[identifier], 'kind':'NODE',
                'code':f'assert.equal(projectBuild.{field},true);'})
            continue
        named=re.fullmatch(r'node\s+--test\s+([A-Za-z0-9_./-]+\.(?:test|spec)\.[cm]?js)が(?:0で終了|成功)する[。.]?',text)
        # Some plans spell the node:test file selector as --test-path.
        # It is a named-file requirement, not a supported Node CLI flag.
        named=named or re.fullmatch(r'(?:Run\s+)?node:test\s+--test-path\s+([A-Za-z0-9_./-]+\.(?:test|spec)\.[cm]?js)\s+(?:passes|and confirm all tests pass)\.?',text,re.I)
        # A command-exit/output requirement is mechanical. Keep this a full
        # match so coverage/behavior requirements cannot be mistaken for it.
        named=named or re.fullmatch(
            r'(?:(?:Run|Running)\s+)?node\s+--test\s+([A-Za-z0-9_./-]+\.(?:test|spec)\.[cm]?js)\s+'
            r'(?:passes(?: without errors)?|(?:exits? with (?:exit )?code 0|exits? successfully)(?: and (?:prints? passing test results|reports? (?:all )?tests? pass(?:ing)?))?)\.?',text,re.I)
        japanese=re.fullmatch(r'(?:全テスト|すべてのテスト|全てのテスト|テスト)(?:が|は)(?:全項目)?(?:成功|合格)する(?:ことを確認する)?[。.]?',text)
        if re.fullmatch(r'(?:All (?:node:test )?tests pass\.?|Run the test (?:script|suite)(?: from package\.json)? and confirm (?:exit code 0|all tests pass)\.?)', text, re.I) or japanese or named:
            tests.append(identifier)
            if named and named.group(1) not in named_tests:
                named_tests.append(named.group(1))
        elif re.fullmatch(r'(?:The app loads in a browser without console errors\.?|Open \S+\.html in a browser and confirm no JavaScript errors in the console\.?)', text, re.I):
            browser.append(identifier)
        elif network_browser_criteria({identifier:text}):
            network.append(identifier)
        elif layout_browser_criteria({identifier:text}):
            stylesheet=re.search(r'(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+\.css)(?![A-Za-z0-9_./-])',text,re.I)
            for width,height in ((375,812),(1280,800)):
                project_checks.append({'ids':[identifier],'kind':'BROWSER','code':
                    'await assertResponsiveLayout('+json.dumps({'width':width,'height':height})+','
                    +json.dumps(stylesheet.group(1) if stylesheet else None)+');'})
    checks = project_checks
    if tests:
        checks.append({'ids':tests, 'kind':'NODE', 'code':
            ''.join('assert.ok(readFile('+json.dumps(path)+').trim().length>0);' for path in named_tests)
            +'const result=await runTests();assert.equal(result.returncode,0);assert.ok(result.tests>0);'})
    if browser:
        # The owned runner always rejects pageerror and console.error events.
        checks.append({'ids':browser, 'kind':'BROWSER', 'code':
            'assert.equal(await page.locator("html").count(),1);'})
    if network:
        # Resource origins, actual attempted requests and declared packages
        # have mechanical definitions. Never ask a model to invent them.
        value={'checks':[{'ids':network,'kind':'BROWSER','actions':[],
            'observations':[{'selector':'html','measure':'local_resources','operator':'eq','expected':True},
                {'selector':'html','measure':'external_request_count','operator':'eq','expected':0},
                {'selector':'package.json','measure':'dependency_count','operator':'eq','expected':0}]}]}
        checks.extend(compile_browser_observations(value,{key:criteria[key] for key in network})['checks'])
    return checks


def run_acceptance(command: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """Reap the owned runner and its children on a hung acceptance check."""
    if len(command)>2 and Path(command[1]).resolve()==Path(__file__).with_name('coding_verification_runner.cjs').resolve():
        from .node_project_verification import run_vite_acceptance, is_vite_project
        if is_vite_project(Path(cwd)):
            return run_vite_acceptance(command, cwd, timeout, _run_acceptance_process)
    return _run_acceptance_process(command,cwd,timeout)


def _run_acceptance_process(command: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess:
    from .generation_control import GenerationCancelled, check_cancelled
    check_cancelled()
    with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        deadline=time.monotonic()+timeout
        try:
            while True:
                check_cancelled()
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout,stderr=process.communicate(timeout=min(0.25,remaining))
                    check_cancelled()
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (subprocess.TimeoutExpired,GenerationCancelled) as exc:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try:
                stdout, stderr = process.communicate(timeout=0.5 if isinstance(exc,GenerationCancelled) else 5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                stdout, stderr = process.communicate()
            if isinstance(exc,GenerationCancelled):
                raise
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _browser_recovery_guidance(original: str, criteria: dict[str,str]) -> list[str]:
    """Keep common browser authoring away from known native-control and persistence traps."""
    requirement='\n'.join([original,*criteria.values()])
    notes=[]
    if re.search(r'\b(?:select|selection|choose|dropdown|meal|weekday|assign(?:ment|ed|ing)?)\b|選択|料理|献立|曜日|朝食|昼食|夕食|割り当て|割当',requirement,re.I):
        notes.append('NATIVE_SELECT_FOCUS: Use selectOption(value) on the owning <select>; never click <option>.')
    if re.search(r'\b(?:manual(?:ly)?|assign(?:ment|ed|ing)?)\b|手動|割り当て|割当',requirement,re.I):
        notes.append('MANUAL_ASSIGNMENT_FOCUS: When the human asks for manual assignment, perform it through the actual weekday/meal-slot controls and assert the chosen dish is in that slot. Do not replace manual assignment with automatic generation.')
    if re.search(r'\b(?:persist(?:ence|ent)?|restore|restored|restart|reload|saved?)\b|保存|再起動|復元|永続化',requirement,re.I):
        notes.append('PERSISTENCE_FOCUS: Perform a real user-facing save or assignment, reload the same page with await page.reload(), then assert the rendered UI restored the saved value. Reading localStorage alone is not proof of restored application state.')
    return notes


def verification_prompt(phase: dict, original: str, sources: dict[str, str], criteria: dict[str, str] | None = None,
                       *, force_code: bool=False) -> str:
    """Keep a check's instructions focused on its own requirement."""
    requested=acceptance_criteria(phase) if criteria is None else criteria
    if (not force_code and (declarative_browser_criteria(requested)
            or layout_browser_criteria(requested) or multi_scenario_browser_criteria(requested))):
        return browser_observation_prompt(original,sources,requested)
    browser_ids=browser_criteria(requested)
    browser_only=bool(requested) and browser_ids==set(requested)
    if browser_only:
        sources={path:source for path,source in sources.items()
                 if Path(path).suffix in {'.html','.js','.mjs','.cjs','.css','.ts','.tsx','.jsx'}
                 and not re.search(r'\.(?:test|spec)\.[cm]?js$',path)}
    text='\n'.join(requested.values())
    notes=["CODE_FRAMING_FOCUS: Prefer single-quoted JavaScript strings such as page.locator('#id') or readFile('logic.js'). Do not insert JSON array/object closing delimiters into code strings."]
    if browser_ids:
        notes.extend(_browser_recovery_guidance(original,requested))
        notes.append('DIALOG_FOCUS: For sequential native dialogs register ONE page.on("dialog", handler) before the action, '
            'using a response queue inside that handler. Multiple page.once("dialog", ...) handlers registered before '
            'a click all receive the SAME first dialog; do not register them as if they were a sequence. '
            'Return/await dialog.accept or dialog.dismiss inside the handler.')
    if browser_only and re.search(r'empty input|input.{0,12}empty|空入力|入力.{0,8}空|有効.{0,8}候補.{0,12}ない',text,re.I):
        notes.append('EMPTY_INPUT_FOCUS: Test both empty and whitespace-only values through the real input; keep the tested input empty/invalid. '
            'Read the existing visible input guidance (hint, label, alert or status) from ACTUAL_SOURCE. '
            'Do not click a disabled button or force-enable it. Unless the user specified a location, a clear visible message need not be inside the result element. '
            'The current-result area can legitimately be empty before any selection; do not require a nonempty result there or assume an uncalled handler ran at startup. '
            'Check disabled state with await expect(locator).toBeDisabled() or await locator.isDisabled(). HTML boolean attributes may have an empty string value, not the string "true". '
            'Do not replace empty values with valid candidates. Prove actual guidance text and visibility, not only a disabled button.')
    if browser_only and re.search(r'per line|multi.?line|1行に1つ|一行に一つ|複数行|改行',text,re.I):
        notes.append('MULTILINE_FOCUS: Fill once with ["A","B"].join(String.fromCharCode(10)); fill replaces the entire value. '
            'Observe inputValue to confirm actual line breaks. Do not accommodate a single-line input that removes required line breaks.')
    if browser_only and re.search(r'history|latest|recent|履歴|直近',text,re.I):
        notes.append('HISTORY_FOCUS: Perform more than the requested N selections. Immediately after each click record the CURRENT RESULT independently of the history list. '
            'Compare the displayed history to observed.slice(-N), allowing either display order. Count alone, first-N results or expected values read from history cannot prove latest-N behavior.')
    if browser_only and re.search(r'random|select|choose|抽選|ランダム|選ぶ|選択|連続',text,re.I):
        notes.append('SELECTION_FOCUS: Use real input and click handlers. Assert membership/invariants of observed results, not a particular random choice. '
            'An exact choice is justified only when input and exclusion leave one eligible candidate.')
        if re.search(r'consecutive|repeat|連続|直前|前回',text,re.I):
            notes.append('NO_REPEAT_FOCUS: Use at least two distinct labels including duplicate input rows and at least eight selections. '
                'Read each result immediately after its own click and compare adjacent stored results. Reading the current result twice after several clicks does not retrieve prior results.')
    if browser_only and re.search(r'smartphones?|mobile|スマートフォン|モバイル',text,re.I):
        notes.append('MOBILE_FOCUS: OLCR starts this check at 375x812. Observe visible usable controls and document.documentElement.scrollWidth <= window.innerWidth. '
            'Do not invent font, shadow or CSS values. Never modify DOM/CSS to make a check pass.')
    if not browser_only:
        notes.append('NODE_FOCUS: readFile reads actual UTF-8 project files; await loadModule(relativePath) loads the actual exports, including CJS named/default exports. '
            'Imported bindings and local declarations are NOT exports of the importing file. Never inspect a test-file namespace to infer what it imports. '
            'Use readFile to inspect actual test imports/calls, loadModule on the actual API module to observe its exports, and runTests for test execution. '
            'Use the API actually present, not invented planner names. await runTests() executes all existing node:test files, rejects failure/zero tests, and returns {tests,returncode,output}. '
            'Use an observed successful nonzero test run for test execution criteria. Test coverage criteria also require checking actual coverage, not only a count.')
    helper_context=('Available browser helpers: page, expect, read-only observations and setStorageFailure. No imports are needed. '
                    if browser_only else
                    'Available: assert (node:assert/strict), expect (Playwright), page/baseURL (BROWSER), workspace, readFile, loadModule, runTests. '
                    'These helpers are already supplied and observed. Do not import or redeclare assert, expect, page, readFile, loadModule or runTests. ')
    readonly_modules=('Readonly require supports fs/path/url/assert and project modules; child_process is limited to canonical node --test. '
                      if not browser_only else '')
    shape={'entrypoint':'relative/existing.html','checks':[{'ids':list(requested)[:1],
        'kind':'BROWSER' if browser_only else 'NODE','code':['JavaScript statements observing and asserting the supplied criterion']}]}
    return ('Author one concise executable acceptance check for CRITERIA, from ACTUAL_SOURCE. Return JSON only: '
        +json.dumps(shape,ensure_ascii=False)+'. code is an array of source lines joined into an async function body. '
        'Use 1-3 meaningful assertions for this criterion only; do not test other original requirements here. '
        'The ORIGINAL_REQUEST outranks conflicting planner wording, invented interfaces or message placement. '
        'Do not modify source, mock the app, implement its logic, skip tests, assert constant true, or derive expected values from the same value under test. '
        +helper_context
        +'page already shows entrypoint in real Chromium, with real event listeners and a fresh page per check. Do not create a browser. '
        'Use actual source IDs/classes/accessible names. A section without an accessible name is not a named region. '
        'For UI structure, verify controls exist and are visible; empty result/history containers need only exist before use and may have zero height. '
        'Check result/history visibility after filling valid input and selecting. Submission may be disabled on empty input. '
        'Do not assume a handler ran at startup or invent nested selectors. Do not require exact rows, classes or initial values unless the human specified them. '
        'Await EVERY asynchronous browser action/observation and Playwright matcher. Locators are not DOM elements: use inputValue(), textContent(), allTextContents(), getAttribute() and isDisabled(), not .value/.className. '
        'isAttached() is an OLCR read-only compatibility helper returning whether actual DOM matches exist; it does not require visibility. A textarea is a tag, not a type attribute value. '
        'Read await results into local values before using string/array methods. page.evaluate/locator.evaluate are read-only observations, never application mutations. To test storage-write failure, await setStorageFailure(true), perform the normal UI save action, and assert visible error guidance; await setStorageFailure(false) restores writes. This host-owned fault never changes application source or DOM. '
        +readonly_modules
        +'\nREQUIRED_BROWSER_IDS: '+json.dumps(sorted(browser_ids))
        +'\nThese IDs MUST use BROWSER with real DOM/viewport observations, not CSS-source inspection.'
        +'\nORIGINAL_REQUEST:\n'+original+'\nCRITERIA:\n'+json.dumps(requested,ensure_ascii=False)
        +'\nACTUAL_SOURCE:\n'+json.dumps(verification_source_excerpts(sources),ensure_ascii=False)
        +'\n'+'\n'.join(notes))


def declarative_browser_criteria(criteria: dict[str,str]) -> bool:
    """Use typed observations for simple layout/structure, not behavioral loops."""
    return network_browser_criteria(criteria) or bool(criteria) and browser_criteria(criteria)==set(criteria) and all(re.search(
        r'\brenders?\b.{0,100}\b(?:UI|interface)\b|\b(?:wires?|connects?)\b.{0,80}\bUI\b'
        r'|\bmobile[- ]friendly\b|\bresponsive\s+(?:layout|UI|design|page|interface)\b'
        r'|\bUI\b.{0,60}\busable\b.{0,60}\b(?:mobile|viewport)\b',text,re.I)
        for text in criteria.values())


def _typed_observation_variants(templates: list[dict]) -> dict:
    boolean={'attached','visible','enabled','overflow_x','font_larger_than_body','file_nonempty','local_resources','module_has_export'}
    numeric={'count','external_request_count','dependency_count','test_count'}
    dynamic={'module_call','json_field'}
    variants=[]
    for template in templates:
        names=set(template['properties']['measure']['enum'])
        groups=[(names&boolean,['eq'],{'type':'boolean'}),
                (names&numeric,['eq','gt','gte','lt','lte'],{'type':'number'}),
                (names-boolean-numeric-dynamic,['eq'],{'type':'string','maxLength':1000}),
                (names-boolean-numeric,['includes'],{'type':'string','minLength':1,'maxLength':1000}),
                (names-boolean-numeric,['one_of'],{'type':'array','minItems':1,'maxItems':20,'items':{'type':'string','maxLength':200}}),
                (names&dynamic,['eq'],template['properties']['expected']),
                (names&dynamic,['gt','gte','lt','lte'],{'type':'number'})]
        for measures,operators,expected in groups:
            if not measures:continue
            item=copy.deepcopy(template)
            item['properties'].update(measure={'type':'string','enum':sorted(measures)},
                operator={'type':'string','enum':operators},expected=expected)
            variants.append(item)
    return {'anyOf':variants}


def browser_observation_schema(criteria: dict[str,str], *, _for_extension: bool=False) -> dict:
    observation={'type':'object','properties':{
        'selector':{'type':'string','minLength':1,'maxLength':300},
        'measure':{'type':'string','enum':['count','attached','visible','enabled','tag','text','value',
            'overflow_x','font_larger_than_body','file_nonempty','file_text']},
        'operator':{'type':'string','enum':['eq','includes','one_of','gt','gte','lt','lte']},
        'expected':{'anyOf':[{'type':'string','maxLength':1000},{'type':'number'},{'type':'boolean'},
            {'type':'array','minItems':1,'maxItems':20,'items':{'type':'string','maxLength':200}}]}},
        'required':['selector','measure','operator','expected'],'additionalProperties':False}
    action_variants=[]
    for name,value in [('fill',{'type':'string','maxLength':1000}),('click',{'type':'null'}),
                       ('select',{'type':'string','maxLength':1000}),('reload',{'type':'null'})]:
        # Reload is a page operation rather than a locator action. Requiring
        # the host token here keeps the model schema aligned with the compiler
        # and prevents CSS selectors such as :root from invalidating a whole
        # verification phase after authoring.
        selector=({'type':'string','enum':['page']} if name=='reload' else
                  {'type':'string','minLength':1,'maxLength':300})
        action_variants.append({'type':'object','properties':{
            'action':{'type':'string','enum':[name]},'selector':selector,'value':value},
            'required':['action','selector','value'],'additionalProperties':False})
    action={'anyOf':action_variants}
    layout_only=layout_browser_criteria(criteria)
    network_only=network_browser_criteria(criteria)
    if layout_only:
        observation['properties']['measure']['enum']=['count','attached','visible','overflow_x','file_nonempty']
    elif network_only:
        observation['properties']['measure']['enum']=['local_resources','external_request_count','dependency_count','file_nonempty','visible','attached']
    layout_ids={key for key,text in criteria.items() if layout_browser_criteria({key:text})}
    scenario_ids=multi_scenario_browser_criteria(criteria)
    scenario_labels=multi_scenario_browser_labels(criteria)
    check_count=max(1,len(criteria))+len(layout_ids)+sum(
        max(0,len(scenario_labels.get(key,[]))-1) for key in scenario_ids)
    check_properties={
        'ids':{'type':'array','minItems':1,'maxItems':1,'items':{'type':'string','enum':list(criteria)}},
        'kind':{'type':'string','enum':['BROWSER']},
        'actions':{'type':'array','maxItems':0 if layout_only or network_only else MAX_OBSERVATION_ACTIONS,'items':action},
        'observations':{'type':'array','minItems':1,'maxItems':12,
            'items':observation if _for_extension else _typed_observation_variants([observation])},
    }
    required=['ids','kind','actions','observations']
    if scenario_ids:
        check_properties['scenario']={'type':'string','enum':sorted({
            label for key in scenario_ids for label in scenario_labels.get(key,[])})}
        required.append('scenario')
    return {'type':'object','properties':{
        'entrypoint':{'type':'string'},'checks':{'type':'array','minItems':check_count,'maxItems':check_count,
            'items':{'type':'object','properties':check_properties,'required':required,'additionalProperties':False}}},
        'required':['entrypoint','checks'],'additionalProperties':False}


def verification_observation_schema(criteria: dict[str,str], *, allow_code: bool=True) -> dict:
    """Literal recovery checks, retaining code for dynamic/stateful comparisons."""
    schema=browser_observation_schema(criteria,_for_extension=True)
    properties=schema['properties']['checks']['items']['properties']
    observation=properties['observations']['items']
    def finish(templates):
        properties['observations']['items']=_typed_observation_variants(templates)
        return schema
    if network_browser_criteria(criteria):return finish([observation])
    viewport={'type':'object','properties':{'action':{'type':'string','enum':['viewport']},
        'selector':{'type':'string','enum':['page']},'value':{'type':'object','properties':{
            'width':{'type':'integer','minimum':240,'maximum':2560},
            'height':{'type':'integer','minimum':240,'maximum':2560}},
            'required':['width','height'],'additionalProperties':False}},
        'required':['action','selector','value'],'additionalProperties':False}
    storage_failure={'type':'object','properties':{
        'action':{'type':'string','enum':['storage_failure']},
        'selector':{'type':'string','enum':['localStorage']},'value':{'type':'boolean'}},
        'required':['action','selector','value'],'additionalProperties':False}
    properties['actions']['items']={'anyOf':[properties['actions']['items'],viewport,storage_failure]}
    if layout_browser_criteria(criteria):
        def fixed_viewport(width: int, height: int) -> dict:
            item=copy.deepcopy(viewport)
            item['properties']['value']['properties']['width']={'type':'integer','const':width}
            item['properties']['value']['properties']['height']={'type':'integer','const':height}
            return item
        properties['actions']['minItems']=1
        properties['actions']['maxItems']=1
        properties['actions']['items']={'anyOf':[fixed_viewport(375,812),fixed_viewport(1280,800)]}
        return finish([observation])
    scalar={'anyOf':[{'type':'string','maxLength':1000},{'type':'number'},{'type':'boolean'},{'type':'null'}]}
    fields={'export_name':{'type':'string','minLength':1,'maxLength':100},
        'arguments':{'type':'array','maxItems':8,'items':{'anyOf':[
            *scalar['anyOf'],{'type':'array','maxItems':20,'items':scalar}]}},
        'property_path':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'string','maxLength':100}}}
    variants=[observation]
    for measure,extra in [('module_call',['export_name','arguments']),('module_has_export',['export_name']),
                          ('json_field',['property_path']),('test_count',[])]:
        item=copy.deepcopy(observation)
        item['properties']['measure']={'type':'string','enum':[measure]}
        item['properties'].update({key:fields[key] for key in extra})
        item['required']=[*item['required'],*extra]
        variants.append(item)
    schema=finish(variants)
    if not declarative_browser_criteria(criteria) and allow_code:
        # Literal final-state observations cannot capture intermediate outputs
        # and derive relational assertions. Recovery must not remove that
        # capability from the ordinary verifier. Code stays line-framed and
        # goes through the same syntax/read-only/semantic gates as initial code.
        checks=schema['properties']['checks']
        checks['items']={'anyOf':[checks['items'],verification_schema(criteria)['properties']['checks']['items']]}
    return schema


def layout_browser_criteria(criteria: dict[str,str]) -> bool:
    # Only these generic contracts are owned geometry checks. Named menus,
    # breakpoints, arrangements, interactions or other richer requirements
    # retain their ordinary authored checks and independent semantic review.
    css=r'[A-Za-z0-9_./-]+\.css'
    patterns=(
        rf'(?:(?:{css}) (?:exists and )?(?:provides|contains|implements) (?:a |an )?)?(?:a |the )?(?:mobile-friendly )?responsive (?:layout|UI|design|page|interface)(?: for (?:both )?(?:mobile|smartphones?)(?: and desktop)?)?[.]?',
        r'The UI is usable on (?:both )?(?:mobile|smartphones?) and desktop[.]?',
        r'スマートフォンとデスクトップの両方で(?:画面|UI)が使いやすい[。.]?',
        rf'(?:{css}が存在し、)?スマートフォンとデスクトップで使いやすいレスポンシブレイアウトを含む[。.]?',
    )
    def generic(text):
        text=re.sub(r'^In the browser, verify the original requirement:\s*','',text,flags=re.I).strip()
        return any(re.fullmatch(pattern,text,re.I) for pattern in patterns)
    return bool(criteria) and all(generic(text) for text in criteria.values())


def multi_scenario_browser_criteria(criteria: dict[str,str]) -> set[str]:
    """Identify one criterion that explicitly requires several independent UI states."""
    return set(multi_scenario_browser_labels(criteria))


def multi_scenario_browser_labels(criteria: dict[str,str]) -> dict[str,list[str]]:
    scenarios=(
        ('empty_data',r'empty\s+(?:data|state|list)|no\s+(?:recipes|items|data)|空データ|空の(?:状態|一覧|データ)'),
        ('no_matching_items',r'no\s+matching|no\s+eligible|nothing\s+matches|条件に合う.{0,16}(?:ない|ありません)|該当する.{0,12}(?:ない|ありません)'),
        ('storage_error',r'save\s+error|storage\s+error|保存エラー|保存失敗|保存できない'),
    )
    test_contract=re.compile(r'(?:\btests?\s*/|\.(?:test|spec)\.[cm]?js\b|\btest\s+(?:file|suite|runner)\b|テスト(?:ファイル|コード|ケース|が))',re.I)
    ui_contract=re.compile(r'\b(?:browser|ui|screen|interface|display|render|handle|show)\b|ブラウザ|ブラウザー|UI|画面|表示|扱う|クリック',re.I)
    labels={key:[name for name,pattern in scenarios if re.search(pattern,text,re.I)]
            for key,text in criteria.items()
            if not test_contract.search(text) and ui_contract.search(text)}
    return {key:value for key,value in labels.items() if len(value)>=2}


def network_browser_criteria(criteria: dict[str,str]) -> bool:
    return bool(criteria) and all(re.fullmatch(
        r'The app can be opened in a browser without external libraries or network calls\.?',text,re.I)
        for text in criteria.values())


def compile_browser_observations(value: Any, criteria: dict[str,str] | None = None,
                                 sources: dict[str,str] | None = None) -> Any:
    """Compile literal DOM observations to owned, awaited read-only checks.

    Legacy code checks remain supported. Selectors, input and expected values
    are encoded as JSON literals, never interpolated as JavaScript programs.
    """
    if not isinstance(value,dict) or not isinstance(value.get('checks'),list):return value
    result=copy.deepcopy(value)
    criteria=criteria or {}
    sources=sources or {}
    layout_ids={key for key,text in criteria.items() if layout_browser_criteria({key:text})}
    scenario_labels=multi_scenario_browser_labels(criteria)
    scenario_ids=set(scenario_labels)
    if layout_ids:
        for identifier in layout_ids:
            matches=[check for check in result['checks'] if isinstance(check,dict)
                     and check.get('ids')==[identifier]]
            owned=standard_verification_checks({identifier:criteria[identifier]})
            if matches and matches==owned:
                continue  # Exact host-owned rendered layout checks need no model selectors.
            if len(matches)!=2:
                raise ValueError(f'{identifier} needs separate mobile and desktop viewport checks')
            widths=[]
            criterion=criteria[identifier]
            stylesheet_match=re.search(r'(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+\.css)(?![A-Za-z0-9_./-])',criterion,re.I)
            for check in matches:
                actions=check.get('actions')
                if (check.get('kind')!='BROWSER' or not isinstance(actions,list) or len(actions)!=1
                        or not isinstance(actions[0],dict) or actions[0].get('action')!='viewport'
                        or actions[0].get('selector')!='page'):
                    raise ValueError(f'{identifier} must use one viewport action per check')
                viewport=actions[0].get('value')
                if not isinstance(viewport,dict) or set(viewport)!={'width','height'}:
                    raise ValueError(f'{identifier} has an invalid viewport')
                # Models often interpret "375x812" as height-by-width. The
                # host owns these two fixed responsive breakpoints, so repair
                # only the exact reversed pairs before validation.
                reversed_viewports={(812,375):(375,812),(800,1280):(1280,800)}
                pair=(viewport.get('width'),viewport.get('height'))
                if all(type(dimension) is int for dimension in pair) and pair in reversed_viewports:
                    width,height=reversed_viewports[pair]
                    actions[0]['value']={'width':width,'height':height}
                    viewport=actions[0]['value']
                if viewport['width']==375 and viewport['height']==812:
                    widths.append(375)
                elif viewport['width']==1280 and viewport['height']==800:
                    widths.append(1280)
                else:
                    raise ValueError(f'{identifier} must observe 375x812 and 1280x800 separately')
                observations=check.get('observations')
                if not isinstance(observations,list):
                    raise ValueError(f'{identifier} requires viewport observations')
                overflow=next((item for item in observations if isinstance(item,dict)
                    and item.get('measure')=='overflow_x' and item.get('operator')=='eq'
                    and item.get('expected') is False),None)
                if overflow is None:
                    overflow={'selector':'body','measure':'overflow_x','operator':'eq','expected':False}
                visible=next((item for item in observations if isinstance(item,dict)
                    and item.get('measure')=='visible' and item.get('operator')=='eq'
                    and item.get('expected') is True),None)
                if visible is None:
                    selector=_layout_control_selector(sources)
                    if selector is None:
                        raise ValueError(f'{identifier} needs a real visible control from the HTML source')
                    visible={'selector':selector,'measure':'visible','operator':'eq','expected':True}
                canonical=[overflow,visible,*(item for item in observations if item not in (overflow,visible))]
                if stylesheet_match:
                    stylesheet=next((item for item in observations if isinstance(item,dict)
                        and item.get('measure')=='file_nonempty'
                        and item.get('selector')==stylesheet_match.group(1)
                        and item.get('operator')=='eq' and item.get('expected') is True),None)
                    file_observation=stylesheet or {'selector':stylesheet_match.group(1),
                        'measure':'file_nonempty','operator':'eq','expected':True}
                    if file_observation not in canonical:canonical.append(file_observation)
                # Preserve authored observations. Silently deleting a second
                # control or region assertion weakens the check's coverage.
                check['observations']=canonical
            if sorted(widths)!=[375,1280]:
                raise ValueError(f'{identifier} must contain one mobile and one desktop observation')
    if scenario_labels:
        result['checks']=_canonicalize_scenario_checks(result['checks'],scenario_labels,criteria,sources)
    for check in result['checks']:
        if not isinstance(check,dict) or 'observations' not in check:continue
        if 'code' in check or check.get('kind')!='BROWSER':raise ValueError('Observations require BROWSER without raw code')
        if 'scenario' in check:
            if check.get('scenario') not in {'empty_data','no_matching_items','storage_error'}:
                raise ValueError('Invalid independent UI scenario')
            check.pop('scenario')
        actions,observations=check.get('actions'),check['observations']
        if not isinstance(actions,list) or len(actions)>MAX_OBSERVATION_ACTIONS or not isinstance(observations,list) or not 1<=len(observations)<=12:
            raise ValueError(f'Use at most {MAX_OBSERVATION_ACTIONS} actions and 1-12 observations')
        layout_only=layout_browser_criteria(criteria)
        network_only=network_browser_criteria(criteria)
        if layout_only and any(not isinstance(action,dict) or action.get('action')!='viewport' for action in actions):
            raise ValueError('Layout-only checks observe the viewport without input or clicks')
        if network_only and actions:raise ValueError('Network loading checks observe without input or clicks')
        multiline_fills={action.get('selector') for action in actions if isinstance(action,dict)
            and action.get('action')=='fill' and isinstance(action.get('value'),str)
            and re.search(r'[\r\n]',action['value'])}
        single_line_tags={item.get('selector') for item in observations if isinstance(item,dict)
            and item.get('measure')=='tag' and item.get('operator')=='eq' and item.get('expected')=='input'}
        for item in observations:
            if (isinstance(item,dict) and item.get('selector') in multiline_fills & single_line_tags
                    and item.get('measure')=='value' and item.get('operator')=='eq'
                    and isinstance(item.get('expected'),str) and re.search(r'[\r\n]',item['expected'])):
                raise ValueError('Contradictory check: a fillable input element cannot preserve multiline text. '
                    'Verify the original multiline behavior without also requiring a single-line input tag.')
        lines=[]
        literal=lambda item:json.dumps(item,ensure_ascii=False,allow_nan=False)
        for action in actions:
            if not isinstance(action,dict) or set(action)!={'action','selector','value'}:raise ValueError('Invalid DOM action')
            if action.get('action')=='reload' and action.get('value') is None:
                # The operation always reloads the whole document; older or
                # schema-drifted contracts sometimes used a DOM-root selector
                # even though it never selected a partial reload target.
                action['selector']='page'
            selector=action['selector']
            if not isinstance(selector,str) or not 0<len(selector)<=300:raise ValueError('Invalid action selector')
            locator='page.locator('+literal(selector)+')'
            if action['action']=='fill' and isinstance(action['value'],str) and len(action['value'])<=1000:
                lines.append('await '+locator+'.fill('+literal(action['value'])+');')
            elif action['action']=='click' and action['value'] is None:
                lines.append('await '+locator+'.click();')
            elif action['action']=='select' and isinstance(action['value'],str) and len(action['value'])<=1000:
                lines.append('await '+locator+'.selectOption('+literal(action['value'])+');')
            elif action['action']=='reload' and selector=='page' and action['value'] is None:
                lines.append('await page.reload();')
            elif action['action']=='storage_failure' and selector=='localStorage' and type(action['value']) is bool:
                lines.append('await setStorageFailure('+literal(action['value'])+');')
            elif action['action']=='viewport' and selector=='page':
                viewport=action['value']
                if (not isinstance(viewport,dict) or set(viewport)!={'width','height'}
                        or any(type(value) is not int or not 240<=value<=2560 for value in viewport.values())):
                    raise ValueError('Invalid viewport dimensions')
                lines.append('await page.setViewportSize('+literal(viewport)+');')
            else:raise ValueError('Only fill(string), click(null), select(string), reload(page), storage_failure and bounded viewport dimensions are available')
        for index,observation in enumerate(observations):
            if not isinstance(observation,dict):
                raise ValueError('Invalid DOM observation')
            extra=({'export_name','arguments'} if observation.get('measure')=='module_call' else
                   {'export_name'} if observation.get('measure')=='module_has_export' else
                   {'property_path'} if observation.get('measure')=='json_field' else set())
            if set(observation)!={'selector','measure','operator','expected'}|extra:
                raise ValueError('Invalid DOM observation')
            selector,measure,operator,expected=(observation[key] for key in ('selector','measure','operator','expected'))
            if measure in {'attached','visible','enabled','overflow_x','font_larger_than_body','file_nonempty','local_resources','module_has_export'}:
                if operator!='eq' or type(expected) is not bool:
                    raise ValueError('Boolean observations require eq and a boolean expected value')
            if measure in {'count','external_request_count','dependency_count','test_count'}:
                if operator not in {'eq','gt','gte','lt','lte'} or type(expected) not in {int,float}:
                    raise ValueError('Numeric observations require a numeric expected value and comparison')
            if layout_only and measure not in {'count','attached','visible','overflow_x','file_nonempty'}:
                raise ValueError('Layout-only checks use visibility, attachment, counts, overflow and file presence')
            if network_only and measure not in {'local_resources','external_request_count','dependency_count','file_nonempty','visible','attached'}:
                raise ValueError('Network loading checks use resource origins, requests, dependencies and presence')
            if not isinstance(selector,str) or not 0<len(selector)<=300:raise ValueError('Invalid observation selector')
            if not (type(expected) in {bool,int,float} or isinstance(expected,str) and len(expected)<=1000
                    or isinstance(expected,list) and 1<=len(expected)<=20 and all(isinstance(item,str) and len(item)<=200 for item in expected)):
                raise ValueError('Invalid observation expected value')
            locator='page.locator('+literal(selector)+')'
            expressions={'count':'await '+locator+'.count()',
                'attached':'(await '+locator+'.count())>0','visible':'await '+locator+'.isVisible()',
                'enabled':'await '+locator+'.isEnabled()',
                'tag':'await '+locator+'.evaluate(el=>el.tagName.toLowerCase())',
                'text':'await (async()=>{const values=await '+locator+'.allTextContents();assert.ok(values.length>0,"Missing text target");return values.join("\\n");})()',
                'value':'await '+locator+'.inputValue()',
                'overflow_x':('await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1)'
                    if selector in {'html','body'} else 'await '+locator+'.evaluate(el=>el.scrollWidth>el.clientWidth+1)'),
                'font_larger_than_body':'await '+locator+'.evaluate(el=>parseFloat(getComputedStyle(el).fontSize)>parseFloat(getComputedStyle(document.body).fontSize))',
                'file_nonempty':'readFile('+literal(selector)+').trim().length>0','file_text':'readFile('+literal(selector)+')',
                'local_resources':"await page.locator('script[src],link[rel=stylesheet][href]').evaluateAll(els=>els.every(el=>{const url=new URL(el.tagName==='SCRIPT'?el.src:el.href,document.baseURI);return url.origin===new URL(document.baseURI).origin||['data:','blob:'].includes(url.protocol)}))",
                'external_request_count':'__olcr_context.externalRequestCount()',
                'dependency_count':"(()=>{if(!require('node:fs').existsSync('package.json'))return 0;const pkg=JSON.parse(readFile('package.json'));return new Set(['dependencies','devDependencies','optionalDependencies'].flatMap(key=>Object.keys(pkg[key]||{}))).size})()"}
            if measure in {'module_call','module_has_export'}:
                name=observation['export_name']
                if not isinstance(name,str) or not 0<len(name)<=100:raise ValueError('Invalid export name')
                if measure=='module_call':
                    arguments=observation['arguments']
                    def valid_argument(value):
                        return value is None or type(value) in {bool,int,float} or isinstance(value,str) and len(value)<=1000
                    if (not isinstance(arguments,list) or len(arguments)>8 or not all(valid_argument(arg) or
                            isinstance(arg,list) and len(arg)<=20 and all(valid_argument(item) for item in arg) for arg in arguments)):
                        raise ValueError('Invalid module arguments')
                    expressions[measure]='await (async()=>{const m=await loadModule('+literal(selector)+');const fn=m['+literal(name)+'];assert.equal(typeof fn,"function");return await fn(...'+literal(arguments)+');})()'
                else:
                    expressions[measure]='Object.hasOwn(await loadModule('+literal(selector)+'),'+literal(name)+')'
            if measure=='json_field':
                keys=observation['property_path']
                if not isinstance(keys,list) or not 1<=len(keys)<=8 or not all(isinstance(key,str) and len(key)<=100 for key in keys):
                    raise ValueError('Invalid JSON property path')
                expressions[measure]=literal(keys)+'.reduce((value,key)=>value?.[key],JSON.parse(readFile('+literal(selector)+')))'
            if measure=='test_count':expressions[measure]='(await runTests()).tests'
            if measure not in expressions:raise ValueError('Unsupported observation measure')
            actual='observed_'+str(index)
            lines.append('const '+actual+'='+expressions[measure]+';')
            if operator=='eq':
                if not isinstance(expected,(str,bool,int,float,list)):raise ValueError('Invalid equality value')
                lines.append('assert.deepEqual('+actual+','+literal(expected)+');')
            elif operator=='includes' and isinstance(expected,str) and expected:
                lines.append('assert.equal(typeof '+actual+',"string");assert.ok('+actual+'.includes('+literal(expected)+'));')
            elif operator=='one_of' and isinstance(expected,list) and expected and all(isinstance(item,str) for item in expected):
                lines.append('assert.ok('+literal(expected)+'.includes('+actual+'));')
            elif operator in {'gt','gte','lt','lte'} and type(expected) in {int,float}:
                symbol={'gt':'>','gte':'>=','lt':'<','lte':'<='}[operator]
                lines.append('assert.equal(typeof '+actual+',"number");assert.ok('+actual+symbol+literal(expected)+');')
            else:raise ValueError('Invalid observation operator/value')
        check.pop('actions');check.pop('observations');check['code']='\n'.join(lines)
    return result


def _inner_html_source(scripts: str) -> str:
    """Collect literal markup assigned to innerHTML without executing JavaScript."""
    form_variables={match.group(1) for match in re.finditer(
        r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[^;]{0,500}?document\s*\.\s*createElement\s*\(\s*([\'\"])form\2\s*\)',
        scripts,re.I|re.S)}
    markup=[]
    assignments=re.finditer(r'(?P<receiver>[A-Za-z_$][\w$]*)\s*\.\s*innerHTML\s*=',scripts,re.I)
    for assignment in assignments:
        start=assignment.end();end=start;quote='';escaped=False
        while end<len(scripts):
            char=scripts[end]
            if quote:
                if escaped:escaped=False
                elif char=='\\':escaped=True
                elif char==quote:quote=''
            elif char in {'\'', '"', '`'}:quote=char
            elif char==';':break
            end+=1
        expression=scripts[start:end]
        fragments=[];index=0
        while index<len(expression):
            if expression[index] not in {'\'', '"'}:
                index+=1;continue
            quote=expression[index];stop=index+1;escaped=False
            while stop<len(expression):
                char=expression[stop]
                if escaped:escaped=False
                elif char=='\\':escaped=True
                elif char==quote:break
                stop+=1
            if stop>=len(expression):break
            literal=expression[index:stop+1]
            try:value=ast.literal_eval(literal)
            except (SyntaxError,ValueError):
                value=literal[1:-1].replace('\\n','\n').replace('\\r','\r').replace('\\t','\t')
            if isinstance(value,str) and re.search(r'<\s*(?:input|select|textarea|button|form|div|section|ul|ol)\b',value,re.I):
                fragments.append(value)
            index=stop+1
        combined=''.join(fragments)
        if combined:
            if assignment.group('receiver') in form_variables:
                combined='<form>'+combined+'</form>'
            markup.append(combined)
    return '\n'.join(markup)


def _canonicalize_scenario_checks(checks: list[dict], scenario_labels: dict[str,list[str]],
                                  criteria: dict[str,str], sources: dict[str,str], *,
                                  preflight_only: bool=False) -> list[dict]:
    """Make each required UI scenario a single, host-owned executable check.

    Local models often duplicate one scenario while omitting another, and they
    frequently place the storage-failure switch after the save action. Rather
    than spending another model call on a fixed check shape, normalize these
    scenarios from the actual form controls and copy only a source-backed empty
    state observation supplied by the author.
    """
    html=next((source for name,source in sources.items()
               if Path(name).suffix.lower() in {'.html','.htm'}), '')
    scripts='\n'.join(source for name,source in sources.items()
                      if Path(name).suffix.lower() in {'.js','.mjs','.cjs'})
    html += '\n' + _inner_html_source(scripts)

    def attr(attributes: str, name: str) -> str:
        match=re.search(r'\b'+re.escape(name)+r'\s*=\s*(["\'])(.*?)\1',attributes,re.I|re.S)
        if match:return match.group(2)
        match=re.search(r'\b'+re.escape(name)+r'\s*=\s*([^\s>]+)',attributes,re.I)
        return match.group(1).strip('"\'') if match else ''

    elements=[]
    for match in re.finditer(r'<(button|input|select|textarea|form|div|section|ul|ol|output|p|span)\b([^>]*)>',html,re.I):
        tag,attributes=match.group(1).lower(),match.group(2)
        identifier=attr(attributes,'id')
        elements.append((tag,attributes,identifier))

    def selector_for(pattern: str, tags: set[str] | None=None) -> str | None:
        for tag,attributes,identifier in elements:
            if tags and tag not in tags:continue
            name=attr(attributes,'name')
            if identifier and re.search(pattern,identifier,re.I):return '#'+identifier
            if name and re.search(pattern,name,re.I):return f'{tag}[name={json.dumps(name,ensure_ascii=False)}]'
        return None

    def button_for_text(pattern: str) -> str | None:
        for match in re.finditer(r'<button\b([^>]*)>(.*?)</button\s*>',html,re.I|re.S):
            attributes,body=match.group(1),re.sub(r'<[^>]+>',' ',match.group(2))
            if not re.search(pattern,body,re.I):continue
            identifier=attr(attributes,'id')
            if identifier:return '#'+identifier
            tab=attr(attributes,'data-tab')
            if tab:return f'button[data-tab={json.dumps(tab,ensure_ascii=False)}]'
            return 'text='+body.strip()
        button_variables=set(re.findall(
            r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*document\s*\.\s*createElement\s*\(\s*[\'\"]button[\'\"]\s*\)',
            scripts,re.I))
        for variable in button_variables:
            for match in re.finditer(r'\b'+re.escape(variable)+r'\.(?:textContent|innerText)\s*=\s*([\'\"])(.*?)\1',scripts,re.I|re.S):
                label=match.group(2)
                if re.search(pattern,label,re.I):return 'text='+label.strip()
        return None

    add_selector=selector_for(r'(?:add|new).*(?:dish|recipe|meal|food)|(?:dish|recipe).*(?:add|new)',{'button'})
    if add_selector is None:
        for match in re.finditer(r'<button\b([^>]*)>(.*?)</button\s*>',html,re.I|re.S):
            if re.search(r'(?:料理|レシピ|食事).{0,8}追加|add\s+(?:dish|recipe)',match.group(2),re.I):
                identifier=attr(match.group(1),'id')
                if identifier:add_selector='#'+identifier;break
    add_is_management_tab=False
    if add_selector is None:
        for tag,attributes,identifier in elements:
            tab=attr(attributes,'data-tab')
            if tag=='button' and tab.lower() in {'meal','meals','dish','dishes','recipe','recipes'}:
                add_selector=f'button[data-tab={json.dumps(tab,ensure_ascii=False)}]'
                add_is_management_tab=True
                break
    if add_selector is None:add_selector=button_for_text(r'(?:料理|レシピ|食事).{0,8}追加|add\s+(?:dish|recipe)')
    plan_tab_selector=next((f'button[data-tab={json.dumps(attr(attributes,"data-tab"),ensure_ascii=False)}]'
        for tag,attributes,_ in elements if tag=='button' and attr(attributes,'data-tab').lower() in {'plan','week'}),None)
    plan_selector=(selector_for(r'(?:generate|create[-_]?plan|make[-_]?plan)',{'button'})
        or button_for_text(r'(?:plan|generate|献立).{0,8}(?:作成|生成)|(?:作成|生成).{0,8}(?:plan|meal)')
        or next((('#'+identifier) if identifier else f'button[name={json.dumps(attr(attributes,"name"),ensure_ascii=False)}]'
                 for tag,attributes,identifier in elements if tag=='button'
                 and re.search(r'(?:^|[-_])(?:plan|week)(?:$|[-_])',identifier+' '+attr(attributes,'name'),re.I)
                 and not re.search(r'clear|reset|delete|remove|クリア|削除',identifier+' '+attr(attributes,'name'),re.I)),None))
    budget_selector=selector_for(r'(?:budget[-_]?(?:input|limit|cap|max|upper)|(?:plan|weekly|week)[-_]?budget|予算(?:上限|制限)|週予算)',{'input'})
    dish_list_selector=selector_for(r'(?:dish|recipe|food).*(?:list|items?)|(?:list|items?).*(?:dish|recipe|food)',
                                    {'div','section','ul','ol'})
    if dish_list_selector is None:
        dish_list_selector=selector_for(r'(?:dish|recipe|food)',{'div','section','ul','ol'})
    toast_selector=selector_for(r'(?:toast|notice|error|message|alert)',{'div','section','output','p','span'})
    if toast_selector is None:
        toast_selector=selector_for(r'status',{'div','section','output','p','span'})

    name_selector=cost_selector=submit_selector=None
    inline_form_available=False
    minimal_form_setup=True
    no_match_form_setup=True
    for form_match in re.finditer(r'<form\b([^>]*)>(.*?)</form\s*>',html,re.I|re.S):
        form_attributes=form_match.group(1)
        form_id=attr(form_attributes,'id')
        body=form_match.group(2)
        inputs=[]
        for input_match in re.finditer(r'<(input|textarea|select)\b([^>]*)>',body,re.I):
            tag,attributes=input_match.group(1).lower(),input_match.group(2)
            inputs.append((tag,attributes,attr(attributes,'id'),attr(attributes,'name')))
        fillable_types={'','text','search','email','url','tel','number','password'}
        fillable_inputs=[item for item in inputs if item[0]=='textarea' or
                         item[0]=='input' and attr(item[1],'type').lower() in fillable_types]
        def control_selector(tag: str, attributes: str, identifier: str, name: str) -> str | None:
            if identifier:return '#'+identifier
            if name:return f'{tag}[name={json.dumps(name,ensure_ascii=False)}]'
            return None
        name=next(((tag,attributes,identifier,name) for tag,attributes,identifier,name in fillable_inputs if
                   re.search(r'(?:name|title|dish|recipe)',identifier+' '+name,re.I)),None)
        if name is None:
            name=next((item for item in fillable_inputs if item[0]=='textarea' or
                       attr(item[1],'type').lower() in {'text',''}),None)
        if not name:continue
        name_selector=control_selector(*name)
        cost=next((item for item in fillable_inputs if re.search(r'(?:cost|price|budget|amount)',item[2]+' '+item[3],re.I)),None)
        cost_selector=control_selector(*cost) if cost else None
        buttons=list(re.finditer(r'<button\b([^>]*)>(.*?)</button\s*>',body,re.I|re.S))
        button=next((b for b in buttons if attr(b.group(1),'type').lower()=='submit'),None)
        button=button or next((b for b in buttons if attr(b.group(1),'type').lower() not in {'button','reset'}),None)
        if button is None:
            # A settings form may contain text/number inputs but have its
            # action button outside the form. It is not the recipe editor and
            # must not be treated as a save form.
            continue
        button_id=attr(button.group(1),'id') if button else ''
        button_type=attr(button.group(1),'type').lower() if button else ''
        form_selector='#'+form_id if form_id else None
        form_style=attr(form_attributes,'style')
        form_classes=set(attr(form_attributes,'class').lower().split())
        inline_form_available=not (
            re.search(r'(?:^|\s)hidden(?:\s|=|$)',form_attributes,re.I)
            or attr(form_attributes,'aria-hidden').lower()=='true'
            or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)',form_style,re.I)
            or form_classes.intersection({'hidden','is-hidden','u-hidden'}))
        submit_selector=('#'+button_id if button_id else
                         (form_selector+' ' if form_selector else 'form ')+
                         ('button[type='+json.dumps(button_type)+']' if button_type else 'button:not([type])'))
        minimal_form_setup=all(item==name or not re.search(r'\brequired(?:\s|=|$)',item[1],re.I)
            or bool(attr(item[1],'value')) or attr(item[1],'type').lower()=='hidden'
            for item in inputs)
        no_match_form_setup=all(item in (name,cost) or not re.search(r'\brequired(?:\s|=|$)',item[1],re.I)
            or bool(attr(item[1],'value')) or attr(item[1],'type').lower()=='hidden'
            for item in inputs)
        break

    def require_no_match_capability(identifier: str) -> None:
        missing=[]
        if not budget_selector:
            missing.append('a user-set budget condition control' if
                re.search(r'budget|予算',criteria.get(identifier,''),re.I) else
                'a user-set condition control')
        elif not ((add_selector or inline_form_available) and name_selector and cost_selector
                  and submit_selector and plan_selector and toast_selector and no_match_form_setup):
            missing.append('a source-backed no-match interaction')
        if not missing:return
        controls=[{'tag':tag,'id':identifier_value,'name':attr(attributes,'name')}
            for tag,attributes,identifier_value in elements
            if tag in {'input','select','textarea','button'}
            and (identifier_value or attr(attributes,'name'))]
        raise VerificationScenarioCapabilityGap(identifier,'no_matching_items',
            criteria.get(identifier,''),missing,controls)

    for identifier,labels in scenario_labels.items():
        if preflight_only and 'no_matching_items' in labels:
            # This discovery limitation is advisory; dynamic or differently
            # named controls must be checked through actual UI interactions.
            require_no_match_capability(identifier)
    if preflight_only:
        return checks

    lang_match=re.search(r'<html\b[^>]*\blang\s*=\s*(["\'])(.*?)\1',html,re.I)
    japanese=bool((lang_match and lang_match.group(2).lower().startswith('ja')) or
                  any(re.search(r'[\u3040-\u30ff\u3400-\u9fff]',criteria[key]) for key in scenario_labels))

    def error_message() -> str | None:
        for block in re.finditer(r'catch\s*\([^)]*\)\s*\{([^{}]{0,800})\}',scripts,re.I|re.S):
            message=re.search(r'(?:show(?:Toast|Notice|Message|Error|Alert|Status|Notification)|textContent\s*=)\s*\(?\s*(["\'])(.*?)\1',block.group(1),re.I|re.S)
            if message:return message.group(2)
        return None

    def no_match_message(identifier: str) -> str | None:
        message_pattern=r'no\s+(?:matching|eligible)|conditions?|条件|該当|対象'
        preferred_message_pattern=(r'no\s+(?:matching|eligible)\b|'
            r'(?:条件に合う|該当する).{0,24}(?:ありません|いません|見つかりません|存在しません|ない場合|いない場合)')
        messages=[]
        # Notifications are often routed through a small generic helper (for
        # example notify(success ? "..." : "条件に合う料理がありません") ).
        # Inspect every literal in the actual call, not only its first branch.
        call_pattern=r'\b(?:notify|show(?:Toast|Notice|Message|Error|Alert|Status|Notification))\s*\('
        literal_pattern=re.compile(r'''(`(?:\\.|[^`])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',re.S)
        for call in re.finditer(call_pattern,scripts,re.I):
            depth=1;quote='';escaped=False;end=call.end()
            while end<len(scripts) and depth:
                char=scripts[end]
                if quote:
                    if escaped:escaped=False
                    elif char=='\\':escaped=True
                    elif char==quote:quote=''
                elif char in {'"',"'",'`'}:quote=char
                elif char=='(':depth+=1
                elif char==')':depth-=1
                end+=1
            body=scripts[call.end():end-1]
            for literal in literal_pattern.finditer(body):
                message=re.sub(r'\$\{[^{}]*\}','',literal.group(0)[1:-1]).strip()
                if re.search(message_pattern,message,re.I):messages.append(message)
        for match in re.finditer(r"textContent\s*=\s*([`'\"])(.*?)\1",scripts,re.I|re.S):
            # Keep user guidance around dynamic reason text, e.g. a direct
            # textContent assignment to a template string.
            message=re.sub(r'\$\{[^{}]*\}','',match.group(2)).strip()
            if re.search(message_pattern,message,re.I):messages.append(message)
        return (next((message for message in messages
                      if re.search(preferred_message_pattern,message,re.I)),None)
                or next(iter(messages),None))

    def authored_empty(identifier: str, pool: list[dict]) -> dict | None:
        candidates=[check for check in pool if check.get('scenario')=='empty_data']
        candidates.sort(key=lambda check: (bool(check.get('actions')), -len(check.get('observations') or [])))
        for check in candidates:
            observations=[item for item in (check.get('observations') or []) if isinstance(item,dict)]
            if any(item.get('measure')=='text' or item.get('measure')=='count'
                   and item.get('operator')=='eq' and item.get('expected')==0 for item in observations):
                return {'ids':[identifier],'kind':'BROWSER','actions':[],
                        'observations':observations[:4]}
        if dish_list_selector:
            message='料理がありません' if japanese else 'No recipes'
            return {'ids':[identifier],'kind':'BROWSER','actions':[],
                    'observations':[{'selector':dish_list_selector+' .empty','measure':'text',
                                     'operator':'includes','expected':message}]}
        return None

    def build(identifier: str, scenario: str, pool: list[dict]) -> dict | None:
        if scenario=='empty_data':
            return authored_empty(identifier,pool)
        candidate=next((check for check in pool if check.get('scenario')==scenario),None)
        if candidate and scenario=='no_matching_items':
            return {**candidate,'ids':[identifier]}
        if scenario=='storage_error' and add_selector and name_selector and submit_selector and toast_selector and error_message():
            actions=(candidate or {}).get('actions') or []
            def selector_key(value):
                return re.sub(r'''\[([\w-]+)\s*=\s*(["'])(.*?)\2\]''',r'[\1=\3]',str(value))
            def action_index(kind,selector):
                return next((index for index,action in enumerate(actions) if isinstance(action,dict)
                    and action.get('action')==kind
                    and selector_key(action.get('selector'))==selector_key(selector)),None)
            opener=action_index('click',add_selector)
            name_fill=action_index('fill',name_selector)
            submit=action_index('click',submit_selector)
            if (candidate and opener is not None and name_fill is not None and submit is not None
                    and opener<name_fill<submit and all(isinstance(action,dict) for action in actions)):
                # Preserve source-backed setup for other required controls.
                # The host only fixes fault ordering; replacing this flow
                # with a name-only form can prevent native HTML submission.
                return {**candidate,'ids':[identifier],'actions':[
                    {'action':'storage_failure','selector':'localStorage','value':True},
                    *(action for action in actions if action.get('action')!='storage_failure')]}
            if not minimal_form_setup:
                # Unknown required controls need authored interactions. Do
                # not invent an incomplete save path or change application
                # validation to make a synthetic scenario submit.
                return {**candidate,'ids':[identifier]} if candidate else None
            return {'ids':[identifier],'kind':'BROWSER','actions':[
                {'action':'storage_failure','selector':'localStorage','value':True},
                {'action':'click','selector':add_selector,'value':None},
                {'action':'fill','selector':name_selector,'value':'OLCR保存失敗確認'},
                {'action':'click','selector':submit_selector,'value':None}],
                'observations':[{'selector':toast_selector,'measure':'text','operator':'includes',
                                 'expected':error_message()}],
                'scenario':scenario}
        if (scenario=='no_matching_items' and (add_selector or inline_form_available)
                and name_selector and cost_selector
                and no_match_form_setup and budget_selector and plan_selector and toast_selector and no_match_message(identifier)):
            actions=[]
            if add_selector:
                actions.append({'action':'click','selector':add_selector,'value':None})
            actions.extend([
                {'action':'fill','selector':name_selector,'value':'OLCR予算外確認'},
                {'action':'fill','selector':cost_selector,'value':'999999'},
                {'action':'click','selector':submit_selector,'value':None},
                {'action':'fill','selector':budget_selector,'value':'1'}])
            if add_is_management_tab and plan_tab_selector:
                actions.append({'action':'click','selector':plan_tab_selector,'value':None})
            actions.append({'action':'click','selector':plan_selector,'value':None})
            return {'ids':[identifier],'kind':'BROWSER','actions':actions,
                'observations':[{'selector':toast_selector,'measure':'text','operator':'includes',
                                 'expected':no_match_message(identifier)}],
                'scenario':scenario}
        # If the current source has no ordinary form for this scenario, retain
        # one model-authored check rather than fabricating a test interface.
        if candidate:
            return {**candidate,'ids':[identifier]}
        return None

    result=[]
    for check in checks:
        if not isinstance(check,dict):
            result.append(check);continue
        ids=check.get('ids') if isinstance(check.get('ids'),list) else []
        remaining=[identifier for identifier in ids if identifier not in scenario_labels]
        if remaining:
            result.append({**check,'ids':remaining})
    for identifier,labels in scenario_labels.items():
        pool=[check for check in checks if isinstance(check,dict)
              and identifier in (check.get('ids') or [])]
        for scenario in labels:
            canonical=build(identifier,scenario,pool)
            if canonical is None:
                if scenario=='no_matching_items':
                    require_no_match_capability(identifier)
                raise ValueError(f'{identifier} requires a real source-backed {scenario} check')
            result.append(canonical)
    return result


def verify_required_scenario_capabilities(criteria: dict[str,str], sources: dict[str,str]) -> list[dict]:
    """Discovery is advisory: an unknown selector is not proof of missing behavior."""
    labels=multi_scenario_browser_labels(criteria or {})
    if labels:
        try:
            _canonicalize_scenario_checks([],labels,criteria or {},sources or {},preflight_only=True)
        except VerificationScenarioCapabilityGap as exc:
            return [exc.evidence()]
    return []


def _layout_control_selector(sources: dict[str,str]) -> str | None:
    """Pick a real static control selector when a layout check omitted one."""
    for name,source in sources.items():
        if Path(name).suffix.lower()!='.html' or re.search(r'\.(?:test|spec)\.[cm]?js$',name):
            continue
        for match in re.finditer(r'<(button|input|select|textarea|a)\b([^>]*)>',source,re.I):
            tag,attributes=match.group(1).lower(),match.group(2)
            if re.search(r'\shidden(?:\s|=|/?>)',attributes,re.I):
                continue
            if tag=='input' and re.search(r'\btype\s*=\s*["\']?hidden\b',attributes,re.I):
                continue
            identifier=re.search(r'\bid\s*=\s*(["\'])(.*?)\1',attributes,re.I)
            if identifier and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',identifier.group(2)):
                return '#'+identifier.group(2)
            return tag
    return None


def browser_observation_prompt(original: str, sources: dict[str,str], criteria: dict[str,str]) -> str:
    return ('Describe executable read-only browser checks using literal actions and observations. Return JSON only, no JavaScript or code field. '
        f'Each check has ids, kind BROWSER, actions (0-{MAX_OBSERVATION_ACTIONS} items), observations (1-12 items). '
        'Action fields: fill (string), click (null), select (string option value on the real owning <select>), or reload (selector="page", value=null). '
        'Never click an <option>: native options are not visible click targets. Select the owning <select> using its actual CSS selector and the exact option value. '
        'For persistence, perform the normal user-facing save or assignment, reload the same page, then observe the restored rendered UI; reading storage alone is not proof of restoration. '
        + ('For scenario checks, actions may also use exactly '
            '{"action":"storage_failure","selector":"localStorage","value":true} to force writes to fail; '
            'place it before the real UI save actions. This is an action item, never a top-level check property. '
            if multi_scenario_browser_criteria(criteria) else '')
        + 'Observation fields: selector, measure, operator, expected. '
        'Measures: count (number), attached/visible/enabled (boolean), tag (lowercase HTML tag), text/value (string), '
        'overflow_x (boolean: horizontal overflow), font_larger_than_body (boolean), file_nonempty (boolean), file_text (string). '
        'For file measures selector is a relative actual file path. Operators: eq, includes, one_of (string array), gt/gte/lt/lte (number). '
        'OLCR compiles these into actual awaited browser observations and assertions. Do not generate source code, mocks or DOM mutations. '
        'Use actual source selectors, not guessed nesting. A textarea is a tag, not a type attribute. '
        'For initial UI structure use attached for empty result/history containers, and visible for input/button controls. '
        'Empty history can have zero height. Do not require initial result text or populated history. '
        'To observe result/history text, visibility or large result font, first fill valid input and click; do not click a disabled button. '
        'Only observe the supplied criterion. Do not freeze incidental rows, class names, CSS values or startup contents as requirements. '
        'For mobile layout set viewport width=375 and height=812; for desktop set width=1280 and height=800. '
        'Width is the horizontal value and height is the vertical value. '
        'Every viewport check must observe overflow_x=false and a visible usable control. '
        'A selection from a single eligible candidate has an exact expected result; multiple candidates require membership, not a particular random choice. '
        'The original human request outranks additional planner assumptions. Existing behavior checks remain separate and mandatory. '
        '\nORIGINAL_REQUEST:\n'+original+'\nCRITERIA:\n'+json.dumps(criteria,ensure_ascii=False)
        +'\nACTUAL_SOURCE:\n'+json.dumps(verification_source_excerpts({name:source for name,source in sources.items()
            if not re.search(r'\.(?:test|spec)\.[cm]?js$',name)}),ensure_ascii=False)
        +('\nLAYOUT_SCOPE: For EACH supplied layout criterion, return exactly TWO check objects with that same single ID. '
            'Each check has exactly one viewport action. The mobile action must be exactly '
            '{"action":"viewport","selector":"page","value":{"width":375,"height":812}}. '
            'The desktop action must be exactly '
            '{"action":"viewport","selector":"page","value":{"width":1280,"height":800}}. '
            'Width is horizontal and height is vertical; never swap them. '
            'At EACH size include an overflow_x observation with operator=eq and expected=false, '
            'plus a visible observation with operator=eq and expected=true on a real usable input/button. '
            'If the criterion names a stylesheet, also observe its file_nonempty=true in each check. '
            'Do not put both viewport actions in one check: all actions run before observations. '
            'Do not click, fill, compare incidental CSS values or test selection behavior here; behavior is checked separately.'
            if layout_browser_criteria(criteria) else '')
        +('\nSCENARIO_SCOPE: For EACH criterion with multiple independent UI states, return exactly one check per required scenario, '
            'with the same single ID and a scenario field set to empty_data, no_matching_items or storage_error. '
            'Keep setup/actions and observations specific to that one state. For empty_data, use the real empty dataset/state: '
            'observe either exact visible empty-state guidance with measure=text or count=0 on the actual recipe/plan result list. '
            'Use selectors and any expected message copied from ACTUAL_SOURCE; never use an unrelated heading or invented selector. '
            'For no_matching_items, exercise the real UI with conditions that leave no eligible item, then observe the actual no-match guidance '
            'with measure=text or count=0 on the actual result list. For storage_error, put the exact storage_failure action first, '
            'perform the real UI save interaction, and observe the actual visible error guidance with measure=text. '
            'Do not add storage_failure as a top-level field. Never combine scenarios or use one scenario as evidence for another.'
            if multi_scenario_browser_criteria(criteria) else '')
        +('\nNETWORK_SCOPE: actions MUST be empty. Observe local_resources=true, external_request_count=0 and dependency_count=0. '
            'These owned measurements resolve script src and stylesheet href against the actual document URL, record attempted external requests, and count declared package dependencies. '
            'Bare filenames such as app.js and style.css are local. Do not write URL tests or JavaScript.'
            if network_browser_criteria(criteria) else '')
        +'\n'+'\n'.join(_browser_recovery_guidance(original,criteria)))
