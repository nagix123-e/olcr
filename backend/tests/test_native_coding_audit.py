"""The opt-in native audit must reject unsafe profiles before loading models."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock


class NativeCodingAuditGuardTests(unittest.TestCase):
    script=Path(__file__).resolve().parents[2]/'scripts/verify-coding-orchestrator-native.py'

    def run_audit(self,*args):
        return subprocess.run([sys.executable,str(self.script),*map(str,args)],capture_output=True,text=True,timeout=10)

    def test_help_does_not_load_backend_or_models(self):
        result=self.run_audit('--help')
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertIn('--resume',result.stdout)
        self.assertNotIn('API_REPAIR_BUILD_ID',result.stdout+result.stderr)

    def test_existing_and_non_temporary_profiles_are_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'sentinel.txt';marker.write_text('unchanged')
            result=self.run_audit('--profile',directory)
            self.assertEqual(2,result.returncode)
            self.assertIn('existing profiles are never overwritten',result.stderr)
            self.assertEqual('unchanged',marker.read_text())
        result=self.run_audit('--profile',Path.home()/'olcr-native-audit-refuse-user-location')
        self.assertEqual(2,result.returncode)
        self.assertIn('inside a temporary directory',result.stderr)

    def test_resume_requires_the_audit_identity_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_audit('--profile',directory,'--resume')
            self.assertEqual(2,result.returncode)
            self.assertIn('Cannot resume this audit profile',result.stderr)
            self.assertNotIn('API_REPAIR_BUILD_ID',result.stdout+result.stderr)

    def audit_module(self):
        spec=importlib.util.spec_from_file_location('native_coding_audit',self.script)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_ordinary_requeue_is_drained_without_human_recovery(self):
        state={'status':'QUEUED','plan_revision':0,'recovery_epoch':0}
        def execute(identifier,root):
            if state['plan_revision']==0:state['plan_revision']=1
            else:state['status']='COMPLETED'
            return 'ordinary scheduler continuation'
        api=SimpleNamespace(db=SimpleNamespace(coding_task=lambda _:state,coding_phase_reports=lambda _:[]),
                            run_managed_task=Mock(side_effect=execute))
        self.audit_module().drain_task(api,'isolated-task',Path('/private/tmp/project'))
        self.assertEqual(2,api.run_managed_task.call_count)
        self.assertEqual(0,state['recovery_epoch'])

    def test_intended_stop_is_not_resumed_by_audit_driver(self):
        api=SimpleNamespace(db=SimpleNamespace(coding_task=lambda _:{'status':'RESUMABLE','pause_requested':True}),
                            run_managed_task=Mock(),_pause_at_checkpoint=Mock())
        self.audit_module().drain_task(api,'isolated-task',Path('/private/tmp/project'))
        api.run_managed_task.assert_not_called()
        api._pause_at_checkpoint.assert_not_called()

    def test_requested_audit_pause_uses_the_host_checkpoint(self):
        api=SimpleNamespace(db=SimpleNamespace(coding_task=lambda _:{'status':'QUEUED','pause_requested':True}),
                            run_managed_task=Mock(),_pause_at_checkpoint=Mock())
        self.audit_module().drain_task(api,'isolated-task',Path('/private/tmp/project'))
        api._pause_at_checkpoint.assert_called_once_with('isolated-task')
        api.run_managed_task.assert_not_called()

    def test_unchanged_queued_checkpoint_fails_instead_of_spinning(self):
        api=SimpleNamespace(db=SimpleNamespace(coding_task=lambda _:{'status':'QUEUED'},coding_phase_reports=lambda _:[]),
                            run_managed_task=Mock(return_value='no progress'))
        with self.assertRaisesRegex(RuntimeError,'without advancing'):
            self.audit_module().drain_task(api,'isolated-task',Path('/private/tmp/project'))
        self.assertEqual(1,api.run_managed_task.call_count)


if __name__=='__main__':unittest.main()
