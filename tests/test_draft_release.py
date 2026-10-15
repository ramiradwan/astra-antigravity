import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_draft_release import DraftError, core_artifacts, prepare

COMMIT = "a" * 40


def fixtures(root):
    for prefix, checksum in (("astra-antigravity-0.9.1", "SHA256SUMS"),
                             ("astra-antigravity-0.9.1-bundled-runtime", "SHA256SUMS.runtime")):
        with zipfile.ZipFile(root/(prefix+".zip"), "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "version": "0.9.1"}))
        (root/(prefix+".inventory.json")).write_text("{}\n", encoding="utf-8")
        (root/checksum).write_text("".join(
            f"{hashlib.sha256((root/name).read_bytes()).hexdigest()}  {name}\n"
            for name in (prefix+".zip", prefix+".inventory.json")), encoding="utf-8")
    notes = root/"notes.md"
    notes.write_text("New draft notes\n", encoding="utf-8")
    return notes


class DraftReleaseTests(unittest.TestCase):
    def invoke(self, root, api, upload, apply=True):
        return prepare("owner/repo", COMMIT, "0.9.1", root, root/"notes.md",
                       apply=apply, api=api, upload=upload)

    def test_published_or_ambiguous_tags_never_mutate(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); fixtures(root)
            for releases in ([{"id": 1, "tag_name": "v0.9.1", "draft": False}],
                             [{"id": i, "tag_name": "v0.9.1", "draft": True} for i in (1, 2)]):
                api=Mock(return_value=[releases]); upload=Mock()
                with self.assertRaises(DraftError):
                    self.invoke(root, api, upload)
                self.assertEqual(api.call_count, 1)
                upload.assert_not_called()

    def test_unchanged_repeat_preserves_reviewed_notes_and_extra_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); fixtures(root)
            release={"id": 42, "tag_name": "v0.9.1", "draft": True,
                     "target_commitish": COMMIT, "body": "Reviewed notes\n",
                     "assets": [{"name": name, "digest": "sha256:"+digest}
                                for name,digest in core_artifacts(root,"0.9.1").items()]
                               + [{"name": "review-video.mp4", "digest": "sha256:media"}]}
            api=Mock(side_effect=[[ [release] ], release]); upload=Mock()
            result=self.invoke(root,api,upload)
            self.assertEqual(result["core_assets_to_upload"], [])
            self.assertEqual(result["release_id"],42)
            self.assertEqual(release["body"],"Reviewed notes\n")
            self.assertEqual(len(release["assets"]),7)
            self.assertTrue(all(call.kwargs.get("method","GET")=="GET" for call in api.call_args_list))
            upload.assert_not_called()

    def test_changed_checksums_upload_only_explicit_core_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); fixtures(root)
            (root/"private-history.log").write_text("Never upload this",encoding="utf-8")
            release={"id":42,"tag_name":"v0.9.1","draft":True,"target_commitish":COMMIT,"assets":[]}
            api=Mock(side_effect=[[ [release] ],release,release]); upload=Mock()
            self.invoke(root,api,upload)
            paths=upload.call_args.args[2]
            self.assertEqual({path.name for path in paths},set(core_artifacts(root,"0.9.1")))
            self.assertNotIn("private-history.log",{path.name for path in paths})

    def test_bad_checksum_stops_before_any_github_operation(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); fixtures(root)
            (root/"astra-antigravity-0.9.1.zip").write_bytes(b"changed after validation")
            api=Mock(); upload=Mock()
            with self.assertRaises(DraftError):
                self.invoke(root,api,upload)
            api.assert_not_called(); upload.assert_not_called()

    def test_new_release_is_draft_with_the_exact_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); fixtures(root)
            release={"id":42,"tag_name":"v0.9.1","draft":True,"target_commitish":COMMIT,"assets":[]}
            api=Mock(side_effect=[[],release,release]); upload=Mock()
            result=self.invoke(root,api,upload)
            payload=api.call_args_list[1].kwargs["body"]
            self.assertIs(payload["draft"],True)
            self.assertEqual(payload["target_commitish"],COMMIT)
            self.assertTrue(result["draft"])

    def test_target_update_explicitly_preserves_the_draft_tag(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures(root)
            release = {"id": 42, "tag_name": "v0.9.1", "draft": True,
                       "target_commitish": "b" * 40, "assets": []}
            updated = {**release, "target_commitish": COMMIT}
            api = Mock(side_effect=[[[release]], release, updated, updated])
            self.invoke(root, api, Mock())
            patch = api.call_args_list[2]
            self.assertEqual(patch.kwargs["method"], "PATCH")
            self.assertEqual(patch.kwargs["body"], {"tag_name": "v0.9.1", "target_commitish": COMMIT})
