"""Evidence-based progress at a stop; never ask a model to guess completion."""
from __future__ import annotations

import hashlib
import json
import re


def _verification_details(reports: list[dict]) -> tuple[list[str], list[str]]:
    summaries,failures=[],[]
    for report in reports:
        commands=[operation for operation in (report.get('typed_execution_summary') or {}).get('operations') or []
                  if operation.get('tool')=='verification_command']
        if not commands:continue
        output=commands[-1].get('output') or {}
        for run in output.get('test_runs') or []:
            count=run.get('registered_tests',run.get('tests'))
            passed,failed=run.get('passed'),run.get('failed')
            if all(isinstance(value,int) for value in (count,passed,failed)):
                summaries.append(f'- Nodeテスト：登録{count}件、成功{passed}件、失敗{failed}件'
                                 + ('（起動・読み込みの失敗を含む）' if count==0 and failed else ''))
            for failure in (run.get('failures') or [])[:3]:
                name=str(failure.get('name') or 'Nodeテスト')[:240]
                detail=str(failure.get('detail') or '')
                location=next(iter(failure.get('source_locations') or []),{})
                where=(f"（{location.get('path')}:{location.get('line')}）" if location.get('path') else '')
                match=re.search(r'\berror:\s*(.*)',detail,re.S)
                detail=(match.group(1) if match else detail)[:1200].strip()
                failures.append('- 失敗したテスト：'+name+where+'\n'+
                                '\n'.join('    '+line for line in detail.splitlines()))
        for failure in output.get('failures') or []:
            error=str(failure.get('error') or '')
            if failure.get('ids')==['REQUIRED_TESTS'] or (failures and 'node:test failed' in error):continue
            assertion=failure.get('assertion') or {}
            label='検証コードのエラー' if failure.get('contract_error') else '失敗した確認項目'
            failures.append('- '+label+'：'+', '.join(map(str,failure.get('ids') or []))+'\n    '+error.split('\n    at ',1)[0][:800]
                            + ('\n    '+json.dumps(assertion,ensure_ascii=False)[:800] if assertion else ''))
    return list(dict.fromkeys(summaries)),list(dict.fromkeys(failures))


