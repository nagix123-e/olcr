"""Task-owned interruption for detached coding workers and direct runners."""
from contextlib import contextmanager
from threading import Lock
from .generation_control import Generation, current_generation, generation_scope

_lock=Lock()
_running={}

@contextmanager
def coding_generation(task_id):
    with _lock:
        existing=_running.get(task_id)
        generation=existing or current_generation() or Generation()
        if existing is None:_running[task_id]=generation
    try:
        with generation_scope(generation):yield generation
    finally:
        if existing is None:
            with _lock:
                if _running.get(task_id) is generation:_running.pop(task_id,None)

def cancel_coding_generation(task_id):
    with _lock:generation=_running.get(task_id)
    if generation is not None:generation.cancel()
    return generation is not None
