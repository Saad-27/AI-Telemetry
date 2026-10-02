import subprocess
import sys
from importlib import metadata

import yourpkg

# Run in a fresh interpreter so other tests or plugins cannot hide side effects.
_IMPORT_PROBE = """
import sys, threading
threads = threading.active_count()
modules = set(sys.modules)
import yourpkg
new = sorted(m for m in set(sys.modules) - modules if not m.startswith("yourpkg"))
assert threading.active_count() == threads, "import started a thread"
assert not new, f"import pulled in extra modules: {new}"
"""


def test_import_has_no_side_effects() -> None:
    subprocess.run([sys.executable, "-c", _IMPORT_PROBE], check=True)


def test_no_runtime_dependencies() -> None:
    assert not metadata.requires("yourpkg")


def test_version_matches_metadata() -> None:
    assert yourpkg.__version__ == metadata.version("yourpkg")
