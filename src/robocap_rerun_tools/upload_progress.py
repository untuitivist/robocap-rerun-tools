"""Upload progress visible in both the terminal and an optional Web callback."""

from contextlib import contextmanager
from datetime import datetime
from threading import Event, Thread
from time import monotonic


def terminal_progress(callback):
    def emit(message):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"{timestamp} | {message}", flush=True)
        if callback is not None and callback is not print:
            callback(message)
    return emit


@contextmanager
def upload_phase(progress, name, interval=30):
    started = monotonic()
    finished = Event()
    progress(f"[upload phase] START {name}")

    def heartbeat():
        while not finished.wait(interval):
            progress(f"[upload phase] RUNNING {name}; elapsed {monotonic() - started:.1f}s")

    thread = Thread(target=heartbeat, name="upload-progress", daemon=True)
    thread.start()
    state = "DONE"
    try:
        yield
    except BaseException:
        state = "FAILED"
        raise
    finally:
        finished.set()
        thread.join()
        progress(f"[upload phase] {state} {name}; elapsed {monotonic() - started:.1f}s")
