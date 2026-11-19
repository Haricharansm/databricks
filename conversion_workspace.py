"""Bounded cleanup retries for engine scratch folders, including Windows file locks."""
from contextlib import contextmanager
import sys
import tempfile
import time

@contextmanager
def conversion_workspace(prefix,on_warning=lambda message:None):
    scratch=tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield scratch.name
    finally:
        primary=sys.exc_info()[1];error=None
        for delay in (0,.05,.1,.2,.4):
            if delay:time.sleep(delay)
            try:scratch.cleanup();error=None;break
            except OSError as exc:
                error=exc
                if not (isinstance(exc,PermissionError) or getattr(exc,'winerror',None) in (32,33)):break
        if error is not None:
            locked=isinstance(error,PermissionError) or getattr(error,'winerror',None) in (32,33)
            message=('Temporary conversion files remain at '+scratch.name+'. '+('An engine/log file is still locked. Close the engine before deleting this folder; it may contain source code and logs.' if locked else 'Scratch cleanup failed; inspect this folder.'))
            if primary is not None:primary.add_note(message)
            if locked:on_warning(message)
            elif primary is None:raise error
