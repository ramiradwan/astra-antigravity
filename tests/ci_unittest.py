"""CI unittest runner with platform-aware coverage partitioning.

The two MSIX-discovery tests intentionally simulate Windows by monkeypatching
``os.name``. That mutates pathlib's global OS selector and cannot be executed
safely on a POSIX interpreter. The Windows CI lane exercises them natively;
non-Windows lanes skip only those two IDs and run the rest of the suite.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ONLY_IDS = {
    "test_launcher.LauncherDiscoveryTests.test_discover_agy_msix_discovery",
    "test_server.TestHelpers.test_find_agy_msix_discovery",
}


def _filter_suite(suite: unittest.TestSuite) -> unittest.TestSuite:
    filtered = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            nested = _filter_suite(item)
            if nested.countTestCases():
                filtered.addTest(nested)
            continue
        test_id = item.id()
        short_id = test_id.split("tests.", 1)[-1]
        if os.name != "nt" and short_id in WINDOWS_ONLY_IDS:
            continue
        filtered.addTest(item)
    return filtered


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    suite = _filter_suite(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