def stop_progress(task: dict, rows: list[dict], reason: str, intended: bool) -> dict:
    phases = (task.get('plan') or {}).get('phases') or []
    reports = {}
    historical = {}
    for row in rows:
        report = row.get('structured_report') or {}
        revision = report.get('plan_revision')
        phase_id = row.get('phase_id') or report.get('phase_id')
        if revision is None or revision <= task.get('plan_revision', 0):
            historical[phase_id] = report
        if revision is not None and revision != task.get('plan_revision', 0):
            continue
        reports[phase_id] = report
    for phase in phases:
        phase_id = phase.get('id')
        inherited = historical.get(phase_id) or {}
        # A replan explicitly retains unchanged completed phases as PASS.
        # Carry their successful evidence, without hiding a current failure.
        if (phase.get('status') == 'pass' and phase_id not in reports
                and inherited.get('status') == 'PASS'
                and (inherited.get('manager_decision') or {}).get('decision') == 'PASS'):
            reports[phase_id] = inherited
    completed = []
    for phase in phases:
        evidence = reports.get(phase.get('id')) or {}
        if phase.get('status') == 'pass' and evidence.get('status') == 'PASS' and (evidence.get('manager_decision') or {}).get('decision') == 'PASS':
            completed.append(phase)
    done_ids = {p['id'] for p in completed}
    remaining = [p for p in phases if p.get('id') not in done_ids]
    def values(key):
        return list(dict.fromkeys(str(value) for report in reports.values() for value in (report.get(key) or []) if value))
    # The latest report owns current failures/completion. Earlier successful
    # mutations still belong to this Task even when a later attempt edits a
    # different file or only runs verification.
    changed = list(dict.fromkeys(str(path) for row in rows
        for report in [row.get('structured_report') or {}]
        if (report.get('plan_revision') is None or report['plan_revision']<=task.get('plan_revision',0))
        and (report.get('typed_execution_summary') or {}).get('worktree_state')!='ROLLED_BACK'
        for path in report.get('changed_files') or [] if path))
    checks = values('test_executed')
    failed = values('test_fail')
    summaries,typed_failures=_verification_details(list(reports.values()))
    if typed_failures:
        def duplicate_raw(value):
            if 'node:test failed' in value or 'TAP version 13' in value:return True
            try:
                decoded=json.loads(value)
                return isinstance(decoded,dict) and 'ids' in decoded and 'error' in decoded
            except (ValueError,TypeError):return False
        failed=[value for value in failed if not duplicate_raw(value)]
    errors = values('errors')
    labels = {'PAUSED':'ユーザーの一時停止要求', 'RESOURCE_CHECKPOINT':'指定された工程境界',
              'PLANNING':'実行可能な計画を確定できなかったため',
              'IMPLEMENTATION_PREPARED':'計画とファイル準備の工程境界', 'PHASE_FAILURE_CHECKPOINT':'指定された失敗時の停止',
              'REPLAN_INEFFECTIVE':'未完了工程の再試行待ち', 'REPLAN_VALIDATION':'再計画の検証失敗', 'REPLAN_LIMIT':'再計画の回数上限', 'MODEL_TIMEOUT':'モデル提供元・接続のタイムアウト',
              'GLOBAL_RECOVERY_LIMIT':'自動復旧の回数上限', 'NO_PROGRESS':'同じ操作失敗が続き、作業が進まなかったため',
              'VERIFICATION_CONTRACT_INVALID':'実行可能な検証コードを生成できなかったため（アプリの検証は未完了）',
              'VERIFICATION_EXECUTOR':'検証を実行する環境・コマンドを起動できなかったため',
              'VERIFICATION_EXECUTOR_UNAVAILABLE':'検証を実行する環境・コマンドを起動できなかったため',
              'PROJECT_TEST_REPAIR_EXHAUSTED':'テスト修正が上限に達したため（計画とアプリを保持しています）',
              'IMPLEMENTATION_REPAIR_EXHAUSTED':'実装結果を受け付けられる形式に修正できず、再試行上限に達したため（計画とファイルを保持しています）',
              'SOURCE_REPAIR_EXHAUSTED':'承認済みアプリ側の修正で検証失敗を解消できず、再試行上限に達したため（計画とファイルを保持しています）',
              'UNATTRIBUTED_TEST_FAILURE_EXHAUSTED':'テスト失敗を解消できず、承認済みアプリ側の修正上限に達したため（テストと計画を保持しています）',
              'STRUCTURAL_REPAIR_MODEL_UNAVAILABLE':'構造エラーを直せる別の設定済みコーディングモデルがないため（計画とファイルを保持しています）',
              'PROJECT_TEST_REPAIR_SCOPE_UNAVAILABLE':'テスト修正に必要なファイルが承認済み範囲にないため',
              'NARRATIVE_ONLY_RETRY_EXHAUSTED':'モデルが実装操作を出力せず、再試行上限に達したため'}
    text = ('作業を一時停止しました。' if intended else '作業を停止しました。') + '\n\n停止理由：' + labels.get(reason, reason)
    text += (f'\n工程：{len(completed)} / {len(phases)} 完了（保存済みの検証結果に基づく）' if phases else
             '\n工程：計画未確定（元の依頼の実装・検証の完了は確認できていません）')
    current_phase_id=task.get('current_phase_id')
    current_index=next((index for index,phase in enumerate(phases)
                        if phase.get('id')==current_phase_id),None)
    if current_index is not None:
        current_phase=phases[current_index]
        phase_rows=[row for row in rows if (row.get('phase_id') or
            (row.get('structured_report') or {}).get('phase_id'))==current_phase_id
            and ((row.get('structured_report') or {}).get('plan_revision') is None
                 or (row.get('structured_report') or {}).get('plan_revision')<=task.get('plan_revision',0))]
        text += f"\n{'現在の工程' if intended else '停止した工程'}：{current_index+1} / {len(phases)} — {current_phase.get('goal') or current_phase_id}"
        if phase_rows:
            text += f"\nこの工程の実行記録：{len(phase_rows)} 回"
    text += '\n\n完了した工程\n' + ('\n'.join('- ' + str(p.get('goal') or p['id']) for p in completed) or '- まだありません。')
    text += '\n\n残っている工程\n' + ('\n'.join('- ' + str(p.get('goal') or p['id']) for p in remaining) or
        ('- ありません。' if phases else '- 計画を確定し、元の依頼の実装・検証を確認します。'))
    text += '\n\n変更ファイル\n' + ('\n'.join('- ' + path for path in changed[:20]) or '- 保存済みの変更記録はありません。')
    text += '\n\n検証結果\n' + ('\n'.join(summaries)+'\n' if summaries else '')
    text += ('\n'.join('- 実行：' + check[:500] for check in checks[:10]) or '- テストの実行記録はありません。')
    details=[*typed_failures,*('- 失敗：'+item[:800] for item in failed[:3])]
    text += '\n' + ('\n'.join(details[:6]) if details else '- 保存済みのテスト失敗：なし（未実行のテストの合格を意味しません）。')
    if errors:
        text += '\n' + '\n'.join('- エラー：' + item[:500] for item in errors[:3])
    if reason == 'VERIFICATION_CONTRACT_INVALID':
        failure=(task.get('resume_state') or {}).get('verification_preparation_failure') or {}
        details=[*(task.get('blockers') or []),*([failure['error']] if failure.get('error') else [])]
        text += '\n' + '\n'.join('- 検証コードのエラー：' + str(item)[:500] for item in details[:3])
    if remaining:
        text += '\n\n未完了の先頭工程：「' + str(remaining[0].get('goal') or remaining[0]['id']) + '」。'
    if intended and remaining:
        text += '\n\n「続行」と入力すると、同じTaskの未完了工程から進めます。'
    if reason == 'PLANNING' and not phases:
        text += '\n\n「再試行」と入力すると、同じTaskで元の依頼の計画作成から再開します。'
    elif reason == 'REPLAN_INEFFECTIVE':
        text += '\n\n「再試行」と入力するか、進捗カードの「失敗情報を使ってこの工程を再実行」を押してください。保存済みの失敗情報を使って現在の工程を再実行します。変更したい条件もチャットへ入力できます。'
    elif reason == 'VERIFICATION_CONTRACT_INVALID':
        text += '\n\n「再試行」と入力すると、完了した実装を維持して検証コードの生成から再開します。'
    elif task.get('status') == 'BLOCKED':
        text += '\n\n「再試行」で停止条件と残りの復旧枠を再確認します。条件が解消していない場合や上限を使い切っている場合は再開しません。'
    payload = {'reason':reason, 'intended':intended, 'plan_revision':task.get('plan_revision'),
               'recovery_epoch':task.get('recovery_epoch'), 'text':text}
    payload['signature'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload
