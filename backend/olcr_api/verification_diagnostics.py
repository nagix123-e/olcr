"""Observed project-test failures, without inferring application defects from exit codes."""
from __future__ import annotations

import ast
from collections import Counter
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_TEST_SOURCE_SUFFIXES={'.c','.cc','.cjs','.cpp','.cs','.dart','.ex','.exs','.go','.h','.hpp','.java',
    '.js','.jsx','.kt','.kts','.mjs','.php','.py','.rb','.rs','.scala','.swift','.ts','.tsx'}


def is_project_test_path(path: str) -> bool:
    normalized=str(path or '').replace('\\','/').rstrip('/')
    candidate=Path(normalized)
    name=candidate.name
    if candidate.suffix.lower() not in _TEST_SOURCE_SUFFIXES:
        return False
    if set(candidate.parts[:-1]) & {'test','tests','__tests__','spec','specs'}:
        return True
    return bool(re.search(r'^(?:test[_-].+|.+[_-]tests?|Test.+|.+(?:_test|_spec|Tests?))\.[^.]+$|\.(?:test|spec)\.[^.]+$',
        name,re.I))


def reviewed_project_test_paths(finding: dict, sources: dict[str, str]) -> list[str]:
    """Return only explicitly reviewed project-test files in the current source set."""
    if not isinstance(finding, dict) or finding.get('verdict') != 'CHECK_DEFECT':
        return []
    paths=finding.get('repair_paths')
    if not isinstance(paths,(list,tuple,set)):
        return []
    return sorted({path for path in paths if isinstance(path,str)
        and path in sources and is_project_test_path(path)})


def is_project_test_command(command: list[str] | tuple[str, ...] | None) -> bool:
    """Recognize common explicit project-test launchers across runtimes."""
    if not isinstance(command,(list,tuple)) or not command or not isinstance(command[0],str):
        return False
    binary=Path(command[0]).name.lower()
    arguments=[str(item).strip() for item in command[1:] if isinstance(item,(str,int,float))]
    lowered=[item.lower() for item in arguments]
    if binary in {'node','node.exe'}:
        return '--test' in lowered or any(is_project_test_path(item) for item in arguments)
    if re.fullmatch(r'(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+(?:\.\d+)*)?)(?:\.exe)?',binary):
        return (any(lowered[index:index+2]==['-m',module] for index in range(len(lowered)-1)
                for module in ('unittest','pytest'))
            or any(is_project_test_path(item) for item in arguments))
    if binary in {'go','cargo','dotnet','swift','mix'}:
        return bool(lowered and lowered[0] in {'test','tests'})
    if binary in {'pytest','py.test','phpunit','rspec','jest','vitest','mocha','ava','tap'}:
        return True
    if binary in {'npm','pnpm','yarn','bun','deno','make','rake','composer',
                  'npx','pnpx','corepack','poetry','pipenv','uv','tox','nox'}:
        if any(item in {'test','tests','check'} or item.startswith(('test:','test-')) for item in lowered):
            return True
        return any(item in {'jest','vitest','mocha','ava','tap','pytest','rspec'} for item in lowered)
    if binary in {'gradle','gradlew'}:
        return any(item == 'test' or item.endswith(':test') for item in lowered)
    if binary in {'mvn','mvnw'}:
        return any(item in {'test','verify'} for item in lowered)
    if binary in {'ruby'}:
        return (any(item in {'rspec','test','spec'} for item in lowered)
            or any(is_project_test_path(item) for item in arguments))
    return False


def is_project_test_setup_failure(failure: dict) -> bool:
    """Classify a setup failure only when all causal source frames are tests."""
    if not isinstance(failure, dict):
        return False
    locations = [str(item.get('path') or '') for item in failure.get('source_locations') or []
                 if isinstance(item, dict)]
    if not locations or any(not is_project_test_path(path) for path in locations):
        return False
    detail = str(failure.get('detail') or failure.get('error') or '')
    patterns = (
        r"(?:ReferenceError:\s*|error:\s*['\"])(?:describe|it|test|expect|beforeEach|afterEach|require|module|exports) is not defined",
        r"(?:TypeError:\s*|error:\s*['\"])(?:expect|describe|it|beforeEach|afterEach) is not a function",
        r"(?:node:test[^\n]*does not provide an export named ['\"]expect|moduleFn\([^\n]*is not a function)",
        r"(?:The ['\"]path['\"] argument must be of type string|ERR_INVALID_ARG_TYPE)[^\n]*(?:instance of Object|received an object)",
        r"(?:ReferenceError:\s*|error:\s*['\"])[A-Za-z_$][\w$]*\s+is not defined",
    )
    if any(re.search(pattern, detail, re.I) for pattern in patterns):
        return True
    # Module-evaluation errors are attributed to test setup only when the
    # complete causal stack stays inside test files. Any application frame
    # above prevents this classification, including application SyntaxErrors.
    return failure.get('name') == 'Node.js module loading'


