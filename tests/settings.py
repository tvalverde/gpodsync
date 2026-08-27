"""Settings for the test suite and the type checker.

The real settings refuse to start without an explicit environment, which is the
point of them. This module supplies one, so that neither pytest nor mypy has to
be run through a wrapper that exports six variables.

Everything here is a test value. Nothing in this file is a default the
application would ever adopt on its own.
"""

import os

os.environ.setdefault("GPODSYNC_ALLOWED_HOSTS", "testserver,gpodder.example.com")
os.environ.setdefault("GPODSYNC_SECRET_KEY", "not-a-real-key-for-tests-only")
os.environ.setdefault("GPODSYNC_DATA_DIR", "/tmp/gpodsync-tests")  # noqa: S108
# The test client speaks http, and a Secure cookie would never come back — the
# same trap a self-hoster on a LAN falls into, reproduced here by accident if
# this line were missing.
os.environ.setdefault("GPODSYNC_SESSION_COOKIE_SECURE", "false")

from gpodsync.settings import *  # noqa: F403

# Hashing is deliberately slow, and the suite creates accounts constantly.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
