"""Host-observed model invocation progress; contains no prompts or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
import uuid

_observer=ContextVar('model_progress_observer',default=None)
_invocation=ContextVar('model_progress_invocation',default=None)

def model_progress_active():return _observer.get() is not None
def current_model_invocation():return _invocation.get()

@contextmanager
def observe_model_progress(callback):
    token=_observer.set(callback);invocation_token=_invocation.set(None)
    try: yield
    finally:
        _observer.reset(token);_invocation.reset(invocation_token)

def model_progress(**data):
    callback=_observer.get()
    if data.get('state') in {'request_started','account_attempt','model_skipped'}:_invocation.set(uuid.uuid4().hex)
    if callback: callback({'invocation_id':_invocation.get(),**data})
    return _invocation.get()