def _node_yaml_scalar(raw: str):
    value=raw.strip()
    if not value:
        return None
    try:
        return ast.literal_eval(value)
    except (ValueError,SyntaxError):
        if len(value)>=2 and value[0]==value[-1]=="'":
            return value[1:-1].replace("''", "'")
        if value in {'true','false'}:
            return value=='true'
        if value in {'null','~'}:
            return None
        return value


def _node_assertion_value(detail: str, field: str):
    """Parse a bounded Node TAP expected/actual scalar or flat object list."""
    match=re.search(r'^[ \t]*'+re.escape(field)+r':[ \t]*(.*?)[ \t]*$',detail,re.M)
    if not match:
        return None
    inline=match.group(1)
    if inline:
        try:
            value=ast.literal_eval(inline)
        except (ValueError,SyntaxError):
            return None
        return value if isinstance(value,list) else None
    base_indent=len(match.group(0))-len(match.group(0).lstrip())
    lines=detail[match.end():].splitlines()
    values=[]
    current=None
    item_indent=None
    found=False
    for line in lines:
        if not line.strip():
            continue
        indent=len(line)-len(line.lstrip())
        if indent<=base_indent:
            break
        item=re.match(r'^\s+(\d+):\s*(.*?)\s*$',line)
        if item and (item_indent is None or indent==item_indent):
            if found:
                values.append(current)
            item_indent=indent
            found=True
            current={} if not item.group(2) else _node_yaml_scalar(item.group(2))
            continue
        if isinstance(current,dict) and item_indent is not None and indent>item_indent:
            prop=re.match(r'^\s+([A-Za-z_][\w-]*):\s*(.*?)\s*$',line)
            if not prop:
                return None
            current[prop.group(1)]=_node_yaml_scalar(prop.group(2))
            continue
        return None
    if found:
        values.append(current)
    return values if found else None


def _node_assertion_fields(detail: str) -> dict:
    operator=re.search(r'^\s*operator:\s*[\'"]?([\w]+)',detail,re.M)
    result={}
    if operator:
        result['assertion_operator']=operator.group(1)
    for field in ('expected','actual'):
        value=_node_assertion_value(detail,field)
        if value is not None:
            result[field]=value
    return result


