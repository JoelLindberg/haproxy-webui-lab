"""URL routes for HAProxy Web UI."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    # Main Admin Dashboard
    path("admin/", views.dashboard_view, name="dashboard"),
    path("admin/edit/", views.legacy_edit_redirect, name="legacy_backend_edit"),
    path("admin/backends/create/", views.backend_create_view, name="backend_create"),
    path("admin/backends/sync/", views.backends_sync_view, name="backends_sync"),
    path("admin/backends/<str:backend_name>/", views.backend_edit_view, name="backend_edit"),
    path("admin/backends/<str:backend_name>/delete/", views.backend_delete_view, name="backend_delete"),
    # Server actions (HTMX + standard POST)
    path("admin/backends/<str:backend_name>/servers/create/", views.server_create_view, name="server_create"),
    path("admin/backends/<str:backend_name>/servers/state/", views.server_state_view, name="server_state"),
    path("admin/backends/<str:backend_name>/servers/rename/", views.server_rename_view, name="server_rename"),
    path("admin/backends/<str:backend_name>/servers/delete/", views.server_delete_view, name="server_delete"),
    # HTMX Live Partials
    path("admin/partials/live-stats/", views.partial_live_stats_view, name="partial_live_stats"),
    path("admin/partials/backends/<str:backend_name>/card/", views.partial_backend_card_view, name="partial_backend_card"),
    path("admin/partials/backends/<str:backend_name>/live/", views.partial_backend_live_view, name="partial_backend_live"),
    path("admin/partials/diagnostics/", views.partial_diagnostics_view, name="partial_diagnostics"),
    path("admin/partials/ping/", views.partial_ping_view, name="partial_ping"),
    path("admin/partials/metrics-summary/", views.partial_metrics_summary_view, name="partial_metrics_summary"),
    path("admin/partials/metrics-raw/", views.partial_metrics_raw_view, name="partial_metrics_raw"),
]
