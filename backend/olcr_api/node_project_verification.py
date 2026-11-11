"""Build Vite acceptance previews in a disposable copy, preserving user sources.

The host invokes fixed Node/Vite/TypeScript entry points. Package scripts and
commands appearing in model-authored criteria are never executed as shell code.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from .package_manager import PackageManagerExecutor, PackageManagerError, _validate_spec

_prepare_lock = threading.Lock()
_CONFIGS = ('vite.config.js', 'vite.config.mjs', 'vite.config.ts',
            'vite.config.cjs', 'vite.config.mts', 'vite.config.cts')
_IGNORED = {'.git', 'node_modules', 'dist', '.olcr-preview', '.olcr', '.DS_Store'}


def is_vite_project(root: Path | None = None, *, paths=()) -> bool:
    """Recognize planned/configured projects and Vite's config-free packages."""
    files=set(paths)
    package={}
    if root is not None:
        root=Path(root)
        files.update(name for name in ('package.json','index.html',*_CONFIGS)
                     if (root/name).is_file())
        try:
            value=json.loads((root/'package.json').read_text())
            if isinstance(value,dict):package=value
        except (OSError,ValueError):
            pass
    declared=any(isinstance(package.get(kind),dict) and 'vite' in package[kind]
                 for kind in ('dependencies','devDependencies'))
    return ('package.json' in files and 'index.html' in files
            and (declared or any(name in files for name in _CONFIGS)))


def _failure(command, contract, message, *, executor=False, path=None, evidence=None):
    ids = list(dict.fromkeys(i for check in contract.get('checks', []) for i in check.get('ids', [])))
    failure = {'ids': ids, 'error': message[-8000:], 'contract_error': executor, 'executor_error': executor}
    if path:
        failure['source_locations'] = [{'path': path}]
    locations = [{'path': match[1], 'line': int(match[2]), 'column': int(match[3])}
                 for match in re.finditer(r'(?:^|\n)([^\n()]+\.[cm]?[jt]sx?)\((\d+),(\d+)\):', message)][:4]
    if locations:
        failure['source_locations'] = locations
        # The existing diagnosis transport consumes application-source frames
        # here for browser and compiler failures alike.
        failure['browser_errors'] = [{'message': message[-4000:], 'source_locations': locations,
                                      'source_paths': list(dict.fromkeys(item['path'] for item in locations))}]
    payload = {'passed_ids': [], 'failures': [failure], 'test_runs': [], 'browser_checks': 0,
               'executor_error': executor}
    result = subprocess.CompletedProcess(command, 1, 'OLCR_VERIFICATION_RESULT=' + json.dumps(payload), '')
    result.project_build = dict(evidence or {})
    return result


def _dependencies(package):
    result = {}
    for kind in ('dependencies', 'devDependencies'):
        if kind in package and not isinstance(package[kind],dict):
            raise ValueError('Invalid package dependency declarations')
        values = package.get(kind) or {}
        if not isinstance(values, dict):
            raise ValueError('Invalid package dependency declarations')
        specs = []
        for name, version in values.items():
            spec, _ = _validate_spec(name + '@' + str(version))
            specs.append(spec)
        result[kind] = specs
    return result


