"""The speech-recognition model, shared by everything that listens.

A call and a spoken question both need the same model, and it takes a couple
of gigabytes. So there is one copy: loaded the first time someone needs it,
kept while it is in use, and given back after a quiet period.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_model = None
_users = 0
_last_used = 0.0


def acquire():
    """The model, loaded if it is not already. Pair every call with :func:`release`."""
    global _model, _users
    with _lock:
        if _model is None:
            import mlx.core as mx
            from parakeet_mlx.audio import get_logmel

            from jarvis import listen

            _model = listen.load_model()
            # The first decode is slow; get it out of the way before anyone is waiting.
            _model.generate(get_logmel(mx.zeros(listen.SAMPLE_RATE), _model.preprocessor_config))
        _users += 1
        return _model


def release() -> None:
    global _users, _last_used
    with _lock:
        _users = max(0, _users - 1)
        _last_used = time.monotonic()


def loaded() -> bool:
    return _model is not None


def unload_if_idle(seconds: float) -> bool:
    """Free the model when nobody has used it for ``seconds``. Returns True if it was freed."""
    global _model
    with _lock:
        if _model is None or _users > 0 or time.monotonic() - _last_used < seconds:
            return False
        _model = None
    import mlx.core as mx

    mx.clear_cache()
    return True
