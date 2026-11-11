"""A cancellable sequential queue owned by one landing-flow session."""
from __future__ import annotations

import threading
import time
import uuid


class LandingDownloads:
    def __init__(self, model_downloads, optional_runtimes, conversion_runtime):
        self.model_downloads = model_downloads
        self.optional_runtimes = optional_runtimes
        self.conversion_runtime = conversion_runtime
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}

    def start(self, model_ids, components, confirm_download):
        from .optional_runtimes import NAMES
        if confirm_download is not True:
            raise ValueError('LANDING_DOWNLOAD_CONSENT_REQUIRED')
        if (not isinstance(model_ids, list) or any(not isinstance(x, str) for x in model_ids)
                or not isinstance(components, list) or any(not isinstance(x, str) for x in components)):
            raise ValueError('LANDING_DOWNLOAD_SELECTION_INVALID')
        model_ids = list(dict.fromkeys(model_ids))
        components = list(dict.fromkeys(components))
        if set(components) - (set(NAMES) | {'convertx'}):
            raise ValueError('LANDING_COMPONENT_UNKNOWN')
        known_models = self.model_downloads.presets()
        if set(model_ids) - set(known_models):
            raise ValueError('MODEL_DOWNLOAD_UNKNOWN_PRESET')
        # Laya is represented by one artifact whether selected as a model role
        # or as a local feature package.
        if 'laya_multilingual' in model_ids:
            components = [item for item in components if item != 'laya']
        with self.lock:
            if any(job['status'] in {'queued', 'downloading', 'cancelling'} for job in self.jobs.values()):
                raise ValueError('LANDING_DOWNLOAD_IN_PROGRESS')
            job = {'id': uuid.uuid4().hex, 'status': 'queued', 'model_ids': model_ids,
                   'components': components, 'completed_models': [], 'completed_components': [],
                   'current': None, 'error': None}
            self.jobs[job['id']] = job
            self.cancel_events[job['id']] = threading.Event()
        threading.Thread(target=self._run, args=(job['id'],), name='olcr-landing-downloads', daemon=True).start()
        return dict(job)

    def get(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            return dict(value) if value else None

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job['status'] not in {'queued', 'downloading'}:
                return False
            self.cancel_events[job_id].set()
            job['status'] = 'cancelling'
            active = job.get('model_job_id')
        if active:
            self.model_downloads.cancel(active)
        return True

    def _run(self, job_id):
        event = self.cancel_events[job_id]
        with self.lock:
            job = self.jobs[job_id]
            model_ids = list(job['model_ids'])
            components = list(job['components'])
            job['status'] = 'downloading'
        try:
            if model_ids:
                if event.is_set():
                    self._finish(job_id, 'cancelled')
                    return
                model_job = self.model_downloads.start(model_ids, True)
                with self.lock:
                    job['model_job_id'] = model_job['id']
                    job['current'] = model_ids[0]
                while True:
                    state = next((row for row in self.model_downloads.catalog()['jobs']
                                  if row.get('id') == model_job['id']), None)
                    if state is None:
                        raise RuntimeError('MODEL_DOWNLOAD_STATUS_LOST')
                    with self.lock:
                        job['current'] = state.get('current')
                        job['completed_models'] = list(state.get('completed') or [])
                    if state.get('status') in {'complete', 'failed', 'cancelled'}:
                        if state['status'] == 'failed':
                            raise RuntimeError('MODEL_DOWNLOAD_FAILED')
                        break
                    if event.wait(0.25):
                        self.model_downloads.cancel(model_job['id'])
                if event.is_set():
                    self._finish(job_id, 'cancelled')
                    return
            for name in components:
                if event.is_set():
                    self._finish(job_id, 'cancelled')
                    return
                with self.lock:
                    job['current'] = name
                if name == 'convertx':
                    self.conversion_runtime.download_assets(event)
                    if event.is_set():
                        self._finish(job_id, 'cancelled')
                        return
                    with self.lock:
                        job['completed_components'].append(name)
                    continue
                state = self.optional_runtimes.status(name)
                if state.get('state') != 'ready':
                    self.optional_runtimes.prepare(name)
                while True:
                    if event.is_set():
                        self._finish(job_id, 'cancelled')
                        return
                    state = self.optional_runtimes.status(name)
                    if state.get('state') == 'ready':
                        break
                    if state.get('state') == 'error':
                        raise RuntimeError('OPTIONAL_COMPONENT_DOWNLOAD_FAILED')
                    event.wait(0.25)
                with self.lock:
                    job['completed_components'].append(name)
            self._finish(job_id, 'complete')
        except Exception as exc:
            self._finish(job_id, 'failed', 'LANDING_DOWNLOAD_FAILED')

    def _finish(self, job_id, status, error=None):
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                job.update(status=status, current=None, error=error)

