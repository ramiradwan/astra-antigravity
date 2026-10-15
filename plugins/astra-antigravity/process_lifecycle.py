"""Stop only processes launched and retained by this plugin."""

import os
import signal
import subprocess

POPEN_TYPE = subprocess.Popen


def stop_owned_process(proc):
    if proc is None or proc.poll() is not None:
        return
    if isinstance(proc, POPEN_TYPE):
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                # Still reap the retained process if tree cleanup is unavailable.
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    else:
        # Explicit process doubles/diagnostic wrappers own their termination.
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if isinstance(proc, POPEN_TYPE) and os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.kill()
        proc.wait(timeout=1)
