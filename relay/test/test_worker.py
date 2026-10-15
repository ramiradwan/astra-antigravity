"""Credential-free worker recovery and transport tests. No provider inference."""
import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from websockets.asyncio.server import serve

SPEC = importlib.util.spec_from_file_location("relay_worker", Path(__file__).parents[1] / "worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class FakeGateway:
    def __init__(self):
        self.launches = 0
        self.closed = 0
        self.acks = []
        self.updates = asyncio.Queue()

    async def launch(self, command, workspace):
        self.launches += 1
        return {"delegation_id": "local-task"}

    async def events(self, local_id, cursor):
        return await self.updates.get()

    def acknowledge(self, local_id, cursor):
        self.acks.append(cursor)

    def result(self, local_id):
        return {"status": "completed", "task_type": "implementation", "model": "sonnet-5.5",
                "implementation": {"summary": "Verified result"}, "planner": {"reasoning": "PRIVATE TRANSCRIPT"}}

    async def readiness(self, *args):
        return {"ready": True, "ready_for": {"implementation": True}}

    async def close(self):
        self.closed += 1
        await self.updates.put({"closed": True, "events": []})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = worker.private_directory(self.root / "private")
        self.config = {"relay": "http://127.0.0.1:8787", "workspaces": {"project": {
            "path": str(self.workspace), "label": "Project", "isolation": ["worktree", "shared"],
            "task_types": list(worker.TASK_TYPES), "capabilities": ["local_files", "shell"]}}}
        self.gateway = FakeGateway()
        self.daemon = worker.Worker(self.config, self.state, self.gateway)
        self.command = {"command_id": "cmd", "delegation_id": "job", "operation": "delegate", "allow_start": True,
            "payload": {"workspace_id": "project", "task": "Create synthetic output", "task_type": "implementation",
                        "isolation": "worktree", "reasoning": "light", "required_capabilities": []}}

    async def asyncTearDown(self):
        await self.daemon.close()
        self.temporary.cleanup()

    async def test_replayed_launch_reuses_private_identity_without_inference(self):
        await self.daemon.command(self.command)
        await self.daemon.command({**self.command, "allow_start": False})
        self.assertEqual(self.gateway.launches, 1)
        self.assertEqual(self.daemon.journal.job("job")["local_id"], "local-task")

    async def test_implementation_changed_files_are_available_for_parent_verification(self):
        await self.daemon.command(self.command)
        content=b"def answer(): return 42\n"
        (self.workspace/"answer.py").write_bytes(content)
        handoff=self.daemon.handoff("job",{"status":"completed","task_type":"implementation","implementation":{
            "summary":"Created answer", "files_changed":["answer.py","__pycache__/answer.pyc"],"worktree":{"path":str(self.workspace)}}})
        refs=handoff["artifact_refs"]
        self.assertEqual(len(refs),1)
        self.assertEqual(refs[0]["path"],"answer.py")
        self.assertEqual(self.daemon.journal.read_artifact("job",refs[0]["artifact_id"])["bytes"],len(content))

    async def test_missing_journal_for_leased_command_requires_recovery(self):
        await self.daemon.command({**self.command, "allow_start": False})
        await self.daemon.command(self.command)
        self.assertEqual(self.gateway.launches, 0)
        self.assertEqual(json.loads(self.daemon.journal.pending()["payload"])["kind"], "recovery_required")

    async def test_reused_command_cannot_change_approved_work(self):
        await self.daemon.command(self.command)
        changed = copy.deepcopy(self.command)
        changed["payload"]["task"] = "Different work"
        with self.assertRaisesRegex(ValueError, "identity was reused"):
            await self.daemon.command(changed)
        self.assertEqual(self.gateway.launches, 1)

    async def test_upstream_ack_follows_durable_outbox_write_and_cursor(self):
        await self.daemon.command(self.command)
        await self.gateway.updates.put({"closed": True, "events": [{"sequence": 1, "kind": "completed", "terminal": True,
            "summary": "Done", "task_type": "implementation", "verification_required": True}]})
        await asyncio.wait_for(self.daemon.monitors["job"], 1)
        row = self.daemon.journal.db.execute("SELECT payload FROM outbox WHERE id='event.job.1'").fetchone()
        self.assertEqual(self.gateway.acks, [1])
        self.assertEqual(self.daemon.journal.job("job")["cursor"], 1)
        self.assertNotIn("PRIVATE TRANSCRIPT", row["payload"])
        self.assertLessEqual(len(worker.canonical(json.loads(row["payload"])["result"])), worker.MAX_RESULT_CHARS)

    async def test_model_offer_preserves_evidence_that_implementation_never_started(self):
        self.daemon.journal.command(self.command)
        report = self.daemon.handoff("job", {"status":"model_unavailable", "implementation_started":False,
            "model_selection":{"preferred_profile":"argon-high", "available":False,
                "offered_fallback":{"profile":"opus-5.5", "available":True}}})
        self.assertIs(report["implementation_started"], False)
        self.assertTrue(report["model_selection"]["offered_fallback"]["available"])
        self.assertEqual(self.gateway.launches, 0)

    async def test_saved_launch_resumes_monitor_without_start_call(self):
        row, _ = self.daemon.journal.command(self.command)
        self.daemon.journal.launch(row["id"], "local-task")
        self.daemon.resume_saved()
        await self.gateway.updates.put({"delivery_suspended": True, "closed": True, "events": []})
        await asyncio.wait_for(self.daemon.monitors["job"], 1)
        self.assertEqual(self.gateway.launches, 0)
        self.assertEqual(json.loads(self.daemon.journal.pending()["payload"])["kind"], "recovery_required")

    async def test_outbox_survives_process_journal_reopen(self):
        self.daemon.journal.queue("saved", {"kind": "recovery_required", "delegation_id": "job"})
        self.daemon.journal.close()
        self.daemon.journal = worker.Journal(self.state)
        self.assertEqual(self.daemon.journal.pending()["id"], "saved")
        self.daemon.journal.acknowledge("saved")
        self.assertIsNone(self.daemon.journal.pending())

    async def test_artifact_export_pins_scope_and_content(self):
        artifact = self.workspace / "answer.txt"
        artifact.write_text("42", encoding="utf-8")
        ref = self.daemon.journal.artifact("job", "answer.txt", self.workspace)
        self.assertEqual(self.daemon.journal.read_artifact("job", ref["artifact_id"])["bytes"], 2)
        artifact.write_text("43", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.daemon.journal.read_artifact("job", ref["artifact_id"])
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.daemon.journal.read_artifact("other-job", ref["artifact_id"])
        protected = self.workspace / ".env"
        protected.write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "protected"):
            self.daemon.journal.artifact("job", ".env", self.workspace)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.daemon.journal.artifact("job", "../private/worker.sqlite3", self.workspace)

    async def test_large_download_snapshot_is_credit_bounded_and_not_persisted(self):
        content = b"PRIVATE-ARTIFACT-CONTENT\x00" * 1700
        artifact = self.workspace / "report.pdf"
        artifact.write_bytes(content)
        reference = self.daemon.journal.artifact("job", "report.pdf", self.workspace)
        self.assertTrue(reference["download_available"])
        self.assertFalse(reference["export_available"])
        self.assertEqual(reference["sha256"], hashlib.sha256(content).hexdigest())
        with self.assertRaisesRegex(ValueError, "size"):
            self.daemon.journal.read_artifact("job", reference["artifact_id"])
        packets = []
        async def send(packet):
            self.assertLessEqual(len(worker.canonical(packet).encode()), worker.MAX_FRAME)
            packets.append(packet)
        sender = worker.ArtifactSender(self.daemon.journal, send)
        await sender.handle({"kind": "artifact_open", "transfer_id": "download", "delegation_id": "job",
            "artifact_id": reference["artifact_id"], "max_bytes": worker.MAX_DOWNLOAD_BYTES, "chunk_bytes": worker.ARTIFACT_CHUNK_BYTES})
        self.assertEqual([item["kind"] for item in packets], ["artifact_manifest"])
        # The source can change after the immutable, verified snapshot is made.
        artifact.write_bytes(b"changed")
        downloaded = bytearray()
        while sender.active:
            await sender.handle({"kind": "artifact_credit", "transfer_id": "download", "credits": 1, "offset": len(downloaded)})
            chunk = next(item for item in reversed(packets) if item["kind"] == "artifact_chunk")
            raw = base64.b64decode(chunk["data"], validate=True)
            self.assertLessEqual(len(raw), 16 * 1024)
            downloaded.extend(raw)
        self.assertEqual(bytes(downloaded), content)
        self.assertEqual(packets[-1], {"kind": "artifact_end", "transfer_id": "download", "bytes": len(content), "sha256": reference["sha256"]})
        self.assertIsNone(self.daemon.journal.pending())
        dumped = "\n".join(self.daemon.journal.db.iterdump())
        self.assertNotIn("PRIVATE-ARTIFACT-CONTENT", dumped)
        self.assertNotIn(base64.b64encode(content[:64]).decode(), dumped)

    async def test_exact_twenty_mib_snapshot_is_downloadable_but_never_a_legacy_export(self):
        path = self.workspace / "maximum.pdf"
        with path.open("wb") as stream:
            stream.truncate(worker.MAX_DOWNLOAD_BYTES)
        reference = self.daemon.journal.artifact("job", path.name, self.workspace)
        self.assertEqual(reference["bytes"], 20 * 1024 * 1024)
        self.assertTrue(reference["download_available"])
        self.assertFalse(reference["export_available"])
        snapshot, manifest = self.daemon.journal.snapshot_artifact("job", reference["artifact_id"])
        try:
            checksum, counted = hashlib.sha256(), 0
            for chunk in iter(lambda: snapshot.read(worker.ARTIFACT_CHUNK_BYTES), b""):
                checksum.update(chunk)
                counted += len(chunk)
            self.assertEqual(counted, worker.MAX_DOWNLOAD_BYTES)
            self.assertEqual(checksum.hexdigest(), manifest["sha256"])
        finally:
            snapshot.close()
        self.assertIsNone(self.daemon.journal.pending())
        self.assertEqual(self.gateway.launches, 0)

    async def test_download_rejects_mutation_oversize_credit_and_expires_without_task_changes(self):
        path = self.workspace / "report.txt"
        path.write_bytes(b"a" * 20_000)
        ref = self.daemon.journal.artifact("job", path.name, self.workspace)
        packets = []
        async def send(packet): packets.append(packet)
        sender = worker.ArtifactSender(self.daemon.journal, send)
        opening = {"kind": "artifact_open", "transfer_id": "download", "delegation_id": "job", "artifact_id": ref["artifact_id"],
                   "max_bytes": worker.MAX_DOWNLOAD_BYTES, "chunk_bytes": worker.ARTIFACT_CHUNK_BYTES}
        path.write_bytes(b"b" * 20_000)
        await sender.handle(opening)
        self.assertEqual(packets[-1]["kind"], "artifact_error")
        self.assertIsNone(sender.active)
        path.write_bytes(b"a" * 20_000)
        await sender.handle(opening)
        await sender.handle({"kind": "artifact_credit", "transfer_id": "download", "credits": 2, "offset": 0})
        self.assertEqual(packets[-1]["code"], "artifact_invalid_credit")
        self.assertFalse(any(item["kind"] == "artifact_chunk" for item in packets))
        await sender.handle(opening)
        snapshot = sender.active["snapshot"]
        sender.active["touched"] -= worker.ARTIFACT_IDLE_SECONDS + 1
        await sender.expire()
        self.assertTrue(snapshot.closed)
        self.assertEqual(packets[-1]["code"], "artifact_timeout")
        await sender.handle(opening)
        snapshot = sender.active["snapshot"]
        await sender.handle({"kind": "artifact_credit", "transfer_id": "../invalid", "credits": 1, "offset": 0})
        self.assertFalse(snapshot.closed)
        await sender.handle({"kind": "artifact_abort", "transfer_id": "download"})
        self.assertTrue(snapshot.closed)
        self.assertIsNone(sender.active)
        with path.open("wb") as stream: stream.truncate(worker.MAX_DOWNLOAD_BYTES + 1)
        self.assertFalse(self.daemon.journal.artifact("job", path.name, self.workspace)["download_available"])
        self.assertEqual(self.gateway.launches, 0)
        self.assertEqual(self.gateway.closed, 0)
        self.assertIsNone(self.daemon.journal.pending())

    async def test_expired_delivery_ack_retires_only_its_job_and_preserves_provider_cursor(self):
        row, _ = self.daemon.journal.command(self.command)
        self.daemon.journal.launch(row["id"], "local-task")
        self.daemon.journal.queue("old-event", {"kind": "event", "delegation_id": "job"}, command_id="cmd", cursor=2)
        self.daemon.journal.queue("unrelated", {"kind": "recovery_required", "delegation_id": "other-job"})
        self.daemon.journal.acknowledge("not-present", "task_expired")
        self.assertEqual(self.daemon.journal.pending()["id"], "old-event")
        self.daemon.journal.acknowledge("old-event", "task_expired")
        self.daemon.journal.queue("later-event", {"kind": "event", "delegation_id": "job"}, command_id="cmd", cursor=3)
        self.assertEqual(self.daemon.journal.pending()["id"], "unrelated")
        self.assertEqual(self.daemon.journal.job("job")["local_id"], "local-task")
        self.assertEqual(self.daemon.journal.job("job")["cursor"], 3)
        self.assertEqual(self.daemon.journal.job("job")["state"], "launched")

    async def test_journal_cannot_be_rebound_to_a_new_worker_identity(self):
        identity = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
        self.daemon.journal.bind_identity(identity)
        self.daemon.journal.bind_identity(identity[:-64] + "d" * 64)
        with self.assertRaisesRegex(ValueError, "different worker"):
            self.daemon.journal.bind_identity("e" * 64 + "." + "f" * 32 + "." + "1" * 64)

    async def test_http_connection_failure_is_transport_loss_without_cancel(self):
        def unavailable(request):
            raise httpx.ConnectError("private URL and token must not be printed", request=request)
        transport = worker.HttpTransport("https://relay.example.test", "private", client=httpx.AsyncClient(transport=httpx.MockTransport(unavailable)))
        try:
            with self.assertRaises(worker.TransportError) as caught:
                await transport.pull()
            self.assertNotIn("private URL", str(caught.exception))
            self.assertEqual(self.gateway.closed, 0)
        finally:
            await transport.close()

    async def test_pairing_over_http1_without_credentials_then_with_claim(self):
        observed = []
        async def endpoint(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                observed.append(header.decode("ascii"))
                body = b'{"status":"ok"}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        async with await asyncio.start_server(endpoint, "127.0.0.1", 0) as server:
            origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            transport = worker.HttpTransport(origin, "")
            try:
                self.assertEqual(await transport.request("POST", "/worker/pair", {"label": "Synthetic"}), {"status": "ok"})
                self.assertEqual(await transport.request("GET", "/worker/pair/synthetic", credential="synthetic-claim"), {"status": "ok"})
            finally:
                await transport.close()
        self.assertNotIn("authorization:", observed[0].lower())
        self.assertIn("Authorization: Bearer synthetic-claim", observed[1])

    async def test_websocket_close_preserves_running_task_and_outbox(self):
        await self.daemon.command(self.command)
        async def drop(socket):
            await socket.close()
        async with serve(drop, "127.0.0.1", 0) as server:
            self.config["relay"] = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            with self.assertRaises(worker.TransportError):
                await asyncio.wait_for(worker.run_websocket(self.daemon, "private"), 2)
        self.assertEqual(self.gateway.closed, 0)
        self.assertFalse(self.daemon.monitors["job"].done())
        self.assertIsNotNone(self.daemon.journal.pending())

    async def test_websocket_artifact_packets_bypass_durable_outbox_and_disconnect_closes_snapshot(self):
        content = b"binary\x00" * 3000
        (self.workspace / "report.docx").write_bytes(content)
        ref = self.daemon.journal.artifact("job", "report.docx", self.workspace)
        observed = []
        async def download(socket):
            await socket.send(worker.canonical({"kind": "artifact_open", "transfer_id": "stream", "delegation_id": "job", "artifact_id": ref["artifact_id"],
                "max_bytes": worker.MAX_DOWNLOAD_BYTES, "chunk_bytes": worker.ARTIFACT_CHUNK_BYTES}))
            async for raw in socket:
                packet = json.loads(raw)
                if "message_id" in packet:
                    await socket.send(worker.canonical({"kind": "ack", "message_id": packet["message_id"]}))
                if packet["kind"] == "artifact_manifest":
                    await socket.send(worker.canonical({"kind": "artifact_credit", "transfer_id": "stream", "credits": 1, "offset": 0}))
                elif packet["kind"] == "artifact_chunk":
                    observed.append(packet)
                    await socket.close()
                    return
        async with serve(download, "127.0.0.1", 0) as server:
            self.config["relay"] = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            with self.assertRaises(worker.TransportError):
                await asyncio.wait_for(worker.run_websocket(self.daemon, "private"), 2)
        self.assertEqual(len(base64.b64decode(observed[0]["data"])), worker.ARTIFACT_CHUNK_BYTES)
        self.assertEqual(self.daemon.journal.db.execute("SELECT COUNT(*) FROM outbox WHERE payload LIKE '%artifact_chunk%'").fetchone()[0], 0)
        self.assertEqual(self.gateway.closed, 0)

    async def test_private_journal_lock_prevents_second_daemon(self):
        with worker.single_worker(self.state):
            with self.assertRaisesRegex(ValueError, "Another worker"):
                with worker.single_worker(self.state):
                    self.fail("Second worker acquired journal")

    async def test_worker_state_cannot_be_inside_delegated_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside every delegated workspace"):
            other = worker.Worker(self.config, self.workspace, self.gateway)
            await other.close()

    async def test_http_rejects_redirect_and_wrong_ack(self):
        for response in (httpx.Response(302, headers={"Location": "https://unapproved.example"}), httpx.Response(200, json={"kind": "ack", "message_id": "wrong"})):
            transport = worker.HttpTransport("https://relay.example.test", "private", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)))
            try:
                with self.assertRaises(worker.TransportError):
                    await transport.send({"message_id": "expected"})
            finally:
                await transport.close()


class RenewalTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_task_reconnects_after_explicit_rotation_without_new_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"; workspace.mkdir()
            state = worker.private_directory(root / "private")
            old = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
            rotated = old[:-64] + "d" * 64
            config = {"relay": "https://relay.example.test", "worker_identity": ".".join(old.split(".")[:2]), "workspaces": {
                "project": {"path": str(workspace), "isolation": ["shared"], "task_types": ["implementation"], "capabilities": []}}}
            worker.private_json(state / "worker.json", config)
            worker.private_json(state / "auth.json", {"credential": old})
            journal = worker.Journal(state)
            journal.command({"command_id": "saved", "delegation_id": "job", "operation": "delegate", "payload": {"workspace_id": "project"}})
            journal.launch("saved", "local-task")
            journal.bind_identity(old)
            journal.close()
            gateway, proofs = FakeGateway(), []
            async def transport(daemon, proof, *_args, **_kwargs):
                proofs.append(proof)
                if len(proofs) == 1:
                    raise worker.TransportError(401)
                await gateway.updates.put({"closed": True, "events": []})
                await asyncio.gather(*list(daemon.monitors.values()))
            def progress(value):
                if value["status"] == "access_expired":
                    worker.private_json(state / "auth.json", {"credential": rotated})
            args = SimpleNamespace(command="run", local=False, cloud_task_id=None, credential_env=None, transport="websocket", idle_timeout=0)
            with patch.object(worker, "Gateway", return_value=gateway), patch.object(worker, "run_websocket", transport), \
                    patch.object(worker, "rotate_device_credentials", AsyncMock(side_effect=worker.TransportError(403))):
                await asyncio.wait_for(worker.main_async(args, state, progress=progress), 2)
            self.assertEqual(proofs, [old, rotated])
            self.assertEqual(gateway.launches, 0)
            journal = worker.Journal(state)
            try:
                self.assertEqual(journal.job("job")["local_id"], "local-task")
            finally:
                journal.close()

    async def test_new_gateway_does_not_inherit_archived_provider_manager(self):
        created, stopped = [], []
        def manager(path):
            value = SimpleNamespace(state_dir=path, _tasks={}, cancel_task=stopped.append)
            created.append(value)
            return value
        provider = SimpleNamespace(TaskManager=manager)
        quota = SimpleNamespace(QuotaCoordinator=lambda base: SimpleNamespace(base=base))
        gateway_module = SimpleNamespace(_background_manager=None,
            AdvancedWorktreeCoordinator=lambda quota: SimpleNamespace(base=quota.base))
        modules = {"server": provider, "quota_server": quota, "luna_gateway": gateway_module}
        with tempfile.TemporaryDirectory() as directory, patch.dict(worker.os.environ), patch.object(worker.importlib, "import_module", side_effect=modules.__getitem__):
            first = worker.Gateway(Path(directory) / "old")
            first.module.coordinator.base._tasks["old-handle"] = SimpleNamespace(status="completed")
            await first.close()
            second = worker.Gateway(Path(directory) / "new")
            self.assertEqual(stopped, ["old-handle"])
            self.assertIsNot(created[0], created[1])
            self.assertEqual(second.module.coordinator.base._tasks, {})
            self.assertTrue(str(second.module.coordinator.base.state_dir).endswith("new\\provider-tasks") if worker.os.name == "nt"
                            else str(second.module.coordinator.base.state_dir).endswith("new/provider-tasks"))
            await second.close()

    async def test_interrupted_renewal_rotates_same_worker_and_preserves_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            credential = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
            worker.private_json(state / "worker.json", {"relay": "https://relay.example.test", "workspaces": {"project": {"path": "private-workspace"}}})
            worker.private_json(state / "auth.json", {"credential": credential})
            journal = worker.Journal(state)
            journal.bind_identity(credential)
            journal.queue("saved", {"kind": "recovery_required", "delegation_id": "job"})
            journal.close()
            before = (state / "worker.sqlite3").read_bytes()
            calls, published, stopped = [], [], threading.Event()
            class Transport:
                def __init__(self, origin, proof): self.proof = proof
                async def request(self, method, path, payload=None):
                    calls.append((method, path, payload, self.proof))
                    if path == "/worker/renew":
                        return {"renewal_id": "renewal", "verification_url": "https://relay.example.test/renew?code=renewal", "expires": int(time.time() * 1000) + 60_000}
                    return {"status": "approved", "owner_key": "a" * 64, "worker_id": "b" * 32, "expires": int(time.time() * 1000) + 100_000}
                async def close(self): pass
            def interrupted(value): published.append(value); stopped.set()
            with patch.object(worker, "HttpTransport", Transport):
                with self.assertRaisesRegex(ValueError, "resumed"):
                    await worker.renew(SimpleNamespace(local=False), state, progress=interrupted, stopped=stopped)
                pending = json.loads((state / "renewal.json").read_text())
                self.assertEqual(json.loads((state / "auth.json").read_text())["credential"], credential)
                stopped.clear()
                await worker.renew(SimpleNamespace(local=False), state, progress=published.append, stopped=stopped)
            self.assertEqual(sum(path == "/worker/renew" for _, path, _, _ in calls), 1)
            self.assertTrue(all(proof == credential for _, _, _, proof in calls))
            rotated = json.loads((state / "auth.json").read_text())["credential"]
            self.assertEqual(rotated, "a" * 64 + "." + "b" * 32 + "." + pending["secret"])
            self.assertEqual(calls[0][2]["credential_hash"], hashlib.sha256(pending["secret"].encode()).hexdigest())
            self.assertEqual((state / "worker.sqlite3").read_bytes(), before)
            self.assertFalse((state / "renewal.json").exists())
            self.assertNotIn(pending["secret"], json.dumps(published))
            self.assertNotIn(credential, json.dumps(published))

    async def test_renewal_rejects_identity_change_without_overwriting_old_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            credential = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
            worker.private_json(state / "worker.json", {"relay": "https://relay.example.test", "workspaces": {}})
            worker.private_json(state / "auth.json", {"credential": credential})
            class Transport:
                def __init__(self, *_args): pass
                async def request(self, method, path, payload=None):
                    if path == "/worker/renew":
                        return {"renewal_id": "renewal", "verification_url": "https://relay.example.test/renew?code=renewal", "expires": int(time.time() * 1000) + 60_000}
                    return {"status": "approved", "owner_key": "a" * 64, "worker_id": "d" * 32}
                async def close(self): pass
            with patch.object(worker, "HttpTransport", Transport), self.assertRaisesRegex(ValueError, "identity"):
                await worker.renew(SimpleNamespace(local=False), state, progress=lambda _value: None)
            self.assertEqual(json.loads((state / "auth.json").read_text())["credential"], credential)
            self.assertTrue((state / "renewal.json").exists())

    async def test_lost_begin_response_retries_the_staged_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            credential = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
            worker.private_json(state / "worker.json", {"relay": "https://relay.example.test", "workspaces": {}})
            worker.private_json(state / "auth.json", {"credential": credential})
            hashes = []
            class Transport:
                def __init__(self, *_args): pass
                async def request(self, method, path, payload=None):
                    if path == "/worker/renew":
                        hashes.append(payload["credential_hash"])
                        if len(hashes) == 1:
                            raise worker.TransportError()
                        return {"renewal_id": "renewal", "verification_url": "https://relay.example.test/renew?code=renewal", "expires": int(time.time() * 1000) + 60_000}
                    return {"status": "approved", "owner_key": "a" * 64, "worker_id": "b" * 32}
                async def close(self): pass
            with patch.object(worker, "HttpTransport", Transport):
                with self.assertRaises(worker.TransportError):
                    await worker.renew(SimpleNamespace(local=False), state, progress=lambda _value: None)
                await worker.renew(SimpleNamespace(local=False), state, progress=lambda _value: None)
            self.assertEqual(len(hashes), 2)
            self.assertEqual(hashes[0], hashes[1])

    async def test_expired_receipt_recovers_approved_rotation_without_new_browser_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            state = worker.private_directory(directory)
            credential = "a" * 64 + "." + "b" * 32 + "." + "c" * 64
            worker.private_json(state / "worker.json", {"relay": "https://relay.example.test", "workspaces": {}})
            worker.private_json(state / "auth.json", {"credential": credential})
            worker.private_json(state / "renewal.json", {"old_credential": credential, "secret": "d" * 64,
                "receipt": {"renewal_id": "renewal", "verification_url": "https://relay.example.test/renew?code=renewal", "expires": int(time.time() * 1000) - 1}})
            published, calls = [], []
            class Transport:
                def __init__(self, *_args): pass
                async def request(self, method, path, payload=None):
                    calls.append(path)
                    return {"status": "approved", "owner_key": "a" * 64, "worker_id": "b" * 32}
                async def close(self): pass
            with patch.object(worker, "HttpTransport", Transport):
                await worker.renew(SimpleNamespace(local=False), state, progress=published.append)
            self.assertEqual(calls, ["/worker/renew/renewal"])
            self.assertEqual([item["status"] for item in published], ["renewed"])
            self.assertEqual(json.loads((state / "auth.json").read_text())["credential"], credential[:-64] + "d" * 64)


if __name__ == "__main__":
    unittest.main()
