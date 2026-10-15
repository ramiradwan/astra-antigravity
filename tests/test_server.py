import dataclasses
import io
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import server
from server import TaskManager, TaskRecord, _usage_delta, truncate_value, find_agy


class QueueStream:
    def __init__(self):
        self.q = queue.Queue()
        self.closed = False

    def emit(self, obj):
        if isinstance(obj, dict):
            data = json.dumps(obj) + "\n"
        else:
            data = obj
        self.q.put(data.encode("utf-8") if isinstance(data, str) else data)

    def close_stream(self):
        self.q.put(None)

    def readline(self):
        item = self.q.get(timeout=5)
        return b"" if item is None else item

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if line == b"":
            raise StopIteration
        return line


class EmptyStream:
    def readline(self):
        return b""


class CapturingStdin:
    def __init__(self, on_message=None, on_close=None):
        self.writes = []
        self.closed = False
        self.on_message = on_message
        self.on_close = on_close

    def write(self, data):
        if self.closed:
            raise ValueError("stdin closed")
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        self.writes.append(text)
        if self.on_message:
            self.on_message(text)
        return len(data)

    def flush(self):
        return None

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.on_close:
            self.on_close()


class FakeStreamingProcess:
    def __init__(self, pid=1001, on_message=None):
        self.pid = pid
        self.returncode = None
        self.stdout = QueueStream()
        self.stderr = EmptyStream()
        self._done = threading.Event()
        self.stdin = CapturingStdin(on_message=on_message, on_close=self._close_clean)

    def _close_clean(self):
        self.finish(0)

    def finish(self, code=0):
        if self.returncode is None:
            self.returncode = code
            self.stdout.close_stream()
            self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("still running")
        return self.returncode

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


class FakeOneShotProcess(FakeStreamingProcess):
    def __init__(self, lines, pid=2001, returncode=0):
        super().__init__(pid=pid)
        self.stdin = None
        for line in lines:
            self.stdout.emit(line)
        self.finish(returncode)


