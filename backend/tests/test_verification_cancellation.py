"""Stop must interrupt an owned verifier rather than wait for its deadline."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from olcr_api.coding_cancellation import coding_generation, cancel_coding_generation
from olcr_api.coding_verification import _run_acceptance_process
from olcr_api.generation_control import GenerationCancelled
from olcr_api.package_manager import PackageManagerExecutor


class VerificationCancellationTests(unittest.TestCase):
    def test_stop_interrupts_dependency_installation_without_running_npm(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'package.json').write_text('{}')
            processes=[]
            def spawn(argv,**kwargs):
                process=subprocess.Popen([sys.executable,'-c',
                    'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'],**kwargs)
                processes.append(process)
                return process
            with coding_generation('dependency-stop-test'):
                timer=threading.Timer(0.2,lambda:cancel_coding_generation('dependency-stop-test'))
                timer.start();started=time.monotonic()
                try:
                    with self.assertRaises(GenerationCancelled):
                        PackageManagerExecutor(popen=spawn).install(workspace_root=Path(directory),
                            package_manager='npm',packages=['react'],authorized_packages=['react'])
                finally:
                    timer.cancel();timer.join()
                self.assertLess(time.monotonic()-started,3)
            self.assertEqual(1,len(processes))
            self.assertIsNotNone(processes[0].poll())

    def test_stop_reaps_the_verifier_and_its_child(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'child.pid'
            code=("import subprocess,sys,time,signal; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "child=subprocess.Popen([sys.executable,'-c','import time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
                  "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)")
            def stop():
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                cancel_coding_generation('verifier-stop-test')
            with coding_generation('verifier-stop-test'):
                thread=threading.Thread(target=stop)
                thread.start()
                started=time.monotonic()
                try:
                    with self.assertRaises(GenerationCancelled):
                        _run_acceptance_process([sys.executable,'-c',code,str(marker)],directory,60)
                finally:
                    thread.join(timeout=6)
                self.assertLess(time.monotonic()-started,3)
            self.assertTrue(marker.exists())
            pid=int(marker.read_text())
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                try: os.kill(pid,0)
                except ProcessLookupError: break
                time.sleep(0.02)
            else: self.fail('Verifier child survived Stop')

    def test_timeout_keeps_captured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                _run_acceptance_process([sys.executable,'-c',"import time; print('started',flush=True); time.sleep(60)"],directory,0.3)
            self.assertIn('started',caught.exception.output)

    def test_already_cancelled_task_does_not_spawn_verification(self):
        with coding_generation('cancel-before-verification'):
            cancel_coding_generation('cancel-before-verification')
            with patch('olcr_api.coding_verification.subprocess.Popen') as spawn:
                with self.assertRaises(GenerationCancelled):
                    _run_acceptance_process(['unused'],'.',60)
                spawn.assert_not_called()
