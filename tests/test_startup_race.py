from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from server import TaskManager


class _Stdin:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def write(self, data):
        # The process exits synchronously as soon as the first prompt is sent,
        # before the reader thread publishes the terminal task state.
        self.proc.returncode = 1
        self.proc.exited.set()
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _DeadOnPromptProcess:
    pid = 4242

    def __init__(self):
        self.returncode = None
        self.exited = threading.Event()
        self.stdin = _Stdin(self)
        self.stdout = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise TimeoutError("still running")
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.exited.set()

    def kill(self):
        self.returncode = -9
        self.exited.set()


class _DelayedPublicationManager(TaskManager):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.initial_wait_seconds = 0.001
        self.reader_saw_exit = threading.Event()
        self.allow_terminal_publish = threading.Event()

    def _run_session(self, record, proc):
        proc.exited.wait(1)
        self.reader_saw_exit.set()
        # Hold terminal publication long enough to make the historical race
        # deterministic. A correct start_task notices proc.poll() and waits.
        self.allow_terminal_publish.wait(0.2)
        with self._lock:
            record.status = "failed"
            record.error = "Persistent Antigravity session ended before a result event."
            record.session_alive = False
            record.exit_code = proc.returncode
            self._session_processes.pop(record.task_id, None)
            self._session_threads.pop(record.task_id, None)
            self._release_workspace(record)
            self._notify(record)


class StartupRaceTests(unittest.TestCase):
    def _exercise_once(self, root: Path, iteration: int):
        state = root / f"state-{iteration}"
        workspace = root / f"workspace-{iteration}"
        workspace.mkdir()
        manager = _DelayedPublicationManager(state)
        proc = _DeadOnPromptProcess()
        manager._launch_session = lambda record: proc

        def release_after_reader_observes_exit():
            if manager.reader_saw_exit.wait(1):
                time.sleep(0.01)
                manager.allow_terminal_publish.set()

        releaser = threading.Thread(target=release_after_reader_observes_exit, daemon=True)
        releaser.start()
        try:
            with patch("server.find_agy", return_value="agy"):
                with self.assertRaisesRegex(RuntimeError, "failed to start"):
                    manager.start_task("die immediately", str(workspace))
            releaser.join(timeout=1)
            self.assertFalse(releaser.is_alive())

            record = next(reversed(manager._tasks.values()))
            self.assertEqual(record.status, "failed")
            self.assertFalse(record.session_alive)
            self.assertEqual(record.exit_code, 1)
        finally:
            manager.allow_terminal_publish.set()

    def test_dead_child_cannot_escape_startup_as_running_under_repetition(self):
        tmp = tempfile.mkdtemp(prefix="astra-startup-race-")
        try:
            root = Path(tmp)
            # Repeat enough times to exercise thread scheduling rather than
            # relying on one favorable interleaving.
            for iteration in range(20):
                with self.subTest(iteration=iteration):
                    self._exercise_once(root, iteration)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
