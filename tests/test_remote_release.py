import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "scripts"))
SPEC=importlib.util.spec_from_file_location("remote_release",ROOT / "scripts/build_remote_release.py")
release=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SCOPES=["workspaces:read","tasks:read","tasks:write","computers:manage","account:delete"]


def expected_server(url):
    return {"type":"streamable-http","url":url,"extensions":{"com.openai":{
        "auth":{"type":"oauth","defaultScopes":SCOPES}}}}


class RemoteReleaseTests(unittest.TestCase):
    def test_github_catalog_offers_the_same_https_onboarding_package(self):
        catalog=json.loads((ROOT/".agents/plugins/marketplace.json").read_text())
        entry=next(item for item in catalog["plugins"] if item["name"]=="astra-antigravity-remote")
        plugin=ROOT/entry["source"]["path"]
        config=json.loads((plugin/"mcp.json").read_text())
        self.assertEqual(entry["policy"]["authentication"],"ON_INSTALL")
        self.assertEqual(config["mcpServers"]["astra_relay"],expected_server("https://astra-antigravity-relay.dipsys.workers.dev/mcp"))
        manifest=json.loads((plugin/"plugin.json").read_text())
        self.assertTrue((plugin/manifest["extensions"]["com.openai"]["onboardingSkill"]).is_file())

    def test_refuses_stdio_local_and_placeholder_endpoints(self):
        for url in ("http://localhost:8787/mcp", "https://10.0.0.1/mcp", "https://127.0.0.1/mcp", "https://YOUR-RELAY-HOST/mcp",
                    "https://relay.example.test/mcp", "https://public-host/path", "https://user:password@public-host/mcp"):
            with self.assertRaises(release.ReleaseError):
                release.endpoint(url)

    def test_reproducible_remote_root_package_has_no_local_executor(self):
        # Only this offline test bypasses live metadata verification. The release
        # CLI always verifies the configured public service before building.
        with tempfile.TemporaryDirectory() as directory, patch.object(release,"verify_endpoint") as verification:
            first=release.build(Path(directory)/"first","https://public-relay.workers.dev/mcp")
            second=release.build(Path(directory)/"second","https://public-relay.workers.dev/mcp")
            self.assertEqual(first["sha256"],second["sha256"])
            with zipfile.ZipFile(first["archive"]) as archive:
                names=archive.namelist()
                self.assertIn("plugin.json",names)
                self.assertIn("mcp.json",names)
                self.assertNotIn(".mcp.json",names)
                self.assertFalse(any("planning" in name or "submission" in name or ".codex-plugin" in name for name in names))
                manifest=json.loads(archive.read("plugin.json"))
                self.assertEqual(Path(first["archive"]).name,"astra-antigravity-remote-"+manifest["version"]+".zip")
                verification.assert_called_with("https://public-relay.workers.dev/mcp",manifest["version"])
                self.assertFalse(manifest["extensions"]["com.openai"]["requires_local_executor"])
                mcp=json.loads(archive.read("mcp.json"))["mcpServers"]["astra_relay"]
                self.assertEqual(mcp,expected_server("https://public-relay.workers.dev/mcp"))
                source=json.loads((release.PLUGIN/"mcp.json").read_text())["mcpServers"]["astra_relay"]
                self.assertEqual(mcp["extensions"],source["extensions"])
                delegation=archive.read("skills/delegate-to-worker/SKILL.md").decode()
                self.assertIn("`delegate_task`",delegation)
                self.assertIn("`continue_task`",delegation)
                self.assertNotIn("delegate_via_luna",delegation)
                self.assertNotIn("continue_via_luna",delegation)

    def test_refuses_missing_or_changed_scope_defaults_and_embedded_credentials_before_network(self):
        entries=json.loads((ROOT/"remote-release-files.json").read_text())
        files=release.read_package(release.PLUGIN,entries)
        config=json.loads(files["mcp.json"])
        changes=(
            lambda server: server.pop("extensions"),
            lambda server: server["extensions"]["com.openai"]["auth"].update(defaultScopes=SCOPES[:3]),
            lambda server: server["extensions"]["com.openai"]["auth"].update(baseScopes=SCOPES),
            lambda server: server["extensions"]["com.openai"]["auth"].update(clientSecret="synthetic-do-not-package"),
            lambda server: server.update(headers={"Authorization":"Bearer synthetic-do-not-package"}),
        )
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                changed=json.loads(json.dumps(config))
                change(changed["mcpServers"]["astra_relay"])
                altered={**files,"mcp.json":json.dumps(changed).encode()}
                with patch.object(release,"read_package",return_value=altered),patch.object(release,"verify_endpoint") as verification:
                    with self.assertRaises(release.ReleaseError) as raised:
                        release.build(directory,"https://public-relay.workers.dev/mcp")
                    self.assertNotIn("synthetic-do-not-package",str(raised.exception))
                    verification.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()),[])

    def test_failed_live_verification_creates_no_archive(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release,"verify_endpoint",side_effect=release.ReleaseError("Unavailable")):
            with self.assertRaises(release.ReleaseError):
                release.build(directory,"https://public-relay.workers.dev/mcp")
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_live_metadata_refuses_missing_authentication_or_local_deployment(self):
        origin="https://public-relay.workers.dev"
        health={"version":"0.14.1","cost_policy":"free-only","deployment_mode":"verified-free",
                "authentication_configured":True}
        discovery={"resource":origin+"/mcp","authorization_servers":[origin]}
        for changes in ({"authentication_configured":False},{"authentication_configured":"true"},
                        {"authentication_configured":None},{"deployment_mode":"local"},{"version":"0.12.1"}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                opener=Mock()
                opener.open.side_effect=[BytesIO(json.dumps({**health,**changes}).encode()),
                                         BytesIO(json.dumps(discovery).encode())]
                with patch.object(release.urllib.request,"build_opener",return_value=opener):
                    with self.assertRaises(release.ReleaseError):
                        release.build(directory,origin+"/mcp")
                self.assertEqual(list(Path(directory).iterdir()),[])

    def test_live_metadata_accepts_configured_free_deployment(self):
        origin="https://public-relay.workers.dev"
        opener=Mock()
        opener.open.side_effect=[BytesIO(json.dumps({"version":"0.14.1","cost_policy":"free-only",
            "deployment_mode":"verified-free","authentication_configured":True}).encode()),
            BytesIO(json.dumps({"resource":origin+"/mcp","authorization_servers":[origin]}).encode())]
        with patch.object(release.urllib.request,"build_opener",return_value=opener):
            release.verify_endpoint(origin+"/mcp","0.14.1")
        for call in opener.open.call_args_list:
            request=call.args[0]
            self.assertEqual(request.get_header("Accept"),"application/json")
            self.assertEqual(request.get_header("User-agent"),"Astra-Antigravity-Release/0.14.1")


if __name__ == "__main__":
    unittest.main()
