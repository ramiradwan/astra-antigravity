from pathlib import Path
import subprocess
import sys
import time
import unittest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN))
from command_probes import ProbeOutputLimitError, run_probe


class CommandProbeTests(unittest.TestCase):
    def test_probe_sees_eof_instead_of_inheriting_input(self):
        result = run_probe([sys.executable, "-c",
            "import sys; assert sys.stdin.buffer.read() == b''; print('closed')"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "closed")

    def test_timeout_does_not_wait_for_descendant_output_pipes(self):
        code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); "
            "print('child holds output handles',flush=True); time.sleep(15)"
        )
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_probe([sys.executable, "-c", code], timeout=0.3)
        self.assertLess(time.monotonic() - start, 5)

    def test_oversized_stdout_and_stderr_fail_closed(self):
        with self.assertRaises(ProbeOutputLimitError):
            run_probe([sys.executable, "-c",
                "import sys; print('x'*40); print('y'*40,file=sys.stderr)"],
                max_output_bytes=64)

    def test_nonzero_exit_is_preserved(self):
        result = run_probe([sys.executable, "-c",
            "import sys; print('not ready',file=sys.stderr); sys.exit(7)"])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr.strip(), "not ready")


class ReportTerminationTests(unittest.TestCase):
    def test_minimum_string_and_impossible_metadata_fail_within_deadline(self):
        code = """import sys
sys.path.insert(0, sys.argv[1])
from communication import bound_report
for value, budget in [({'summary':'x'*25},30),
                      ({str(i):'x'*26 for i in range(300)},8000)]:
    try:
        bound_report(value,max_budget=budget)
    except ValueError as exc:
        assert 'cannot fit' in str(exc)
    else:
        raise AssertionError('Impossible metadata unexpectedly fit')
print('terminated')
"""
        # Run in an isolated process so the former infinite loop cannot hang CI.
        result = subprocess.run([sys.executable, "-c", code, str(PLUGIN)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "terminated")


if __name__ == "__main__":
    unittest.main()
