from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BackgroundRelayTests(unittest.TestCase):
    def test_exact_packaged_helper_yields_pushes_and_recovers_without_polling(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the code-mode relay contract")
        result = subprocess.run([node, str(ROOT / "tests/fixtures/background_relay_contract.cjs"),
                                 str(ROOT / "plugins/astra-antigravity/skills/delegate-to-antigravity/scripts/background-relay.js")],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"root_status_checks":0', result.stdout)


if __name__ == "__main__":
    unittest.main()
