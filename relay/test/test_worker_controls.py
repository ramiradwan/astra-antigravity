"""Computer management uses durable local state, never model inference."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location("controlled_worker", Path(__file__).parents[1] / "worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class Gateway:
    def __init__(self):
        self.launches = 0
        self.cancelled = []

    async def launch(self, *_args):
        self.launches += 1
        return {"delegation_id": "local-task"}

    async def events(self, *_args):
        await asyncio.sleep(3600)

    async def cancel(self, local_id):
        self.cancelled.append(local_id)

    async def close(self):
        pass


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state = worker.private_directory(root / "state")
        workspace = root / "workspace"
        workspace.mkdir()
        self.config = {"relay": "https://relay.example", "worker_identity": "a" * 64 + "." + "b" * 32,
                       "workspaces": {"project": {"path": str(workspace), "isolation": ["worktree"],
                           "task_types": ["implementation"], "capabilities": []}}}
        self.gateway = Gateway()
        self.committed = []
        self.local = queue.Queue()
        self.callbacks = SimpleNamespace(local_controls=self.local, control_committed=self.committed.append)
        self.daemon = worker.Worker(self.config, self.state, self.gateway, controls=self.callbacks)
        self.command = {"command_id": "cmd", "delegation_id": "job", "operation": "delegate", "allow_start": True,
                        "payload": {"workspace_id": "project", "task_type": "implementation", "isolation": "worktree", "required_capabilities": []}}

    async def asyncTearDown(self):
        await self.daemon.close()
        self.temporary.cleanup()

    async def test_pause_blocks_in_transit_launch_without_consuming_identity(self):
        await self.daemon.computer_control({"operation_id": "pause.1", "action": "pause", "parameters": {}, "revision": 2})
        await self.daemon.command(self.command)
        self.assertEqual(self.gateway.launches, 0)
        self.assertIsNone(self.daemon.journal.db.execute("SELECT * FROM commands").fetchone())
        packets = [json.loads(row[0]) for row in self.daemon.journal.db.execute("SELECT payload FROM outbox")]
        self.assertTrue(any(row["kind"] == "command_deferred" for row in packets))
        await self.daemon.computer_control({"operation_id": "resume.1", "action": "resume", "parameters": {}, "revision": 3})
        await self.daemon.command(self.command)
        self.assertEqual(self.gateway.launches, 1)

    async def test_full_quit_fences_new_work_on_default_socket_path(self):
        self.daemon.external_stop = threading.Event()
        self.daemon.external_stop.set()
        await self.daemon.command(self.command)
        self.assertEqual(self.gateway.launches, 0)

    async def test_pause_persists_across_worker_restart_and_stale_resume_is_rejected(self):
        await self.daemon.computer_control({"operation_id": "pause.1", "action": "pause", "parameters": {}, "revision": 4})
        await self.daemon.close()
        self.daemon = worker.Worker(self.config, self.state, self.gateway, controls=self.callbacks)
        self.assertEqual(self.daemon.admission(), (True, 4))
        result = await self.daemon.computer_control({"operation_id": "old.resume", "action": "resume", "parameters": {}, "revision": 3})
        self.assertEqual(result["state"], "rejected")
        self.assertTrue(self.daemon.admission()[0])

    async def test_stop_waits_for_provider_terminal_evidence(self):
        await self.daemon.command(self.command)
        result = await self.daemon.computer_control({"operation_id": "stop.1", "action": "stop_tasks", "parameters": {}, "revision": 1})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.gateway.cancelled, ["local-task"])
        for task in self.daemon.monitors.values():
            task.cancel()
        await asyncio.gather(*list(self.daemon.monitors.values()), return_exceptions=True)
        await self.daemon.maintain_controls()
        saved = json.loads(self.daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='stop.1'").fetchone()[0])
        self.assertEqual(saved["state"], "pending", "Missing monitor is not evidence provider work stopped")
        self.daemon.journal.db.execute("UPDATE commands SET state='completed'")
        await self.daemon.maintain_controls()
        saved = json.loads(self.daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='stop.1'").fetchone()[0])
        self.assertEqual(saved["state"], "succeeded")
        self.assertTrue(self.daemon.admission()[0])

    async def test_local_pause_is_consumed_without_remote_receipt(self):
        self.local.put({"operation_id": "local.pause", "action": "pause", "parameters": {}})
        observed = []
        await self.daemon.maintain_controls(observed.append)
        self.assertTrue(self.daemon.admission()[0])
        self.assertTrue(observed[-1]["paused"])
        self.assertFalse(any(json.loads(row[0])["kind"] == "computer_control_result" for row in self.daemon.journal.db.execute("SELECT payload FROM outbox")))

    async def test_stop_cancels_launch_that_returns_its_receipt_after_the_stop(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.launch
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        self.gateway.launch = delayed
        launch = asyncio.create_task(self.daemon.command(self.command))
        self.daemon.commands["cmd"] = launch
        await entered.wait()
        result = await self.daemon.computer_control({"operation_id": "stop.race", "action": "stop_tasks", "parameters": {}, "revision": 1})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.gateway.cancelled, [])
        release.set()
        await launch
        self.daemon.commands.pop("cmd")
        self.assertEqual(self.gateway.cancelled, ["local-task"])
        await self.daemon.maintain_controls()
        result = json.loads(self.daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='stop.race'").fetchone()[0])
        self.assertEqual(result["state"], "pending")

    async def test_operation_identity_cannot_be_repurposed(self):
        packet = {"operation_id": "op.1", "action": "pause", "parameters": {}, "revision": 1}
        first = await self.daemon.computer_control(packet)
        self.assertEqual(first, await self.daemon.computer_control(packet))
        with self.assertRaises(ValueError):
            await self.daemon.computer_control({**packet, "action": "resume"})
        with self.assertRaises(ValueError):
            await self.daemon.computer_control({**packet, "operation_id": "bad", "parameters": {"command": "anything"}})

    async def test_offline_independent_settings_are_not_lost_to_latest_admission_revision(self):
        observed = []
        self.callbacks.control_handler = lambda action, parameters, identifier: (observed.append(action) or {"state": "succeeded", "summary": "Saved."})
        self.daemon.set_admission(True, 5)
        first = await self.daemon.computer_control({"operation_id": "settings.1", "action": "set_startup", "parameters": {"enabled": True}, "revision": 2})
        second = await self.daemon.computer_control({"operation_id": "settings.2", "action": "set_automatic_updates", "parameters": {"enabled": True}, "revision": 3})
        self.assertEqual(observed, ["set_startup", "set_automatic_updates"])
        self.assertEqual((first["state"], second["state"]), ("succeeded", "succeeded"))
        self.assertEqual((first["revision"], second["revision"]), (2, 3))
        self.assertEqual(self.daemon.admission(), (True, 5))

    async def test_uninstall_commit_callback_runs_only_after_durable_ack(self):
        calls = []
        def handler(action, parameters, identifier):
            calls.append(parameters)
            return {"state": "pending", "summary": "Removal scheduled."}
        self.callbacks.control_handler = handler
        await self.daemon.computer_control({"operation_id": "remove.1", "action": "uninstall", "parameters": {"erase_history": False}, "revision": 1,
            "completion": {"token": "c" * 48, "expires_at": int(time.time() * 1000) + 60000}})
        self.assertEqual(self.committed, [])
        self.assertEqual(calls[0]["completion"]["url"], "https://relay.example/worker/removals/" + "a" * 64 + "/remove.1/complete")
        self.daemon.acknowledge_packet("computer.result.remove.1.pending")
        self.assertEqual(self.committed, ["remove.1"])
        self.assertTrue(self.daemon.admission()[0])

    async def test_expired_uninstall_receipt_cannot_schedule_removal_or_block_controls(self):
        expired = []
        self.callbacks.control_expired = expired.append
        self.callbacks.control_handler = lambda *_args: {"state": "pending", "summary": "Removal scheduled."}
        await self.daemon.computer_control({"operation_id": "remove.expired", "action": "uninstall", "parameters": {"erase_history": False}, "revision": 1,
            "completion": {"token": "c" * 48, "expires_at": int(time.time() * 1000) + 60000}})
        self.daemon.acknowledge_packet("computer.result.remove.expired.pending", "operation_expired")
        self.assertEqual(self.committed, [])
        self.assertEqual(expired, ["remove.expired"])
        result = json.loads(self.daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='remove.expired'").fetchone()[0])
        self.assertEqual(result["state"], "unconfirmed")
        resumed = await self.daemon.computer_control({"operation_id": "resume.after.expiry", "action": "resume", "parameters": {}, "revision": 2})
        self.assertEqual(resumed["state"], "succeeded")


class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_renewal_discards_an_older_uncommitted_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            identity = "a" * 64 + "." + "b" * 32
            old = identity + "." + "c" * 64
            renewed = identity + "." + "e" * 64
            worker.private_json(state / "auth.json", {"credential": renewed, "refresh_credential": renewed, "approval_mode": "until_revoked"})
            worker.private_json(state / "credentials-rotation.json", {"worker_identity": identity, "rotation_id": "old.proposal", "proof": old,
                "origin_hash": hashlib.sha256(old.encode()).hexdigest(), "access_secret": "1" * 64, "refresh_secret": "2" * 64})
            observed = []
            class Server:
                async def request(self, method, path, body, *, credential):
                    observed.append(credential)
                    return {"rotation_id": body["rotation_id"], "owner_key": "a" * 64, "worker_id": "b" * 32,
                            "approval_mode": "until_revoked", "expires_at": int(time.time() * 1000) + 86_400_000}
            await worker.rotate_device_credentials(state, {"relay": "https://relay.example", "worker_identity": identity}, transport=Server())
            self.assertEqual(observed, [renewed])

    async def test_approved_server_conversion_recovers_a_legacy_local_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            identity = "a" * 64 + "." + "b" * 32
            worker.private_json(state / "auth.json", {"credential": identity + "." + "c" * 64})
            class ApprovedServer:
                async def request(self, method, path, body, *, credential):
                    return {"rotation_id": body["rotation_id"], "owner_key": "a" * 64, "worker_id": "b" * 32,
                            "approval_mode": "until_revoked", "expires_at": int(time.time() * 1000) + 86_400_000}
            result = await worker.rotate_device_credentials(state, {"relay": "https://relay.example", "worker_identity": identity},
                transport=ApprovedServer(), discover_approved_conversion=True)
            self.assertEqual(result["approval_mode"], "until_revoked")
            self.assertNotEqual(result["credential"], result["refresh_credential"])

    async def test_rotation_retries_same_secrets_after_lost_response_without_task_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            identity = "a" * 64 + "." + "b" * 32
            worker.private_json(state / "auth.json", {"credential": identity + "." + "c" * 64, "refresh_credential": identity + "." + "d" * 64, "approval_mode": "until_revoked"})
            requests = []
            class Transport:
                async def request(self, method, path, body, *, credential):
                    requests.append((body, credential))
                    if len(requests) == 1:
                        raise worker.TransportError(502)
                    return {"rotation_id": body["rotation_id"], "owner_key": "a" * 64, "worker_id": "b" * 32,
                            "approval_mode": "until_revoked", "expires_at": int(time.time() * 1000) + 86_400_000}
            transport = Transport()
            config = {"relay": "https://relay.example", "worker_identity": identity}
            with self.assertRaises(worker.TransportError):
                await worker.rotate_device_credentials(state, config, transport=transport)
            pending = json.loads((state / "credentials-rotation.json").read_text())
            result = await worker.rotate_device_credentials(state, config, transport=transport)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(result["credential"], identity + "." + pending["access_secret"])
            self.assertNotEqual(result["credential"], result["refresh_credential"])
            self.assertFalse((state / "credentials-rotation.json").exists())

    async def test_legacy_approval_does_not_gain_refresh_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            worker.private_json(state / "auth.json", {"credential": "a" * 64 + "." + "b" * 32 + "." + "c" * 64})
            with self.assertRaises(ValueError):
                await worker.rotate_device_credentials(state, {"relay": "https://relay.example"})


if __name__ == "__main__":
    unittest.main()
