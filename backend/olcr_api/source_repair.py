"""Compile model-proposed line edits against an immutable source snapshot.

This transport is used after an ineffective repair or a rejected patch. The
diagnosing model supplies the actual edit, so another model cannot silently
discard its correction while re-encoding a search/replacement pair.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


def html_boundary_errors(source: str) -> list[dict]:
    """Find self-closing HTML text containers that consume following markup."""
    class BoundaryParser(HTMLParser):
        def __init__(self):
            super().__init__();self.foreign=[];self.invalid=[]
        def handle_starttag(self,tag,attrs):
            if tag in {'svg','math'}:self.foreign.append(tag)
        def handle_endtag(self,tag):
            if self.foreign and tag==self.foreign[-1]:self.foreign.pop()
        def handle_startendtag(self,tag,attrs):
            if not self.foreign and tag in {'textarea','script','style','title','iframe','xmp','noembed','noframes'}:
                self.invalid.append({'line':self.getpos()[0],'tag':tag,'source_fragment':self.get_starttag_text()})
    parser=BoundaryParser();parser.feed(source);parser.close()
    return parser.invalid


def repair_schema(sources: dict[str, str]) -> dict:
    variants = []
    for path, source in sources.items():
        count = max(1, len(source.splitlines(keepends=True)))
        location={
            "path": {"const": path},
            "start_line": {"type": "integer", "minimum": 1, "maximum": count},
            "end_line": {"type": "integer", "minimum": 1, "maximum": count},
        }
        for action in ('delete','replace'):
            properties={"action":{"const":action},**location}
            if action=='replace':properties['replacement']={"type":"string","minLength":1,"maxLength":16000}
            variants.append({"type":"object","properties":properties,
                "required":list(properties),"additionalProperties":False})
    return {"type": "object", "properties": {
        "edits": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"anyOf": variants}},
        "reason": {"type": "string", "maxLength": 240},
    }, "required": ["reason", "edits"], "additionalProperties": False}


def numbered_sources(sources: dict[str, str]) -> str:
    return "\n\n".join(
        f"FILE {path}\n" + "".join(f"{index}: {line}" for index, line in
                                     enumerate(source.splitlines(keepends=True), 1))
        for path, source in sources.items())


def observe_test_locals(root: Path, sources: dict[str, str], failures: list[dict]) -> list[dict]:
    """Capture bounded values in an isolated source copy before an assertion.

    The bundled Node can lack Inspector support. A host-owned observation
    statement is inserted into a temporary copy; the actual worktree and the
    original assertion remain untouched. These values are diagnosis, not PASS.
    """
    from .coding_verification import run_acceptance
    from .node_mcp_runtime import runtime_root
    import tempfile
    resources=runtime_root()
    if resources is None:
        return []
    for failure in failures:
        frames=[frame for frame in failure.get('source_locations') or []
                if frame.get('path') in sources and re.search(r'\.(?:test|spec)\.[cm]?js$',str(frame.get('path')))]
        if not frames:
            continue
        frame=frames[-1];relative=frame['path'];target=(root/relative).resolve()
        lines=sources[relative].splitlines(keepends=True);number=frame.get('line')
        if not target.is_relative_to(root) or type(number) is not int or not 1<=number<=len(lines):
            continue
        # Do not insert a statement into the middle of a multiline expression.
        if not re.match(r'^\s*(?:assert(?:\.\w+)*|expect)\s*\(',lines[number-1]):continue
        names=list(dict.fromkeys(re.findall(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)',''.join(lines[:number]))))
        names=[name for name in names if not re.search(r'token|secret|password|credential|authorization',name,re.I)][-30:]
        if not names:continue
        reads=''.join('try{const item=describe('+name+');if(item!==undefined)values['+json.dumps(name)+']=item;}catch{}' for name in names)
        capture=r'''
;(()=>{
 const describe=(value,depth=0)=>{
  if(value===null||typeof value==='boolean'||typeof value==='number')return value;
  if(typeof value==='string')return value.slice(0,240);
  if(typeof value==='undefined')return {type:'undefined'};
  if(Array.isArray(value)&&depth<2){
   const properties=Object.getOwnPropertyDescriptors(value),entries={};
   for(const key of Object.keys(properties).filter(k=>k==='length'||/^\d+$/.test(k)).slice(0,21)){
    if(Object.hasOwn(properties[key],'value'))entries[key]=describe(properties[key].value,depth+1);
   }
   return {type:'array',entries};
  }
 };
 const values={};
'''+reads+'\nconsole.log("OLCR_TEST_STATE="+JSON.stringify({locals:values}));\n})();\n'
        with tempfile.TemporaryDirectory(prefix='olcr-test-probe-') as directory:
            copied_root=Path(directory).resolve()
            for name,source in sources.items():
                copied=(copied_root/name).resolve()
                if not copied.is_relative_to(copied_root):return []
                copied.parent.mkdir(parents=True,exist_ok=True);copied.write_text(source,encoding='utf8')
            copied_target=copied_root/relative
            lines.insert(number-1,capture);copied_target.write_text(''.join(lines),encoding='utf8')
            try:
                result=run_acceptance([str(resources/'node/bin/node'),'--test',str(copied_target)],str(copied_root),20)
            except (OSError,subprocess.TimeoutExpired):
                return []
        for line in result.stdout.splitlines():
            if 'OLCR_TEST_STATE=' in line:
                try:
                    value=json.loads(line.split('OLCR_TEST_STATE=',1)[1])
                    if len(json.dumps(value))<=16000:
                        return [{'path':relative,'line':number,'probe_origin':'ISOLATED_SOURCE_COPY',**value}]
                except ValueError:
                    continue
        return []
    return []


def native_registration_repair(failure: dict, sources: dict[str,str]) -> dict | None:
    """Propose only imports proven missing by both Node and lexical scope."""
    from .node_mcp_runtime import runtime_root
    from .verification_diagnostics import is_project_test_setup_failure
    if not is_project_test_setup_failure(failure):
        return None
    match=re.search(r"(?:\bReferenceError:\s*|\berror:\s*['\"])(describe|it|test|before|after|beforeEach|afterEach|expect) is not defined\b",
                    failure.get('detail') or '')
    if not match:
        return None
    frame=failure['source_locations'][0]
    path=frame.get('path','')
    source=sources.get(path)
    if not source or Path(path).suffix not in {'.js','.cjs','.mjs'}:
        return None
    packaged=runtime_root()
    if not packaged:
        return None
    try:
        result=subprocess.run([str(packaged/'node/bin/node'),str(Path(__file__).with_name('node_test_imports.cjs')),
                               str(packaged)],input=json.dumps({'source':source,'missing':match[1],
            'errorLines':[f.get('line') for f in failure['source_locations'] if f.get('path')==path],'esm':path.endswith('.mjs') or 'file://' in failure['detail']}),
            capture_output=True,text=True,timeout=5)
        if result.returncode or not result.stdout:
            return None
        updated=json.loads(result.stdout)
        old=source.splitlines(keepends=True); new=updated.splitlines(keepends=True)
        start=0
        while start<min(len(old),len(new)) and old[start]==new[start]:
            start+=1
        tail=0
        while tail<min(len(old),len(new))-start and old[-1-tail]==new[-1-tail]:
            tail+=1
        # Include an anchor for a pure insertion. Use the exact live snapshot
        # and the same restricted line-edit validator as a model proposal.
        if len(old)-tail==start:
            if tail:tail-=1
            elif start:start-=1
        replacement=''.join(new[start:len(new)-tail])
        if start>=len(old) or len(replacement)>16000:
            return None
        return {'reason':'Bind native Node test APIs and preserve executable expectations',
                'edits':[{'path':path,'start_line':start+1,'end_line':len(old)-tail,'replacement':replacement}]}
    except (OSError,subprocess.TimeoutExpired,ValueError,KeyError):
        return None


def compile_repair(value: dict, sources: dict[str, str]) -> tuple[dict, list[dict]]:
    if not isinstance(value, dict) or set(value) != {"reason", "edits"}:
        raise ValueError("source repair requires reason and edits")
    edits = value["edits"]
    if not isinstance(value["reason"], str) or not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise ValueError("invalid source repair proposal")
    grouped: dict[str, list[dict]] = {}
    evidence = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("invalid source repair fields")
        action=edit.get('action')
        if action is not None:
            expected={'path','start_line','end_line','action'}|({'replacement'}if action=='replace'else set())
            if action not in {'delete','replace'} or set(edit)!=expected:
                raise ValueError('invalid source repair action/fields')
            edit={key:value for key,value in edit.items()if key!='action'}
            if action=='delete':edit['replacement']=''
        if set(edit)!={'path','start_line','end_line','replacement'}:
            raise ValueError('invalid source repair fields')
        path, start, end, replacement = (edit[key] for key in ("path", "start_line", "end_line", "replacement"))
        if not isinstance(path, str) or path not in sources:
            raise ValueError("source repair target outside supplied writable snapshot")
        lines = sources[path].splitlines(keepends=True) or [""]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError("source repair line range is invalid")
        if not isinstance(replacement, str) or len(replacement) > 16000:
            raise ValueError("invalid source repair replacement")
        if any(start <= previous["end_line"] and previous["start_line"] <= end for previous in grouped.get(path, [])):
            raise ValueError("source repair ranges overlap")
        # Replacements describe complete lines. Preserve the boundary to the
        # next untouched line if the model omitted its final line terminator.
        if replacement and lines[end - 1].endswith("\n") and not replacement.endswith("\n"):
            replacement += "\r\n" if lines[end - 1].endswith("\r\n") else "\n"
        if "".join(lines[start - 1:end]) == replacement:
            raise ValueError("source repair does not change the selected source")
        grouped.setdefault(path, []).append({**edit, "replacement": replacement})
        evidence.append({"path": path, "start_line": start, "end_line": end,
                         "snapshot_sha256": hashlib.sha256(sources[path].encode()).hexdigest()})
    operations = []
    for path, changes in grouped.items():
        lines = sources[path].splitlines(keepends=True) or [""]
        for edit in sorted(changes, key=lambda e: e["start_line"], reverse=True):
            lines[edit["start_line"] - 1:edit["end_line"]] = [edit["replacement"]]
        content = "".join(lines)
        if content == sources[path]:
            raise ValueError("source repair has no net change")
        # The ordinary executor still checks the live source hash, scope,
        # structure, package ownership, rollback and actual verification.
        operations.append({"op": "write", "path": path, "content": content})
    return {"change_required": True, "source_inspected": True, "condition_evaluated": True,
            "reason_code": value["reason"][:160], "operations": operations,
            "verification": "host must execute the approved verifier"}, evidence