def node_test_run(command: list[str], output: str, returncode: int, root: Path) -> dict:
    """Return individual failing TAP leaves, not duplicated parent-suite summaries."""
    def count(name):
        # Node 22+ defaults `node --test` to the human-readable spec reporter
        # in non-TTY runs (`ℹ tests 1`) instead of TAP (`# tests 1`). Treating
        # that valid summary as zero registered tests incorrectly labels an
        # assertion failure as a module-load/setup failure.
        match=re.search(r'^\s*(?:#|ℹ)\s*'+re.escape(name)+r'\s+(\d+)\s*$',output,re.M)
        return int(match.group(1)) if match else 0
    lines=output.splitlines()
    points=[]
    point_pattern=re.compile(r'^(?P<indent>\s*)not ok \d+ - (?P<name>.+?)\s*$')
    for index,line in enumerate(lines):
        match=point_pattern.match(line)
        if match:
            points.append((index,len(match.group('indent')),match.group('name')))

    def relative_location(raw_path, line_number, column_number):
        raw_path=raw_path.strip().strip("'\"`)")
        if raw_path.startswith('file://'):
            raw_path=unquote(urlsplit(raw_path).path)
        else:
            raw_path=unquote(raw_path)
        path=Path(raw_path)
        if not path.is_absolute():
            path=root/path
        try:
            relative=path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError,ValueError):
            return None
        return {'path':relative,'line':int(line_number),'column':int(column_number or 1)}

    failures=[]
    containers=0
    for position,(index,indent,name) in enumerate(points):
        end=len(lines)
        for next_index,next_indent,_ in points[position+1:]:
            if next_indent<=indent:
                end=next_index
                break
        next_subtest=next((i for i in range(index+1,end)
            if (match:=re.match(r'^(?P<indent>\s*)# Subtest: ',lines[i]))
            and len(match.group('indent'))<=indent),None)
        if next_subtest is not None:
            end=next_subtest
        # The next file can print a stack before its Subtest line. The TAP
        # diagnostic ends at its YAML terminator; those frames are not ours.
        end=next((i+1 for i in range(index+1,end) if lines[i].strip()=='...'),end)
        detail='\n'.join(lines[index:end])[:2400]
        failure_type=re.search(r'^\s*failureType:\s*[\'\"]?([^\'\"\s]+)',detail,re.M)
        failure_type=failure_type.group(1) if failure_type else ''
        if failure_type=='subtestsFailed':
            continue
        locations=[]
        seen=set()
        for line in lines[index:end]:
            field=re.search(r'^\s*location:\s*(.*?)\s*$',line)
            raw_values=[field.group(1)] if field else []
            raw_values.extend(match.group(0) for match in re.finditer(
                r'((?:file:///|/)[^\n]*?):(\d+):(\d+)',line))
            for raw in raw_values:
                raw=raw.strip().strip("'\"`")
                match=(re.match(r'^(.*):(\d+):(\d+)$',raw)
                    or re.match(r'^(.*):(\d+)$',raw))
                if not match:
                    continue
                location=relative_location(match.group(1),match.group(2),match.group(3) if match.lastindex==3 else 1)
                if location:
                    key=(location['path'],location['line'],location['column'])
                    if key not in seen:
                        seen.add(key)
                        locations.append(location)
        # Node 20 emits a file's startup exception BEFORE its synthetic
        # file subtest. Only attach the adjacent comment block to its own
        # file container, never to a real assertion or another file.
        container=(relative_location(name,1,1) if failure_type=='testCodeFailure'
                   and index and lines[index-1].strip()=='# Subtest: '+name else None)
        if (container and container['path'] in {loc['path'] for loc in locations}
                and re.search(r"^\s*code:\s*['\"]?ERR_TEST_FAILURE\b",detail,re.M)
                and index and lines[index-1].strip() == '# Subtest: '+name):
            containers+=1
            start=index-1
            while start and lines[start-1].startswith('# ') and not lines[start-1].startswith('# Subtest:'):
                start-=1
            block=lines[start:index-1]
            error=next((i for i,line in enumerate(block) if re.match(
                r'^# (?:[A-Za-z]*Error)(?: \[[^\]]+\])?:',line)),None)
            if error is not None:
                frames=[]
                for line in block:
                    match=re.search(r'(?:^# |\(|^#\s+at )((?:file:///|/).*?):(\d+)(?::(\d+))?\)?$',line)
                    if match:
                        loc=relative_location(*match.groups())
                        if loc and loc not in frames:
                            frames.append(loc)
                detail='\n'.join(block[error:error+10])[:2000]
                if frames:
                    locations=frames
                    name='Node.js module loading'
        failure={'name':name,'detail':detail,'failure_type':failure_type,
                 'source_locations':locations[:8]}
        failure.update(_node_assertion_fields(detail))
        failures.append(failure)
        if len(failures)>=32:
            break

    # Some older/custom reporters omit TAP points. Retain their first causal
    # error as a single case rather than losing the failed command entirely.
    if not failures:
        for index,line in enumerate(lines):
            if not re.search(r'(?:[A-Za-z]*Error(?:\s*\[[^]]+\])?:|^\s*error:)',line):
                continue
            detail='\n'.join(lines[max(0,index-4):min(len(lines),index+80)])[:4000]
            locations=[]
            for match in re.finditer(r'^\s*test at\s+(.+?):(\d+):(\d+)\s*$',detail,re.M):
                location=relative_location(match.group(1),match.group(2),match.group(3))
                if location and location not in locations:
                    locations.append(location)
            for match in re.finditer(r'((?:file:///|/)[^\n]*?):(\d+)(?::(\d+))?',detail):
                location=relative_location(match.group(1),match.group(2),match.group(3) or 1)
                if location and location not in locations:
                    locations.append(location)
            failure={'name':'Node.js module loading' if count('tests')==0 else 'Node test',
                     'detail':detail,'source_locations':locations[:8]}
            failure.update(_node_assertion_fields(detail))
            failures.append(failure)
            break
    return {'command':command,'tests':count('tests'),'registered_tests':max(0,count('tests')-containers),
            'passed':count('pass'),'executed_tests':max(0,count('pass')-containers),
            'skipped':count('skipped'),'todo':count('todo'),
            'failed':count('fail'),'returncode':returncode,'failures':failures}


