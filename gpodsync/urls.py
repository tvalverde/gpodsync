"""URL routing.

Note the absence of a trailing-slash form for anything under `/api/`. Django's
APPEND_SLASH is off and these paths are matched exactly, because a redirect here
reaches the user as a wrong password.
"""

from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, path
from django.views.decorators.http import require_safe

from gpodsync.api import views

# Read once at import, served verbatim: the page is static by design, and going
# through the template engine would add a rendering pass that can only ever be
# a way for dynamic content to creep into a page that must stay generic.
HOME_PAGE = (Path(__file__).parent / "templates" / "home.html").read_text(encoding="utf-8")


def healthz(request: HttpRequest) -> HttpResponse:
    """Liveness only. Deliberately says nothing about the database.

    A health check that reports internals is a reconnaissance endpoint, and this
    one answers before authentication.
    """
    return HttpResponse("ok\n", content_type="text/plain; charset=utf-8")


@require_safe
def home(request: HttpRequest) -> HttpResponse:
    """The front door, for the person who types the hostname into a browser.

    Everything on it is generic — the host it shows is read client-side from
    location.host — so the same bytes serve every deployment.
    """
    return HttpResponse(HOME_PAGE, content_type="text/html; charset=utf-8")


urlpatterns: list[URLPattern | URLResolver] = [
    path("", home),
    path("healthz/", healthz),
    path("api/2/auth/<str:username>/login.json", views.LoginView.as_view()),
    path("api/2/devices/<str:username>.json", views.DeviceListView.as_view()),
    path("api/2/devices/<str:username>/<str:device>.json", views.DeviceConfigView.as_view()),
    path(
        "api/2/subscriptions/<str:username>/<str:device>.json",
        views.SubscriptionsView.as_view(),
    ),
    path("api/2/episodes/<str:username>.json", views.EpisodeActionsView.as_view()),
]

if settings.ENABLE_ADMIN:
    from django.contrib import admin

    urlpatterns += [path("admin/", admin.site.urls)]