def _copy_sources(root: Path, target: Path):
    total = 0
    for directory, folders, files in __import__('os').walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in _IGNORED
                      and not Path(directory, name).is_symlink()]
        for name in files:
            source = Path(directory, name)
            if name in _IGNORED or source.is_symlink():
                continue
            total += source.stat().st_size
            if total > 32 * 1024 * 1024:
                raise ValueError('Vite source snapshot exceeds 32 MiB')
            destination = target / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def run_vite_acceptance(command, cwd, timeout, run_process):
    root = Path(cwd).resolve()
    contract = json.loads(Path(command[2]).read_text())
    evidence = {'source_isolated': True, 'install_passed': False,
                'typecheck_executed': False, 'typecheck_passed': False,
                'build_executed': False, 'build_passed': False}
    try:
        try:
            package = json.loads((root / 'package.json').read_text())
            if not isinstance(package,dict):
                raise ValueError('package.json must contain an object')
            dependencies = _dependencies(package)
        except ValueError as exc:
            return _failure(command, contract, str(exc), path='package.json', evidence=evidence)
        names = {spec.rsplit('@', 1)[0] for values in dependencies.values() for spec in values}
        if 'vite' not in names:
            return _failure(command, contract, 'Vite is missing from package.json', path='package.json', evidence=evidence)
        # The cache belongs to this task's acceptance profile. Never install
        # into or rewrite the selected user workspace during verification.
        identity = hashlib.sha256(json.dumps(dependencies, sort_keys=True).encode()).hexdigest()[:24]
        cache = Path(command[2]).resolve().parent / 'node-project-runtime' / identity
        with _prepare_lock:
            if not (cache / 'ready.json').is_file():
                cache.mkdir(parents=True, exist_ok=True)
                (cache / 'package.json').write_text(json.dumps({'private': True}), encoding='utf8')
                manager = PackageManagerExecutor(timeout_seconds=timeout)
                for kind, specs in dependencies.items():
                    if specs:
                        manager.install(workspace_root=cache, packages=specs, dependency_kind=kind,
                                        package_manager='npm', authorized_packages=specs,
                                        authorized_workspace_root=cache)
                (cache / 'ready.json').write_text(json.dumps({'dependencies': dependencies}), encoding='utf8')
        evidence['install_passed'] = True
        with tempfile.TemporaryDirectory(prefix='olcr-vite-acceptance-') as temporary:
            snapshot = Path(temporary).resolve()
            _copy_sources(root, snapshot)
            (snapshot / 'node_modules').symlink_to(cache / 'node_modules', target_is_directory=True)
            node = command[0]
            tsc = snapshot / 'node_modules/typescript/bin/tsc'
            if any(snapshot.glob('tsconfig*.json')):
                if not tsc.is_file():
                    return _failure(command, contract, 'TypeScript is missing from package.json', path='package.json', evidence=evidence)
                evidence['typecheck_executed'] = True
                checked = run_process([node, str(tsc), '--build', '--pretty', 'false'], str(snapshot), timeout)
                if checked.returncode:
                    return _failure(command, contract, checked.stdout + checked.stderr, evidence=evidence)
                evidence['typecheck_passed'] = True
            vite = snapshot / 'node_modules/vite/bin/vite.js'
            if not vite.is_file():
                return _failure(command, contract, 'Installed Vite entry point is unavailable', executor=True, evidence=evidence)
            evidence['build_executed'] = True
            built = run_process([node, str(vite), 'build', '--outDir', '.olcr-preview', '--emptyOutDir',
                                 '--base', '/'], str(snapshot), timeout)
            if built.returncode:
                return _failure(command, contract, built.stdout + built.stderr, evidence=evidence)
            if not (snapshot / '.olcr-preview/index.html').is_file():
                return _failure(command, contract, 'Vite did not produce index.html', evidence=evidence)
            evidence['build_passed'] = True
            isolated_contract = {**contract, '_olcr_vite_preview': '.olcr-preview',
                                 '_olcr_project_build': {'install_passed': True, 'build_passed': True}}
            copied_contract = snapshot / '.olcr-acceptance.json'
            copied_contract.write_text(json.dumps(isolated_contract), encoding='utf8')
            actual = [*command[:2], str(copied_contract), str(snapshot), *command[4:]]
            result = run_process(actual, str(snapshot), timeout)
            observed = subprocess.CompletedProcess(command, result.returncode,
                result.stdout.replace(str(snapshot), str(root)), result.stderr.replace(str(snapshot), str(root)))
            # Attach evidence in the host process; model-authored check stdout
            # cannot manufacture a successful build/typecheck here.
            observed.project_build = dict(evidence)
            return observed
    except PackageManagerError as exc:
        source_error = exc.failure_class in {'PACKAGE_SPEC_INVALID', 'PACKAGE_NOT_FOUND', 'VERSION_NOT_FOUND',
                                             'DEPENDENCY_KIND_INVALID', 'PACKAGE_MANAGER_METADATA_INVALID'}
        details = exc.details.get('evidence') or {}
        # PackageManager's failure_reason is already bounded/redacted. Retain
        # the measured package/version diagnostic instead of sending only the
        # generic "installation failed" exception to source repair.
        reason = str(details.get('failure_reason') or '')[:2000]
        message = f'{exc.failure_class}: {exc}' + (': '+reason if reason else '')
        return _failure(command, contract, message, executor=not source_error,
                        path='package.json' if source_error else None, evidence=evidence)
    except (OSError, ValueError) as exc:
        return _failure(command, contract, str(exc), executor=True, evidence=evidence)