def project_test_review_cases(observed: dict) -> list[dict]:
    """Flatten structured runner failures without merging separate test outcomes."""
    cases=[]
    for run_index,run in enumerate(observed.get('test_runs') or []):
        if not isinstance(run,dict):
            continue
        failures=run.get('failures') or []
        for failure_index,failure in enumerate(failures):
            if not isinstance(failure,dict):
                continue
            cases.append({**failure,'run_index':run_index,'failure_index':failure_index,
                          'run_command':run.get('command')})
        if failures or run.get('passed') is True or run.get('returncode')==0:
            continue
        detail=run.get('output')
        if detail:
            cases.append({'name':'Unparsed project-test failure','detail':str(detail)[-4000:],
                          'source_locations':[],'run_index':run_index,'failure_index':0,
                          'run_command':run.get('command')})
    if cases:
        return cases
    detail=observed.get('output')
    if detail:
        return [{'name':'Unparsed project-test failure','detail':str(detail)[-4000:],
                 'source_locations':[],'run_index':0,'failure_index':0,
                 'run_command':None}]
    return []


def project_test_failure_diagnosis(observed: dict, sources: dict[str,str], *,
                                   requirements: list[str] | None=None,
                                   file_manifest: list[dict] | None=None) -> dict:
    tests={name:source for name,source in sources.items() if is_project_test_path(name)}
    failures=[failure for run in observed.get('test_runs') or [] for failure in run.get('failures') or []]
    setup_failures=[]
    # Node's strict deep-equality assertion also compares array order. A
    # shopping-list requirement that asks only for the meal ingredients does
    # not define ordering, so a newly-created test that fails solely because
    # those same unique strings appear in another order is over-specified.
    # Keep this narrow: it needs one parsed assertion, one writable test file,
    # exact permutation equality, and a requirement with no ordering rule.
    if len(failures)==1 and requirements:
        failure=failures[0]
        expected,actual=failure.get('expected'),failure.get('actual')
        locations=[item.get('path') for item in failure.get('source_locations') or []
                   if isinstance(item,dict)]
        manifests={str(item.get('path')):str(item.get('action') or '').lower()
                   for item in (file_manifest or []) if isinstance(item,dict)}
        test_paths={path for path in locations if isinstance(path,str) and path in tests}
        requirement_text=' '.join(str(item) for item in requirements)
        order_specified=bool(re.search(r'順序|順番|指定順|並び|並べ替え|昇順|降順|alphabetic|sorted|order(?:ed|ing)?',
                                      requirement_text,re.I))
        shopping_requirement=bool(re.search(r'買い物リスト|shopping list',requirement_text,re.I)
            and re.search(r'食材|材料|ingredient|material',requirement_text,re.I))
        test_path=next(iter(test_paths),None) if len(test_paths)==1 else None
        created_or_modified=bool(test_path and manifests.get(test_path) in {'create','modify'})
        test_source=tests.get(test_path,'') if test_path else ''
        def order_free_shopping_rows(rows):
            if not isinstance(rows,list) or not rows:
                return None
            if all(isinstance(item,str) and item for item in rows):
                return Counter(rows) if len(rows)==len(set(rows)) else None
            if all(isinstance(item,dict) and set(item)=={'name','count'}
                   and isinstance(item.get('name'),str) and item['name'].strip()
                   and isinstance(item.get('count'),(int,float)) and not isinstance(item.get('count'),bool)
                   and math.isfinite(item['count']) and item['count']>=0
                   for item in rows):
                names=[item['name'] for item in rows]
                if len(names)!=len(set(names)):
                    return None
                return Counter((item['name'],float(item['count'])) for item in rows)
            if all(isinstance(item,dict) and set(item)=={'name','quantity','unit'}
                   and isinstance(item.get('name'),str) and item['name'].strip()
                   and isinstance(item.get('quantity'),(int,float)) and not isinstance(item.get('quantity'),bool)
                   and item['quantity']>=0 and isinstance(item.get('unit'),str)
                   for item in rows):
                names=[item['name'] for item in rows]
                if len(names)!=len(set(names)):
                    return None
                return Counter(json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                               for item in rows)
            return None
        expected_rows=order_free_shopping_rows(expected)
        actual_rows=order_free_shopping_rows(actual)
        if (failure.get('assertion_operator') in {'deepStrictEqual','deepEqual'}
                and expected_rows is not None and actual_rows is not None
                and expected!=actual and expected_rows==actual_rows
                and not order_specified and shopping_requirement and created_or_modified
                and re.search(r'assert\s*\.\s*deep(?:Strict)?Equal\s*\(',test_source)):
            return {'verdict':'CHECK_DEFECT',
                'reason':'The generated test requires an order that the shopping-list requirement does not specify; actual and expected contain the same unique ingredient rows and counts.',
                'repair_paths':[test_path],'test_fragment':test_source.strip(),
                'failure_case':failure,'evidence_origin':'UNREQUESTED_ARRAY_ORDER_ASSERTION',
                'project_test_setup_repair':False}
    for failure in failures:
        locations=[str(item.get('path') or '') for item in failure.get('source_locations') or []]
        if (not locations or locations[0] not in tests
                or any(path not in tests for path in locations)
                or not is_project_test_setup_failure(failure)):
            # A single harness-shaped failure must never hide application
            # assertion failures from the same run. Test-only repair is safe
            # only when every observed failure is attributable to the test.
            return {'verdict':'INCONCLUSIVE','reason':'The current run contains failures that are not all attributable to project-test setup.',
                    'repair_paths':[],'evidence_origin':'CURRENT_PROJECT_TEST_EXECUTION'}
        setup_failures.append((failure,locations[0]))
    if setup_failures:
        paths=sorted({path for _,path in setup_failures})
        return {'verdict':'CHECK_DEFECT','reason':'Every current failure occurred inside project-test setup before application assertions could establish behavior.',
                'repair_paths':paths,'failure_case':setup_failures[0][0],
                'failure_cases':[failure for failure,_ in setup_failures],
                'evidence_origin':'CURRENT_PROJECT_TEST_EXECUTION','project_test_setup_repair':True}
    return {'verdict':'INCONCLUSIVE','reason':'A failing project test alone does not identify whether its assertion or the application is wrong.',
            'repair_paths':[],'evidence_origin':'CURRENT_PROJECT_TEST_EXECUTION'}


