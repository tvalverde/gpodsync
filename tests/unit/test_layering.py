"""The domain layer's independence from Django, enforced rather than intended.

Everything else about that boundary is a convention someone can breach without
noticing. This notices.
"""

import subprocess
import sys

import pytest

# Marked as a unit test despite spawning an interpreter: it belongs to the layer
# whose rule it enforces, and a fresh process is the only way to ask "what did
# importing this actually pull in" without pytest's own imports polluting the
# answer.
pytestmark = pytest.mark.unit


def test_domain_layer_imports_no_django():
    import gpodsync.domain  # noqa: F401  — also gives the coverage run something to measure

    probe = (
        "import importlib, sys; "
        "importlib.import_module('gpodsync.domain'); "
        "print(','.join(m for m in sys.modules if m == 'django' or m.startswith('django.')))"
    )
    # S603: the executable is this interpreter and every argument is a literal
    # defined above. There is no untrusted input here to check.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )

    leaked = result.stdout.strip()
    assert leaked == "", (
        f"gpodsync.domain pulled in Django: {leaked}. The 100% branch-coverage gate "
        f"on this layer is only reachable because it has no framework in it."
    )
