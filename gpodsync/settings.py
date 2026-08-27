"""Django settings, driven entirely by the environment and failing closed.

Several ordinary hardening measures are ruled out by the client this server
exists to serve, so the choices here are narrower than they look. Each one that
is not obvious carries the reason, because the next person to read this file will
otherwise "fix" it and break every phone in the field.
"""

from pathlib import Path

from gpodsync.config import (
    ConfigurationError,
    env_bool,
    env_int,
    env_list,
    env_text,
    resolve_secret_key,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(env_text("GPODSYNC_DATA_DIR", "/data") or "/data")

DEBUG = env_bool("GPODSYNC_DEBUG", default=False)

SECRET_KEY = resolve_secret_key(DATA_DIR)

ALLOWED_HOSTS = env_list("GPODSYNC_ALLOWED_HOSTS")
if not ALLOWED_HOSTS and not DEBUG:
    raise ConfigurationError(
        "GPODSYNC_ALLOWED_HOSTS is required. Defaulting it to '*' would turn a "
        "misconfigured proxy into an open one."
    )
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*"]

# The loopback names are always allowed, whatever the operator configured.
#
# The container's own health check reaches /healthz/ over 127.0.0.1, and host
# validation happens before any view runs — so an operator who lists only their
# public hostname, which is the ordinary and correct thing to do, gets a container
# that serves real traffic perfectly and reports itself unhealthy forever, while
# writing a traceback into the log every thirty seconds. Under an orchestrator
# that is a container killed on a loop.
#
# Safe to allow: Django uses the host to build absolute URLs and redirects, and
# this application emits neither. Nothing here reflects the host back to anyone.
ALLOWED_HOSTS += [host for host in ("127.0.0.1", "localhost", "[::1]") if host not in ALLOWED_HOSTS]

ENABLE_ADMIN = env_bool("GPODSYNC_ENABLE_ADMIN", default=False)
TRACE_REQUESTS = env_bool("GPODSYNC_TRACE_REQUESTS", default=False)
BEHIND_PROXY = env_bool("GPODSYNC_BEHIND_PROXY", default=False)

# --- Applications ------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "axes",
    "gpodsync",
]

# The admin is opt-in. It is a second login surface to attack, it is the only
# browser context in which the shared session cookie makes the API's unavoidable
# CSRF exemption concrete, and account management does not need it: that is what
# the adduser command is for.
if ENABLE_ADMIN:
    INSTALLED_APPS += [
        "django.contrib.admin",
        "django.contrib.messages",
        "django.contrib.staticfiles",
    ]

