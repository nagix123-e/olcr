from pathlib import Path
import importlib.util
import tempfile
import tarfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ReleaseBuildTests(unittest.TestCase):
    def test_built_archive_contains_recipes_without_optional_engine_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,content in {
                'backend/olcr_api/runtime_recipes/recipe.json':'{}',
                'frontend/dist/index.html':'<main>OLCR</main>',
                'desktop/src-tauri/binaries/olcr-knowledge-reader':'helper',
                'packaging/install.sh':'#!/bin/sh\n',
                'models/laya/english/model.safetensors':'must not ship',
                'models/laya/multilingual/model.safetensors':'must not ship',
                'agent-runtime/node_modules/engine.js':'must not ship',
                'mcp-runtime/node/browser/browser':'must not ship',
            }.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
            python=root/'input/python/bin/python3';python.parent.mkdir(parents=True);python.write_text('python')
            archive=root/'python.tar.gz'
            with tarfile.open(archive,'w:gz') as output:output.add(root/'input/python',arcname='python')
            with patch.object(BUILD,'ROOT',root),patch.object(BUILD,'RUNTIME',archive),patch.object(BUILD,'RUNTIME_SHA',BUILD.sha(archive)), \
                 patch.object(BUILD.platform,'system',return_value='Darwin'),patch.object(BUILD.platform,'machine',return_value='arm64'), \
                 patch.object(BUILD,'run'),patch.object(BUILD,'collect_licenses',return_value=[]), \
                 patch.object(BUILD,'stage_coding_knowledge',return_value={}),patch.object(BUILD,'CODING_KNOWLEDGE_INDEX',root/'fixture-index'),patch.object(BUILD,'SERENA_RUNTIME',None), \
                 patch.object(BUILD.subprocess,'check_output',return_value='test-commit'):
                BUILD.main()
            with tarfile.open(root/'release'/f'{BUILD.NAME}.tar.gz') as result:
                names=result.getnames()
            self.assertTrue(any(name.endswith('runtime_recipes/recipe.json') for name in names))
            self.assertFalse(any('model.safetensors' in name or 'node_modules' in name or '/mcp-runtime/' in name for name in names))

    def test_gui_acceptance_launcher_correlates_exact_app_backend_and_profile(self):
        source = (ROOT / "scripts" / "start-cua-gui-test.sh").read_text()
        self.assertIn('APP_COMMAND="$(ps -ww -p "$OLCR_PID"', source)
        self.assertIn('[[ "$APP_COMMAND" == "$BIN" ]]', source)
        self.assertIn('/usr/bin/open -n -W --stdout /dev/null --stderr "$LOG"', source)
        self.assertIn('--env "OLCR_APP_SUPPORT=$APP_SUPPORT"', source)
        self.assertIn('--env "OLCR_API_REPAIR_BUILD_ID=$BUILD_ID"', source)
        self.assertIn('CODESIGN_STABLE_BUILD_ID="olcr-cua-stable-screencapture-client-v1"', source)
        self.assertIn('OLCR_API_REPAIR_BUILD_ID="$CODESIGN_STABLE_BUILD_ID" cargo build', source)
        self.assertIn('[[ "$command_line" == "$BIN" ]] && APP_PIDS+=("$pid")', source)
        self.assertIn('OLCR_LAUNCH_METHOD=LAUNCH_SERVICES', source)
        self.assertNotIn('lsof -t -c olcr-desktop', source)
        self.assertIn('"${BACKEND_PIDS[0]}" == "$BACKEND_PID"', source)
        self.assertIn('"$BACKEND_PARENT" == "$OLCR_PID"', source)
        self.assertIn('actual_support == expected_support', source)
        self.assertIn('COMPUTER_USE_APP_SELECTION_RULE=EXACT_ABSOLUTE_PATH_ONLY', source)
        self.assertIn("pgrep -f 'OLCR\\.app/Contents/MacOS/olcr-desktop$'", source)
        self.assertIn('Another OLCR GUI is already running from $command_line (PID $pid)', source)
        self.assertIn('refusing to launch beside a different app/profile', source)
        self.assertIn('reject_orphaned_backends || exit 1', source)
        self.assertIn('reject_orphaned_mlx_workers || exit 1', source)
        self.assertIn('ORPHANED_OLCR_BACKEND_PID=$pid', source)
        self.assertIn('ORPHANED_OLCR_MLX_WORKER_PID=$pid', source)
        self.assertNotIn('cleanup_orphaned_backends', source)
        self.assertNotIn('cleanup_orphaned_mlx_workers', source)
        self.assertIn('for required_var in OLCR_CUA_TARGET_DIR OLCR_CUA_APP_SUPPORT OLCR_CUA_LOG OLCR_API_REPAIR_BUILD_ID', source)
        self.assertIn('must be set to a fresh, run-specific value', source)
        self.assertIn('must be an absolute path', source)
        self.assertIn('Refusing to reuse existing E2E path:', source)
        self.assertIn('Refusing GUI E2E with Keychain restoration disabled', source)
        self.assertIn('OLCR_CUA_BACKEND_STARTUP_TIMEOUT_SECONDS:-180', source)

    def test_gui_e2e_build_identity_is_runtime_scoped(self):
        source = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text()
        self.assertIn('env::var("OLCR_API_REPAIR_BUILD_ID")', source)
        self.assertIn('.or_else(|| option_env!("OLCR_API_REPAIR_BUILD_ID").map(str::to_owned))', source)
        self.assertIn('.env("OLCR_API_REPAIR_BUILD_ID", &build_id)', source)

    def test_release_identity_is_current(self):
        self.assertEqual("0.5.0", BUILD.VERSION)
        source = (ROOT / "scripts" / "build_release.py").read_text()
        self.assertIn("OLCR v0.5.0", source)
        self.assertNotIn("OLCR v0.1.3", source)

    def test_release_keeps_only_download_recipes_for_optional_engines(self):
        self.assertEqual("052592a15d198d9ad47da779604259b10b47b7aa", BUILD.LAYA_MULTILINGUAL_REVISION)
        source = (ROOT / "scripts" / "build_release.py").read_text()
        main = source[source.index("def main():"):]
        installer = (ROOT / "packaging" / "install.sh").read_text()
        self.assertIn('"optional_components"', main)
        self.assertNotIn('shutil.copytree(LAYA_', main)
        self.assertNotIn('stage_agent_runtime(stage', main)
        self.assertNotIn('stage_node_mcp_runtime(stage', main)
        self.assertNotIn('OLCR_LAYA_ENGLISH_REVISION', installer)
        self.assertNotIn('export OLCR_LAYA_CACHE=', installer)

    def test_runtime_cache_is_removed_from_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            cache = stage / "runtime" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "module.cpython-310.pyc").write_bytes(b"cache")
            (stage / "runtime" / "module.pyc").write_bytes(b"cache")
            (stage / "runtime" / "module.pyo").write_bytes(b"cache")
            (stage / "runtime" / "module.py").write_text("pass")
            BUILD.clean_runtime_cache(stage)
            self.assertEqual([], list(stage.rglob("__pycache__")))
            self.assertEqual([], list(stage.rglob("*.pyc")))
            self.assertEqual([], list(stage.rglob("*.pyo")))
            self.assertTrue((stage / "runtime" / "module.py").exists())


if __name__ == "__main__":
    unittest.main()