def project_test_runtime_source_diagnosis(observed: dict, sources: dict[str,str],
                                          file_manifest: list[dict] | None) -> dict:
    """Authorize a source-only retry for one unambiguous app runtime failure.

    A failing suite can report several downstream test failures for one thrown
    application error. Keep the repair narrow by requiring every app-side
    runtime frame to point to the same manifested source line and every other
    failure to stay inside a task-created test. Assertion-only and ambiguous
    runs still require independent ownership review.
    """
    runs=[run for run in (observed.get('test_runs') or []) if isinstance(run,dict)]
    failures=[failure for run in runs for failure in (run.get('failures') or [])
              if isinstance(failure,dict)]
    if not failures:
        return {'verdict':'INCONCLUSIVE','reason':'Runtime source recovery requires at least one attributable project-test failure.'}
    if any(run.get('returncode') not in (None,0) and not (run.get('failures') or []) for run in runs):
        return {'verdict':'INCONCLUSIVE','reason':'A failed test command has no complete failure record.'}

    entries={str(item.get('path')):str(item.get('action') or '').lower()
             for item in (file_manifest or []) if isinstance(item,dict) and isinstance(item.get('path'),str)}
    task_created_tests={path for path,action in entries.items()
                        if action=='create' and is_project_test_path(path) and path in sources}
    repairable_sources={path for path,action in entries.items()
                        if action in {'create','modify'} and not is_project_test_path(path) and path in sources}
    supported={'TypeError','ReferenceError','RangeError','URIError','EvalError','AggregateError'}
    roots=[]
    for failure in failures:
        if failure.get('failure_type')!='testCodeFailure' or failure.get('name')=='Node.js module loading':
            return {'verdict':'INCONCLUSIVE','reason':'The run contains a failure outside an executed task-created Node test case.'}
        locations=[item for item in (failure.get('source_locations') or []) if isinstance(item,dict)]
        test_locations=[item for item in locations
                        if isinstance(item.get('path'),str) and is_project_test_path(item['path'])]
        if (not test_locations or any(item['path'] not in task_created_tests for item in test_locations)
                or any(not isinstance(item.get('path'),str) for item in locations)):
            return {'verdict':'INCONCLUSIVE','reason':'At least one failure is not confined to a test created by the current coding task.'}

        detail=str(failure.get('detail') or failure.get('error') or '')
        if re.search(r'\b(?:AssertionError|ERR_ASSERTION)\b',detail):
            if any(not is_project_test_path(item['path']) for item in locations):
                return {'verdict':'INCONCLUSIVE','reason':'An assertion failure also has an application-source frame.'}
            continue
        exception_name=(failure.get('exception_name') or '')
        match=re.search(r"^\s*name:\s*['\"]?(TypeError|ReferenceError|RangeError|URIError|EvalError|AggregateError)['\"]?\s*$",
                        detail,re.M)
        if not match:
            match=re.search(r'\b(TypeError|ReferenceError|RangeError|URIError|EvalError|AggregateError):',detail)
        if not match and exception_name not in supported:
            # Test-side failures may be downstream symptoms, but they cannot
            # themselves supply evidence for a source repair.
            if any(not is_project_test_path(item['path']) for item in locations):
                return {'verdict':'INCONCLUSIVE','reason':'A non-runtime failure crosses into application code.'}
            continue
        exception_name=match.group(1) if match else exception_name

        source_locations=[item for item in locations
                          if not is_project_test_path(item['path'])]
        if not source_locations:
            continue
        source_paths={item['path'] for item in source_locations}
        if len(source_paths)!=1:
            return {'verdict':'INCONCLUSIVE','reason':'A runtime stack crosses multiple application files.'}
        source_path=source_locations[0]['path']
        if source_path not in repairable_sources:
            return {'verdict':'INCONCLUSIVE','reason':'The throwing application file is outside this task’s writable manifest.'}
        line=source_locations[0].get('line')
        source_lines=sources[source_path].splitlines()
        if not isinstance(line,int) or line<1 or line>len(source_lines) or not source_lines[line-1].strip():
            return {'verdict':'INCONCLUSIVE','reason':'The runtime stack does not identify an exact source line.'}
        roots.append((failure,exception_name,source_path,line,source_lines[line-1].strip()))

    if not roots:
        return {'verdict':'INCONCLUSIVE','reason':'No uncaught runtime exception points to an exact manifested application line.'}
    root_signatures={(exception_name,source_path,line)
                     for _,exception_name,source_path,line,_ in roots}
    if len(root_signatures)!=1:
        return {'verdict':'INCONCLUSIVE','reason':'The current run contains distinct application runtime roots.'}
    failure,exception_name,source_path,line,source_fragment=roots[0]
    return {'verdict':'SOURCE_DEFECT',
        'reason':f'{len(roots)} task-created Node test failure(s) share the same uncaught {exception_name} at the exact application frame {source_path}:{line}; other failures in the run are confined to task-created tests. Retry writes are restricted to that manifested application file; test files are excluded.',
        'requirement':'Task-created executable behavior must complete without an uncaught application runtime exception.',
        'source_fragment':source_fragment,'test_fragment':'',
        'repair_paths':[source_path],'source_location':{'path':source_path,'line':line},
        'failure_case':failure,'failure_cases':failures,'runtime_failure_count':len(roots),
        'other_failure_count':len(failures)-len(roots),
        'evidence_origin':'CURRENT_TASK_CREATED_NODE_TEST_RUNTIME_EXCEPTION_CLUSTER'}
