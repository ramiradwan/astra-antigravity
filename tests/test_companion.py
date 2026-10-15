"""First-use consent, local UI security, singleton lifecycle and installer checks."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion.controller import Controller, worker
from companion import integration, providers
from companion import app as companion_app


class Providers:
    accounts = staticmethod(lambda _path: {"codex":{"installed":True,"status":"connected"}, "agy":{"installed":True,"status":"available"}, "git":{"installed":True,"status":"available"}})
    find_program = staticmethod(lambda *_args: None)


class WorkerAPI:
    TransportError = worker.TransportError
    private_directory = staticmethod(worker.private_directory)
    private_json = staticmethod(worker.private_json)
    safe_id = staticmethod(worker.safe_id)
    relay_origin = staticmethod(worker.relay_origin)
    single_worker = staticmethod(worker.single_worker)
    def __init__(self):
        self.pairings = 0
        self.renewals = 0
        self.starts = 0
        self.approve = threading.Event()
        self.running = threading.Event()
    async def initialise(self, args, state, progress, stopped):
        self.pairings += 1
        progress({"status":"approval_required", "verification_url":"https://relay.example.test/pair?code=" + "a" * 24})
        while not self.approve.is_set():
            if stopped.is_set():
                return
            await asyncio.sleep(.01)
        worker.private_json(Path(state) / "worker.json", {"relay":args.relay,"workspaces":{"project":{"path":args.shared_workspace[0].split("=",1)[1],"isolation":["shared"]}}})
        worker.private_json(Path(state) / "auth.json", {"credential":"PRIVATE-TEST-CREDENTIAL"})
        progress({"status":"paired", "workspace_ids":["project"]})
    async def main_async(self, args, state, progress, stopped):
        self.starts += 1
        progress({"status":"connected", "transport":"websocket", "active_tasks":0})
        self.running.set()
        while not stopped.is_set():
            await asyncio.sleep(.01)
    async def renew(self, args, state, progress, stopped):
        self.renewals += 1
        progress({"status": "renewal_approval_required", "verification_url": "https://relay.example.test/renew?code=synthetic"})
        worker.private_json(Path(state) / "auth.json", {"credential": "ROTATED-PRIVATE-TEST-CREDENTIAL"})
        progress({"status": "renewed", "workspace_ids": ["project"]})


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "approved-folder"
        self.workspace.mkdir()
        self.api = WorkerAPI()
        self.opened = []
        self.controller = Controller(self.root / "private", provider_api=Providers, worker_api=self.api, browser=self.opened.append)
        self.controller.check_accounts()
        self.controller.operation.join(2)
    def tearDown(self):
        self.controller.close()
        if self.controller.operation:
            self.controller.operation.join(2)
        self.temporary.cleanup()

    def test_fresh_setup_approves_once_and_reuses_existing_worker_without_inference(self):
        with self.assertRaisesRegex(ValueError,"approval"):
            self.controller.approve_workspace(str(self.workspace),"shared",approved=False)
        self.assertEqual(self.api.pairings,0)
        self.controller.approve_workspace(str(self.workspace),"shared",approved=True)
        for _ in range(100):
            if self.controller.snapshot()["status"] == "approval_required": break
            time.sleep(.01)
        self.assertEqual(self.controller.snapshot()["status"],"approval_required")
        self.assertEqual(self.api.starts,0)
        self.api.approve.set()
        self.assertTrue(self.api.running.wait(2))
        self.controller.operation.join(2)
        self.controller.start()
        self.assertEqual(self.api.starts,1)
        self.assertEqual(self.api.pairings,1)
        self.assertNotIn("PRIVATE-TEST-CREDENTIAL",json.dumps(self.controller.snapshot()))
        self.controller.stop()
        self.controller.worker_thread.join(2)
        self.assertTrue((self.controller.connection / "auth.json").exists())

    def test_state_cannot_be_inside_approved_workspace(self):
        with self.assertRaisesRegex(ValueError,"private state"):
            self.controller.approve_workspace(str(self.root),"shared",approved=True)

    def test_renewal_is_explicit_reuses_connection_and_preserves_local_state(self):
        self.controller.approve_workspace(str(self.workspace), "shared", approved=True)
        self.api.approve.set()
        self.assertTrue(self.api.running.wait(2))
        self.controller.operation.join(2)
        self.controller.stop()
        self.controller.worker_thread.join(2)
        journal = self.controller.connection / "worker.sqlite3"
        journal.write_bytes(b"SAVED-JOURNAL-AND-CURSORS")
        self.controller.update({"status": "access_expired"})
        self.controller.renew_connection()
        self.controller.operation.join(2)
        self.assertEqual(self.api.renewals, 1)
        self.assertEqual(self.api.pairings, 1)
        self.assertEqual(journal.read_bytes(), b"SAVED-JOURNAL-AND-CURSORS")
        self.assertEqual(self.opened, [])
        self.assertIsNone(self.controller.snapshot()["verification_url"])
        self.assertNotIn("ROTATED-PRIVATE", json.dumps(self.controller.snapshot()))

    def test_fresh_approval_archives_old_identity_and_never_rebinds_saved_task(self):
        self.controller.attach_request("old-task")
        worker.private_json(self.controller.connection / "worker.json", {"relay": self.controller.relay, "workspaces": {}})
        worker.private_json(self.controller.connection / "auth.json", {"credential": "OLD-PRIVATE-PROOF"})
        (self.controller.connection / "worker.sqlite3").write_bytes(b"OLD-JOURNAL")
        provider = self.controller.connection / "provider-tasks"
        provider.mkdir()
        (provider / "saved.json").write_text('{"provider_handle":"saved","cursor":7}')
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.controller.fresh_connection(approved=False)
        with worker.single_worker(self.controller.connection):
            with self.assertRaisesRegex(ValueError, "Another worker"):
                self.controller.fresh_connection(approved=True)
        self.controller.fresh_connection(approved=True)
        archives = list((self.controller.state / "worker-archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "worker.sqlite3").read_bytes(), b"OLD-JOURNAL")
        self.assertIn('"cursor":7', (archives[0] / "provider-tasks/saved.json").read_text())
        self.assertIsNone(self.controller.snapshot()["setup_request_id"])
        self.assertFalse((self.controller.connection / "worker.sqlite3").exists())
        self.assertEqual(self.api.starts, 0)
        self.assertEqual(self.api.pairings, 0)
        self.assertFalse((self.controller.state / "setup-request.json").exists())

    def test_missing_proof_requires_fresh_approval_and_cannot_overwrite_old_journal(self):
        (self.controller.connection / "worker.sqlite3").write_bytes(b"OLD-JOURNAL")
        with self.assertRaisesRegex(ValueError, "connection details are missing"):
            self.controller.renew_connection()
        with self.assertRaisesRegex(ValueError, "saved connection details"):
            self.controller.approve_workspace(str(self.workspace), "shared", approved=True)
        self.assertEqual((self.controller.connection / "worker.sqlite3").read_bytes(), b"OLD-JOURNAL")
        self.assertEqual(self.api.pairings, 0)
        self.assertEqual(self.controller.snapshot()["recovery_action"], "fresh")

    def test_connection_recovery_uses_explicit_evidence_and_preserves_saved_work(self):
        worker.private_json(self.controller.connection / "worker.json", {"relay": self.controller.relay, "workspaces": {}})
        journal = self.controller.connection / "worker.sqlite3"
        journal.write_bytes(b"SAVED-PROVIDER-HANDLES-AND-CURSORS")
        for fault, expiry, action in (
            (worker.TransportError(), -1000, "reconnect"),
            (worker.TransportError(503), 60000, "reconnect"),
            (worker.TransportError(401, "invalid_worker_token"), 60000, "reconnect"),
            (worker.TransportError(409), 60000, "reconnect"),
            (worker.TransportError(401, "invalid_worker_token"), -1000, "renew"),
            (worker.TransportError(401, "worker_expired"), 60000, "renew"),
            (worker.TransportError(403, "worker_revoked"), 60000, "fresh"),
            (RuntimeError("local failure"), 60000, "reconnect"),
        ):
            with self.subTest(status=getattr(fault, "status", None), code=getattr(fault, "code", None), expiry=expiry):
                worker.private_json(self.controller.connection / "auth.json", {"credential": "UNCHANGED-PRIVATE-PROOF", "expires": time.time() * 1000 + expiry})
                async def fail(*_args, **_kwargs):
                    raise fault
                with patch.object(self.api, "main_async", fail):
                    self.controller.start()
                    self.controller.worker_thread.join(2)
                snapshot = self.controller.snapshot()
                self.assertEqual(snapshot["recovery_action"], action)
                self.assertEqual(snapshot["fresh_approval_required"], action == "fresh")
                self.assertEqual(journal.read_bytes(), b"SAVED-PROVIDER-HANDLES-AND-CURSORS")
                self.assertEqual(json.loads((self.controller.connection / "auth.json").read_text())["credential"], "UNCHANGED-PRIVATE-PROOF")
        self.assertEqual(self.api.pairings, 0)
        self.assertEqual(self.api.renewals, 0)
        self.assertEqual(self.opened, [])

    def test_legacy_access_label_does_not_guess_expiry_without_saved_evidence(self):
        worker.private_json(self.controller.connection / "worker.json", {"relay": self.controller.relay, "workspaces": {}})
        worker.private_json(self.controller.connection / "auth.json", {"credential": "UNCHANGED"})
        self.controller.connection_progress({"status": "access_expired", "active_tasks": 2})
        self.assertEqual(self.controller.snapshot()["recovery_action"], "reconnect")
        self.assertEqual(self.controller.snapshot()["active_tasks"], 2)
        worker.private_json(self.controller.connection / "auth.json", {"credential": "UNCHANGED", "expires": time.time() * 1000 - 1000})
        self.controller.connection_progress({"status": "access_expired", "active_tasks": 2})
        self.assertEqual(self.controller.snapshot()["recovery_action"], "renew")
        self.assertEqual(self.api.pairings, 0)
        self.assertEqual(self.api.renewals, 0)

    def test_renewal_approval_expiry_retries_same_worker_while_denial_offers_fresh_approval(self):
        worker.private_json(self.controller.connection / "worker.json", {"relay": self.controller.relay, "workspaces": {}})
        worker.private_json(self.controller.connection / "auth.json", {"credential": "UNCHANGED-PRIVATE-PROOF"})
        journal = self.controller.connection / "worker.sqlite3"
        journal.write_bytes(b"SAVED-JOURNAL")
        for fault, action in ((worker.TransportError(410, "renewal_expired"), "renew"), (worker.TransportError(403, "renewal_denied"), "fresh")):
            with self.subTest(action=action):
                self.controller.update({"status": "access_expired", "recovery_action": "renew", "fresh_approval_required": False})
                async def fail(*_args, **_kwargs):
                    raise fault
                with patch.object(self.api, "renew", fail):
                    self.controller.renew_connection()
                    self.controller.operation.join(2)
                self.assertEqual(self.controller.snapshot()["recovery_action"], action)
                self.assertEqual(self.controller.snapshot()["fresh_approval_required"], action == "fresh")
                self.assertEqual(journal.read_bytes(), b"SAVED-JOURNAL")
                self.assertEqual(json.loads((self.controller.connection / "auth.json").read_text())["credential"], "UNCHANGED-PRIVATE-PROOF")
                if action == "fresh":
                    # The old daemon may report its original rejection after
                    # finishing provider work. It cannot undo renewal denial.
                    self.controller.connection_progress({"status": "access_expired", "active_tasks": 0})
                    self.assertEqual(self.controller.snapshot()["recovery_action"], "fresh")
        self.assertEqual(self.api.starts, 0)
        self.assertEqual(self.api.pairings, 0)






    def test_empty_folder_never_approves_the_process_working_directory(self):
        with self.assertRaisesRegex(ValueError,"Choose a workspace"):
            self.controller.approve_workspace("","shared",approved=True)
        self.assertEqual(self.api.pairings,0)

    def test_account_connection_is_required_before_workspace_pairing(self):
        self.controller.update({"accounts":{}})
        with self.assertRaisesRegex(ValueError,"Connect Codex"):
            self.controller.approve_workspace(str(self.workspace),"shared",approved=True)
        self.assertEqual(self.api.pairings,0)

    def test_provider_check_reports_its_action_without_starting_delegated_work(self):
        entered, release = threading.Event(), threading.Event()
        accounts = Providers.accounts(None)
        def check(_path):
            entered.set()
            release.wait(2)
            return accounts
        try:
            with patch.object(Providers, "accounts", side_effect=check):
                self.controller.check_accounts()
                self.assertTrue(entered.wait(2))
                snapshot = self.controller.snapshot()
                self.assertTrue(snapshot["busy"])
                self.assertEqual(snapshot["message"], "Checking provider sign-ins…")
                self.assertEqual(self.api.starts, 0)
                self.assertEqual(self.api.pairings, 0)
        finally:
            release.set()
            self.controller.operation.join(2)

    def test_saved_task_survives_setup_restart_and_cannot_change_during_pairing(self):
        self.controller.attach_request("original-task")
        reopened = Controller(self.controller.state, provider_api=Providers, worker_api=self.api)
        self.assertEqual(reopened.snapshot()["setup_request_id"], "original-task")
        self.controller.update({"status":"pairing"})
        with self.assertRaisesRegex(ValueError,"current browser approval"):
            self.controller.attach_request("another-task")
        self.assertEqual(self.controller.arguments("init").setup_request_id,"original-task")




    def test_connection_links_cannot_inject_local_paths_or_commands(self):
        self.assertEqual(integration.parse_connection_uri("astra-antigravity://connect?request_id=original-task"),"original-task")
        for url in ("https://evil.example/connect", "astra-antigravity://connect?relay=https://evil.example", "astra-antigravity://connect?request_id=x&request_id=y", 'astra-antigravity://connect?request_id=x" --state C:/other',"astra-antigravity://connect?request_id=../other"):
            with self.subTest(url=url),self.assertRaises(ValueError): integration.parse_connection_uri(url)

    def test_installer_verifies_digests_and_refuses_unapproved_redirects(self):
        providers.verify(b"verified",hashlib.sha256(b"verified").hexdigest())
        with self.assertRaises(ValueError): providers.verify(b"changed",hashlib.sha256(b"verified").hexdigest())
        with self.assertRaises(ValueError): providers.check_url("https://github.com@evil.example/asset",{"github.com"})
        with self.assertRaises(ValueError): providers.check_url("http://github.com/asset",{"github.com"})
        target=self.root/"binary"
        providers.write_executable(target,b"verified")
        with self.assertRaises(ValueError): providers.write_executable(target,b"replacement")

    @unittest.skipUnless(os.name == "nt", "Windows provider archive")
    def test_codex_installer_preserves_official_helper_binaries_and_bounds_paths(self):
        def archive(escape=False):
            data=io.BytesIO()
            with zipfile.ZipFile(data,"w") as output:
                output.writestr("codex-x86_64-pc-windows-msvc.exe",b"main")
                output.writestr("codex-command-runner.exe",b"helper")
                output.writestr("../escape" if escape else "resources/notice.txt",b"resource")
            return data.getvalue()
        with patch.object(providers,"find_program",return_value=None), patch.object(providers.platform,"machine",return_value="x86_64"), patch.object(providers.platform,"system",return_value="Windows"):
            with patch.object(providers,"github_asset",return_value=("codex.exe.zip",archive())):
                providers.install("codex",self.root/"bin")
            self.assertEqual((self.root/"bin/codex/codex.exe").read_bytes(),b"main")
            self.assertEqual((self.root/"bin/codex/codex-command-runner.exe").read_bytes(),b"helper")
            self.assertEqual((self.root/"bin/codex/resources/notice.txt").read_bytes(),b"resource")
            with patch.object(providers,"github_asset",return_value=("codex.exe.zip",archive(True))),self.assertRaisesRegex(ValueError,"escaped"):
                providers.install("codex",self.root/"rejected")
            self.assertFalse((self.root/"rejected").exists())


class PairingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_setup_resumes_the_original_claim_without_another_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=root/"workspace";workspace.mkdir();state=worker.private_directory(root/"private")
            args=SimpleNamespace(relay="https://relay.example.test",local=False,workspace=[],shared_workspace=["project="+str(workspace)],label="My worker",codex_cloud=False,cloud_task_id=None,setup_request_id="original-task")
            stop=threading.Event();calls=[];published=[]
            class Transport:
                def __init__(self,*_args): pass
                async def request(self,method,path,payload=None,credential=None):
                    calls.append((method,path,credential))
                    if method=="POST": return {"code":"a"*24,"claim":"b"*64,"expires":int(time.time()*1000)+600000,"verification_url":"https://relay.example.test/pair?code="+"a"*24}
                    return {"status":"approved","owner_key":"c"*64,"worker_id":"d"*32}
                async def close(self): pass
            def interrupted(value):
                published.append(value);stop.set()
            with patch.object(worker,"HttpTransport",Transport):
                with self.assertRaisesRegex(ValueError,"resumed"):
                    await worker.initialise(args,state,progress=interrupted,stopped=stop)
                self.assertTrue((state/"pairing.json").exists())
                stop.clear()
                await worker.initialise(args,state,progress=published.append,stopped=stop)
            self.assertEqual(sum(method=="POST" for method,_,_ in calls),1)
            self.assertEqual(calls[-1][2],"b"*64)
            self.assertFalse((state/"pairing.json").exists())
            self.assertNotIn("b"*64,json.dumps(published))
            self.assertEqual(Path(json.loads((state/"worker.json").read_text())["workspaces"]["project"]["path"]).resolve(),workspace.resolve())


if __name__=="__main__": unittest.main()
