#!/usr/bin/env python3
"""Opt-in real-model coding audit, always in a separate temporary profile.

This invokes the chat approval controller, not the OLCR GUI. It never edits a
user project, changes model settings, downloads models, commits or pushes.
Native inference can take several minutes; bounded task recovery still applies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import uuid


def drain_task(api, identifier: str, project: Path) -> None:
    """Follow ordinary requeues without issuing a human recovery or resetting limits."""
    previous = None
    while True:
        task = api.db.coding_task(identifier)
        if task['status'] not in {'QUEUED', 'RUNNING'}:
            return
        if task.get('pause_requested'):
            api._pause_at_checkpoint(identifier)
            return
        checkpoint = tuple(task.get(key) for key in (
            'status', 'activity', 'current_phase_id', 'plan_revision', 'retry_count',
            'replan_count', 'recovery_epoch', 'task_total_recovery_model_calls'))
        checkpoint += (len(api.db.coding_phase_reports(identifier)),)
        if checkpoint == previous:
            raise RuntimeError('Native audit runner returned without advancing its queued checkpoint')
        previous = checkpoint
        print('AUDIT_RESULT', api.run_managed_task(identifier, str(project)), flush=True)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,type=Path,help='Fresh absolute directory under the OS temporary directory or /private/tmp')
    parser.add_argument('--resume',action='store_true',help='Resume only a profile created by this audit')
    parser.add_argument('--request-file',type=Path,help='UTF-8 coding request; any task can use the same isolated audit')
    args=parser.parse_args()
    if not args.profile.is_absolute():parser.error('--profile must be absolute')
    profile=args.profile.resolve()
    temporary_roots={Path(tempfile.gettempdir()).resolve(),Path('/tmp').resolve()}
    if profile in temporary_roots or not any(profile.is_relative_to(root) for root in temporary_roots):
        parser.error('--profile must be inside a temporary directory')
    repository=Path(__file__).resolve().parents[1]
    request_file=(args.request_file if args.request_file else
                  profile/'request.txt' if args.resume and (profile/'request.txt').is_file() else
                  repository/'backend/tests/fixtures/native-lunch-request.txt')
    goal=request_file.read_text(encoding='utf8').rstrip('\n')
    if not goal.strip():parser.error('The coding request must not be empty')
    digest=hashlib.sha256(goal.encode()).hexdigest()
    marker=profile/'native-coding-audit.json'
    project=profile/'project'
    if args.resume:
        try:
            metadata=json.loads(marker.read_text())
            if metadata['goal_sha256']!=digest or metadata['workspace']!=str(project):raise ValueError('identity mismatch')
            identifier=metadata['task_id']
            connection=sqlite3.connect((profile/'olcr.db').as_uri()+'?mode=ro',uri=True)
            row=connection.execute('SELECT t.original_goal,p.workspace_path FROM coding_tasks t '
                'JOIN conversations c ON c.id=t.conversation_id JOIN projects p ON p.id=c.project_id WHERE t.id=?',(identifier,)).fetchone()
            connection.close()
            if row!=(goal,str(project)):raise ValueError('saved task identity mismatch')
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
            parser.error('Cannot resume this audit profile: '+str(exc))
    else:
        if profile.exists():parser.error('Use a fresh profile; existing profiles are never overwritten')
        project.mkdir(parents=True)
        (profile/'request.txt').write_text(goal,encoding='utf8')
        identifier='native-audit-'+uuid.uuid4().hex
        marker.write_text(json.dumps({'task_id':identifier,'workspace':str(project),'goal_sha256':digest},indent=2))
    os.environ['OLCR_APP_SUPPORT']=str(profile)
    os.environ['OLCR_DB_PATH']=str(profile/'olcr.db')
    os.environ['OLCR_ALLOWED_ROOTS']=str(project)
    os.environ['HF_HUB_OFFLINE']='1'
    os.chdir(repository)
    sys.path.insert(0,str(repository/'backend'))
    import olcr_api.app as api
    api.stop_coding_scheduler()
    if Path(api.db.path).resolve()!=profile/'olcr.db':
        raise RuntimeError('Audit database identity mismatch')
    try:
        if args.resume:
            saved=api.db.coding_task(identifier)
            epoch=saved['recovery_epoch']
            result=api._resume_coding_task_control(saved,source='EXPLICIT_NATIVE_AUDIT_RESUME')
            print('AUDIT_RESUME',json.dumps(result,ensure_ascii=False),flush=True)
            print('AUDIT_EPOCH',epoch,api.db.coding_task(identifier)['recovery_epoch'],flush=True)
        else:
            api.db.create_project('Native coding audit',str(project),time.time(),'audit-project')
            api.db.create_conversation('Native coding audit',time.time(),'audit-conversation','audit-project')
            api.db.create_coding_task(identifier,'audit-conversation',goal,'PLANNING','QWEN_PLANNING',None,time.time())
            api.db.update_coding_task(identifier,requirements=api.canonical_coding_requirements(goal))
            api._run_coding_planning(api.db.coding_task(identifier))
            task=api.db.coding_task(identifier)
            if task['status']!='WAITING_FOR_PLAN_APPROVAL':
                raise RuntimeError('Planning did not reach the explicit approval gate: '+task['status'])
            if list(project.iterdir()):raise RuntimeError('Planning created files before approval')
            print('AUDIT_APPROVAL_GATE=PASS SOURCE_FILES_BEFORE_APPROVAL=0',flush=True)
            api._managed_coding_chat(api.ChatInput(conversation_id='audit-conversation',message='承認します',
                coding_task_id=identifier,coding_followup='queue'),'audit-conversation','audit-approval')
            api.stop_coding_scheduler()
        drain_task(api,identifier,project)
    finally:
        api.stop_coding_scheduler()
        task=api.db.coding_task(identifier) or {}
        reports=api.db.coding_phase_reports(identifier)
        result={key:task.get(key) for key in ('status','activity','current_phase_id','plan_revision','recovery_epoch','recovery_reason','final_report_status')}
        result.update(task_id=identifier,workspace=str(project),phase_reports=len(reports),
            report_path=str(profile/'audit-result.json'))
        (profile/'audit-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('AUDIT_FINAL',json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if task.get('status')=='COMPLETED' else 1


if __name__=='__main__':
    raise SystemExit(main())
