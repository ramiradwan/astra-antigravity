import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from record_review import extract_release, matches_supplied_source, unchanged_inputs, verify_answer


class ReviewRecorderTests(unittest.TestCase):
    def test_source_references_resolve_to_the_actual_supplied_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "source.txt"
            source.write_text("12 joined; 8 completed", encoding="utf-8")
            (root / "other.txt").write_text("wrong source", encoding="utf-8")
            for location in ("source.txt", str(source), source.as_uri()):
                self.assertTrue(matches_supplied_source(location, root), location)
            self.assertFalse(matches_supplied_source("other.txt", root))
            self.assertFalse(matches_supplied_source("https://example.invalid/source.txt", root))
            self.assertFalse(matches_supplied_source("../source.txt", root))

    def test_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("../outside.py", "raise RuntimeError('outside')")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                extract_release(archive, digest, root / "extracted")
            self.assertFalse((root / "outside.py").exists())

    def test_answer_verification_does_not_execute_module_side_effects(self):
        verify_answer('def answer():\n    return 42\n\nif __name__ == "__main__":\n    assert answer() == 42\n')
        for source in ('def answer():\n    return 41\n',
                       'def answer():\n    return True\n',
                       'import os\ndef answer():\n    return 42\n',
                       'def answer():\n    return 42\nopen("outside", "w")\n'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                verify_answer(source)

    def test_modified_or_missing_input_cannot_pass_preservation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.txt"
            source.write_bytes(b"12 joined; 8 completed\n")
            inputs = {"source.txt": hashlib.sha256(source.read_bytes()).hexdigest()}
            self.assertTrue(unchanged_inputs(root, inputs))
            source.write_bytes(b"incorrect replacement\n")
            self.assertFalse(unchanged_inputs(root, inputs))
            source.unlink()
            self.assertFalse(unchanged_inputs(root, inputs))