class TestHelpers(unittest.TestCase):
    def test_truncate_value(self):
        long = "x" * 5000
        self.assertIn("truncated", truncate_value(long, 100))

    def test_usage_delta(self):
        self.assertEqual(
            _usage_delta({"total_tokens": 150, "output_tokens": 20}, {"total_tokens": 100, "output_tokens": 7}),
            {"total_tokens": 50, "output_tokens": 13},
        )

    def test_find_agy_override(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            path = tf.name
        try:
            with patch.dict(os.environ, {"AGY_PATH": path}):
                self.assertEqual(find_agy(), str(Path(path).resolve()))
        finally:
            os.unlink(path)

    def test_find_agy_msix_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msix_agy = root / "Packages" / "OpenAI.Codex_abc123" / "LocalCache" / "Local" / "agy" / "bin" / "agy.exe"
            msix_agy.parent.mkdir(parents=True, exist_ok=True)
            msix_agy.write_text("dummy", encoding="utf-8")
            with patch.dict(os.environ, {"AGY_PATH": "", "LOCALAPPDATA": str(root)}), \
                 patch("shutil.which", return_value=None), \
                 patch("os.name", "nt"):
                self.assertEqual(find_agy(), str(msix_agy.resolve()))


class TestPersistentSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agy_v020_")
        self.state = Path(self.tmp) / "state"
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()
        self.manager = TaskManager(self.state)
        self.manager.initial_wait_seconds = 0.01
        self.manager.session_idle_ttl_seconds = 60

    def tearDown(self):
        for proc in list(self.manager._session_processes.values()):
            try:
                if proc.stdin and not getattr(proc.stdin, "closed", False):
                    proc.stdin.close()
            except Exception:
                pass
        time.sleep(0.02)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_launch_session_uses_streaming_input_and_no_print_prompt(self):
        record = TaskRecord(
            task_id="task-command",
            workspace=str(self.workspace),
            status="started",
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id=server.SERVER_INSTANCE_ID,
            log_path=str(self.state / "x.log"),
            meta_path=str(self.state / "x.json"),
        )
        sentinel = object()
        with patch("server.find_agy", return_value="agy"), patch("server.subprocess.Popen", return_value=sentinel) as popen:
            got = self.manager._launch_session(record)
        self.assertIs(got, sentinel)
        cmd = popen.call_args.args[0]
        self.assertIn("--input-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertNotIn("-p", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], str(self.workspace))
        self.assertEqual(popen.call_args.kwargs["stdin"], server.subprocess.PIPE)

    def test_two_turns_reuse_same_process_and_compute_usage_delta(self):
        prompt_count = 0
        proc = None

        def on_message(text):
            nonlocal prompt_count
            prompt_count += 1
            payload = json.loads(text)
            self.assertEqual(payload["event"], "user")
            if prompt_count == 1:
                proc.stdout.emit({"event": "init", "conversation_id": "cid-hot"})
                proc.stdout.emit({"event": "result", "result": {
                    "conversation_id": "cid-hot", "status": "SUCCESS", "response": "first",
                    "num_turns": 1, "duration_seconds": 4.0,
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                }})
            else:
                proc.stdout.emit({"event": "result", "result": {
                    "conversation_id": "cid-hot", "status": "SUCCESS", "response": "second",
                    "num_turns": 2, "duration_seconds": 6.5,
                    "usage": {"input_tokens": 130, "output_tokens": 17, "total_tokens": 147},
                }})

        proc = FakeStreamingProcess(pid=3333, on_message=on_message)
        self.manager._launch_session = lambda record: proc

        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("first prompt", str(self.workspace))

        self.assertTrue(self.manager.wait_for_task(record.task_id, 1)["finished"])
        self.assertEqual(record.status, "completed")
        self.assertTrue(record.session_alive)
        self.assertEqual(record.pid, 3333)

        follow = self.manager.steer_task(record.task_id, "second prompt")
        self.assertTrue(follow["session_reused"])
        self.assertEqual(follow["pid"], 3333)
        self.assertTrue(self.manager.wait_for_task(record.task_id, 1)["finished"])

        result = self.manager.get_result(record.task_id)
        self.assertEqual(result["response"], "second")
        self.assertEqual(result["num_turns"], 2)
        self.assertEqual(result["usage"], {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37})
        self.assertAlmostEqual(result["duration_seconds"], 2.5)
        self.assertEqual(len(proc.stdin.writes), 2)
        self.assertTrue(record.session_alive)

    def test_wait_finishes_on_result_while_reader_thread_stays_alive(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-wait"})
            threading.Timer(0.08, lambda: proc.stdout.emit({
                "event": "result", "result": {
                    "conversation_id": "cid-wait", "status": "SUCCESS", "response": "done",
                    "num_turns": 1, "duration_seconds": 1.0, "usage": {"total_tokens": 10},
                }
            })).start()

        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("wait", str(self.workspace))
        t0 = time.monotonic()
        waited = self.manager.wait_for_task(record.task_id, 1)
        self.assertTrue(waited["finished"])
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(waited["session_alive"])
        self.assertTrue(self.manager._session_threads[record.task_id].is_alive())

    def test_wait_timeout_returns_unfinished(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-long"})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("long", str(self.workspace))
        t0 = time.monotonic()
        waited = self.manager.wait_for_task(record.task_id, 0.12)
        self.assertFalse(waited["finished"])
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)
        self.manager.cancel_task(record.task_id)

    def test_queued_followup_is_sent_only_after_first_result(self):
        proc = None
        def on_message(text):
            if len(proc.stdin.writes) == 1:
                proc.stdout.emit({"event": "init", "conversation_id": "cid-queue"})
            elif len(proc.stdin.writes) == 2:
                proc.stdout.emit({"event": "result", "result": {
                    "conversation_id": "cid-queue", "status": "SUCCESS", "response": "turn2",
                    "num_turns": 2, "duration_seconds": 2.0, "usage": {"total_tokens": 20},
                }})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("turn1", str(self.workspace))

        queued = self.manager.steer_task(record.task_id, "turn2")
        self.assertTrue(queued["queued"])
        self.assertEqual(len(proc.stdin.writes), 1)

        proc.stdout.emit({"event": "result", "result": {
            "conversation_id": "cid-queue", "status": "SUCCESS", "response": "turn1",
            "num_turns": 1, "duration_seconds": 1.0, "usage": {"total_tokens": 10},
        }})

        self.assertTrue(self.manager.wait_for_task(record.task_id, 1)["finished"])
        self.assertEqual(len(proc.stdin.writes), 2)
        self.assertEqual(self.manager.get_result(record.task_id)["response"], "turn2")
        self.assertEqual(record.current_turn_index, 2)

    def test_completed_turn_releases_workspace_while_session_stays_hot(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-unlock"})
            proc.stdout.emit({"event": "result", "result": {
                "conversation_id": "cid-unlock", "status": "SUCCESS", "response": "done",
                "num_turns": 1, "duration_seconds": 1, "usage": {"total_tokens": 1},
            }})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("one", str(self.workspace))
        self.manager.wait_for_task(record.task_id, 1)
        self.assertNotIn(self.manager._workspace_key(record.workspace), self.manager._active_workspaces)
        self.assertTrue(record.session_alive)

    def test_idle_ttl_closes_session_but_preserves_completed_result(self):
        self.manager.session_idle_ttl_seconds = 0.08
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-idle"})
            proc.stdout.emit({"event": "result", "result": {
                "conversation_id": "cid-idle", "status": "SUCCESS", "response": "idle done",
                "num_turns": 1, "duration_seconds": 1, "usage": {"total_tokens": 5},
            }})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("idle", str(self.workspace))
        self.manager.wait_for_task(record.task_id, 1)
        time.sleep(0.16)
        self.assertFalse(record.session_alive)
        self.assertEqual(record.status, "completed")
        self.assertEqual(self.manager.get_result(record.task_id)["response"], "idle done")

    def test_followup_after_idle_close_uses_conversation_fallback(self):
        self.manager.session_idle_ttl_seconds = 0.05
        session = None
        def first_on_message(text):
            session.stdout.emit({"event": "init", "conversation_id": "cid-fallback"})
            session.stdout.emit({"event": "result", "result": {
                "conversation_id": "cid-fallback", "status": "SUCCESS", "response": "first",
                "num_turns": 1, "duration_seconds": 1, "usage": {"total_tokens": 10},
            }})
        session = FakeStreamingProcess(pid=4100, on_message=first_on_message)
        self.manager._launch_session = lambda record: session
        fallback = FakeOneShotProcess([
            {"event": "result", "result": {
                "conversation_id": "cid-fallback", "status": "SUCCESS", "response": "fallback second",
                "num_turns": 2, "duration_seconds": 2, "usage": {"total_tokens": 16},
            }}
        ], pid=4200)
        self.manager._launch_fallback_turn = lambda record, prompt: fallback

        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("first", str(self.workspace))
        self.manager.wait_for_task(record.task_id, 1)
        time.sleep(0.12)
        self.assertFalse(record.session_alive)

        follow = self.manager.steer_task(record.task_id, "second")
        self.assertFalse(follow["session_reused"])
        self.assertTrue(self.manager.wait_for_task(record.task_id, 1)["finished"])
        self.assertEqual(self.manager.get_result(record.task_id)["response"], "fallback second")
        self.assertEqual(record.current_turn_index, 2)

    def test_cancel_running_task_wakes_waiter(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-cancel"})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("run", str(self.workspace))

        result_box = {}
        waiter = threading.Thread(target=lambda: result_box.update(self.manager.wait_for_task(record.task_id, 2)))
        waiter.start()
        time.sleep(0.05)
        cancel = self.manager.cancel_task(record.task_id)
        waiter.join(1)
        self.assertTrue(cancel["cancelled"])
        self.assertEqual(result_box["status"], "cancelled")
        self.assertTrue(result_box["finished"])

    def test_restart_marks_inflight_unknown_but_keeps_completed(self):
        completed = TaskRecord(
            task_id="task-completed",
            workspace=str(self.workspace),
            status="completed",
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id="old",
            log_path=str(self.state / "task-completed.log"),
            meta_path=str(self.state / "task-completed.json"),
            conversation_id="cid-old",
            latest_response="done",
            session_alive=True,
            pid=999,
        )
        save = server.save_metadata
        save(completed)
        running = dataclasses.replace(
            completed,
            task_id="task-running",
            status="running",
            meta_path=str(self.state / "task-running.json"),
            log_path=str(self.state / "task-running.log"),
        )
        save(running)

        new = TaskManager(self.state)
        c = new.get_task("task-completed")
        r = new.get_task("task-running")
        self.assertEqual(c.status, "completed")
        self.assertFalse(c.session_alive)
        self.assertEqual(r.status, "unknown")

    def test_error_result_marks_failed_and_closes_session(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-error"})
            proc.stdout.emit({"event": "result", "result": {
                "conversation_id": "cid-error", "status": "ERROR", "response": "",
                "error": "boom", "num_turns": 1, "duration_seconds": 1,
                "usage": {"total_tokens": 3},
            }})
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            try:
                record = self.manager.start_task("fail", str(self.workspace))
            except RuntimeError:
                record = next(reversed(self.manager._tasks.values()))
        waited = self.manager.wait_for_task(record.task_id, 1)
        self.assertTrue(waited["finished"])
        self.assertEqual(waited["status"], "failed")
        self.assertIn("boom", self.manager.get_result(record.task_id)["error"])
        with self.manager._lock:
            reader = self.manager._session_threads.get(record.task_id)
        if reader:
            reader.join(1)
        self.assertFalse(record.session_alive)

    def test_unexpected_eof_during_running_turn_marks_failed(self):
        proc = None
        def on_message(text):
            proc.stdout.emit({"event": "init", "conversation_id": "cid-eof"})
            proc.finish(1)
        proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: proc
        with patch("server.find_agy", return_value="agy"):
            with self.assertRaises(RuntimeError):
                self.manager.start_task("eof", str(self.workspace))
        record = next(reversed(self.manager._tasks.values()))
        waited = self.manager.wait_for_task(record.task_id, 1)
        self.assertEqual(waited["status"], "failed")
        self.assertFalse(record.session_alive)

    def test_fallback_command_uses_conversation_resume(self):
        record = TaskRecord(
            task_id="task-fallback-command",
            workspace=str(self.workspace),
            status="completed",
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id=server.SERVER_INSTANCE_ID,
            log_path=str(self.state / "f.log"),
            meta_path=str(self.state / "f.json"),
            conversation_id="cid-resume",
        )
        sentinel = object()
        with patch("server.find_agy", return_value="agy"), patch("server.subprocess.Popen", return_value=sentinel) as popen:
            got = self.manager._launch_fallback_turn(record, "follow up")
        self.assertIs(got, sentinel)
        cmd = popen.call_args.args[0]
        self.assertIn("-p", cmd)
        self.assertIn("follow up", cmd)
        self.assertIn("--conversation", cmd)
        self.assertIn("cid-resume", cmd)
        self.assertNotIn("--input-format", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], str(self.workspace))

    def test_parallel_active_turn_in_same_workspace_is_rejected(self):
        first_proc = None
        def on_message(text):
            first_proc.stdout.emit({"event": "init", "conversation_id": "cid-lock"})
        first_proc = FakeStreamingProcess(on_message=on_message)
        self.manager._launch_session = lambda record: first_proc
        with patch("server.find_agy", return_value="agy"):
            record = self.manager.start_task("first", str(self.workspace))
            with self.assertRaisesRegex(ValueError, "currently locked"):
                self.manager.start_task("second", str(self.workspace))
        self.manager.cancel_task(record.task_id)


if __name__ == "__main__":
    unittest.main()