MIDDLEWARE = [
    # Outermost, so its correlation id covers everything below it and its timing
    # includes every other middleware rather than only the view.
    "gpodsync.middleware.RequestTracing",
    # Kept for its headers only. It can also redirect, and every redirect setting
    # it offers is switched off below.
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Header-only, no redirects. The API serves JSON and has nothing to frame,
    # but the admin does when it is enabled, and this costs nothing either way.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

X_FRAME_OPTIONS = "DENY"

if ENABLE_ADMIN:
    MIDDLEWARE.insert(3, "django.middleware.csrf.CsrfViewMiddleware")
    MIDDLEWARE.insert(5, "django.contrib.messages.middleware.MessageMiddleware")
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "gpodsync.urls"
WSGI_APPLICATION = None
ASGI_APPLICATION = "gpodsync.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

# --- Database ----------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            # WAL lets a reader and the writer proceed at once, which is the whole
            # reason SQLite is adequate here.
            # NORMAL rather than FULL: with WAL this stays corruption-safe across an
            # unclean container stop, and risks only the last un-checkpointed
            # transactions if the host itself loses power. For listening history
            # that is the right trade; for money it would not be.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "timeout": env_int("GPODSYNC_DB_TIMEOUT", default=20),
            # Take the write lock when the transaction opens rather than when it
            # first writes. Deferred mode turns a concurrent write into an
            # immediate "database is locked" that no retry can rescue, because the
            # transaction has already read.
            "transaction_mode": "IMMEDIATE",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication ----------------------------------------------------------

# Axes must come first: it wraps the backend behind it, and a backend listed
# before it would authenticate without ever being counted.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AXES_ENABLED = env_bool("GPODSYNC_LOCKOUT_ENABLED", default=True)
AXES_FAILURE_LIMIT = env_int("GPODSYNC_LOCKOUT_FAILURES", default=10)
AXES_COOLOFF_TIME = env_int("GPODSYNC_LOCKOUT_MINUTES", default=15) / 60
# Username *and* address together. Locking on the username alone would let anyone
# who knows it lock the account's real owner out from anywhere — the defence
# becomes the attack, and with a single account the username is not a secret.
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_CALLABLE = "gpodsync.api.base.locked_out"

# The client logs in at the start of every sync, so recording each success
# would grow a table forever on a server nobody prunes — and nothing here ever
# reads it. The failed-attempts table, which is the actual defence, is separate
# and unaffected.
AXES_DISABLE_ACCESS_LOG = True

# The banner axes prints from every AppConfig.ready() lands once per manage.py
# command in the entrypoint — four times per container boot. The lockout mode
# it announces is pinned by the settings above and by tests; the repetition
# only makes real startup problems harder to spot.
AXES_VERBOSE = False

# How many proxies are genuinely in front of this container. Zero means the
# socket address is the client, and X-Forwarded-For is ignored — which is what
# stops anyone from claiming a fresh address per attempt on a direct deployment.
TRUSTED_PROXY_HOPS = env_int("GPODSYNC_TRUSTED_PROXY_HOPS", default=1) if BEHIND_PROXY else 0

# Ours, not django-ipware's. Axes reaches for ipware and, when it is not
# installed, falls back to REMOTE_ADDR while ignoring every proxy setting it was
# given — so behind a proxy the lockout key would silently become
# (username, the proxy's address) and one attacker would lock out everybody.
AXES_CLIENT_IP_CALLABLE = "gpodsync.api.base.axes_client_ip"

# --- Sessions ----------------------------------------------------------------

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "sessionid"

# Never set. AntennaPod's cookie jar discards a cookie whose Domain does not name
# the request host exactly, and the session cookie is the only authentication it
# sends after login — so setting this breaks every deployment silently, in a way
# that reads as a wrong password.
SESSION_COOKIE_DOMAIN = None

# Configurable, and that is not laziness. Java will not return a Secure cookie
# over plain http, so leaving this on for someone serving over http on their LAN
# produces a successful login followed by nothing that works.
SESSION_COOKIE_SECURE = env_bool("GPODSYNC_SESSION_COOKIE_SECURE", default=True)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# The client logs in at the start of every sync, so a short life costs nothing
# and shortens the window in which a stolen cookie is worth anything.
SESSION_COOKIE_AGE = env_int("GPODSYNC_SESSION_DAYS", default=7) * 24 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = False

# Follows the session cookie, for the same reason and with the same escape
# hatch. It exists only when the admin is enabled, which is the only browser
# context here — and with the admin on, Django's deployment check fails without
# it, so this was a configuration that could not pass its own gate.
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# --- Requests ----------------------------------------------------------------

# A real batch of thirty actions is about ten kilobytes.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("GPODSYNC_MAX_REQUEST_BYTES", default=1_048_576)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100

# Both of these redirect, and no endpoint under /api/ may. A same-origin redirect
# makes OkHttp rewrite the login POST into a GET; a cross-scheme one strips the
# credentials. Both reach the user as "wrong username or password".
APPEND_SLASH = False
SECURE_SSL_REDIRECT = False

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# HSTS belongs to whatever terminates TLS. Setting it here would poison the
# browser of anyone who visited a LAN deployment served over http.
SECURE_HSTS_SECONDS = 0

# Three of Django's deployment warnings are deliberate here, and silencing them
# without saying why would be indistinguishable from not having read them.
#
# W003 — CsrfViewMiddleware. AntennaPod sends no CSRF token, so the API cannot
#        require one. The compensating controls are a SameSite session cookie and
#        refusing any request carrying a browser's Origin or Sec-Fetch-Site
#        marker; see gpodsync/api/base.py. The middleware is still added when the
#        admin is enabled, which is the only browser context here.
# W004 — SECURE_HSTS_SECONDS. HSTS belongs to whatever terminates TLS. Setting it
#        here would poison the browser of anyone who visited a LAN deployment
#        served over plain http, which is a supported configuration.
# W008 — SECURE_SSL_REDIRECT. No endpoint under /api/ may return 3xx: a redirect
#        makes OkHttp rewrite the login POST into a GET, or strips the
#        credentials outright, and reaches the user as a wrong password.
SILENCED_SYSTEM_CHECKS = ["security.W003", "security.W004", "security.W008"]

if BEHIND_PROXY:
    # Only when a proxy we control is actually in front. Trusting this header
    # unconditionally would let anyone claim any address and walk past the
    # lockout that address is keyed on.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

# --- Miscellany --------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = DATA_DIR / "static"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

TRACE_BODY_LIMIT = env_int("GPODSYNC_TRACE_BODY_LIMIT", default=8192)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "gpodsync.logging_config.JsonFormatter"}},
    "filters": {"drop_healthy_probes": {"()": "gpodsync.logging_config.HealthProbeFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["drop_healthy_probes"],
        }
    },
    "root": {"handlers": ["console"], "level": env_text("GPODSYNC_LOG_LEVEL", "INFO")},
}
