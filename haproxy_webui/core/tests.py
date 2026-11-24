"""Tests for authentication, HAProxy integration views, live stats, and Prometheus parsing."""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .haproxy_client import HAProxyError, get_live_overview_stats, get_prometheus_metrics
from .models import Backend

User = get_user_model()


class AuthenticationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="admin",
            email="admin@haproxy.local",
            password="secret-password-123",
        )

    def test_unauthenticated_redirects_to_login(self):
        res = self.client.get(reverse("dashboard"))
        self.assertRedirects(res, f"{reverse('login')}?next={reverse('dashboard')}")

    def test_login_with_email_succeeds(self):
        res = self.client.post(
            reverse("login"),
            {"identifier": "admin@haproxy.local", "password": "secret-password-123"},
        )
        self.assertRedirects(res, reverse("dashboard"))

    def test_login_with_username_succeeds(self):
        res = self.client.post(
            reverse("login"),
            {"identifier": "admin", "password": "secret-password-123"},
        )
        self.assertRedirects(res, reverse("dashboard"))

    def test_logout_clears_session(self):
        self.client.force_login(self.user)
        res = self.client.post(reverse("logout"))
        self.assertRedirects(res, reverse("login"))


class SeedCommandTests(TestCase):
    @patch("core.management.commands.seed.list_configured_backends")
    def test_seed_creates_admin_and_syncs_backends(self, mock_list):
        mock_list.return_value = [
            {"name": "test-app_be", "mode": "http", "algorithm": "roundrobin"},
            {"name": "haproxy-webui-db_be", "mode": "tcp", "algorithm": "roundrobin"},
        ]
        out = StringIO()
        call_command("seed", stdout=out)
        self.assertTrue(User.objects.filter(username="admin", is_superuser=True).exists())
        self.assertEqual(Backend.objects.count(), 2)
        self.assertEqual(Backend.objects.get(name="haproxy-webui-db_be").mode, "tcp")

    @patch("core.management.commands.seed.list_configured_backends")
    def test_seed_fallback_when_haproxy_unreachable(self, mock_list):
        mock_list.side_effect = HAProxyError("Connection refused", status_code=502)
        out = StringIO()
        call_command("seed", stdout=out)
        self.assertEqual(Backend.objects.count(), 1)


class BackendAndServerManagementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin",
            email="admin@haproxy.local",
            password="secret-password-123",
        )
        self.client.force_login(self.user)
        self.backend = Backend.objects.create(
            name="test-app_be",
            mode="http",
            balance_algorithm="roundrobin",
        )

    @patch("core.views.create_backend")
    def test_htmx_create_backend(self, mock_create):
        mock_create.return_value = {"name": "api_be"}
        res = self.client.post(
            reverse("backend_create"),
            {"name": "api_be", "mode": "http", "balance_algorithm": "leastconn"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Backend.objects.filter(name="api_be", balance_algorithm="leastconn").exists())
        self.assertEqual(res["HX-Trigger"], "backendsChanged")

    @patch("core.views.delete_backend")
    def test_htmx_delete_backend_requires_exact_confirmation(self, mock_delete):
        with patch("core.views._build_backend_workspace_context", return_value={"backend": self.backend, "servers": [], "create_server_form": MagicMock()}):
            bad_res = self.client.post(
                reverse("backend_delete", args=[self.backend.name]),
                {"confirm_name": "wrong"},
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(bad_res.status_code, 200)
            self.assertTrue(Backend.objects.filter(name="test-app_be").exists())
            mock_delete.assert_not_called()

        ok_res = self.client.post(
            reverse("backend_delete", args=[self.backend.name]),
            {"confirm_name": "test-app_be"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(ok_res.status_code, 204)
        self.assertEqual(ok_res["HX-Redirect"], reverse("dashboard"))
        self.assertFalse(Backend.objects.filter(name="test-app_be").exists())

    @patch("core.views._build_backend_workspace_context")
    @patch("core.views.create_server")
    @patch("core.views.set_server_state")
    @patch("core.views.rename_server")
    @patch("core.views.delete_server")
    def test_server_lifecycle_actions(
        self,
        mock_del_srv,
        mock_ren_srv,
        mock_state_srv,
        mock_add_srv,
        mock_ctx,
    ):
        mock_ctx.return_value = {"backend": self.backend, "servers": [], "create_server_form": MagicMock()}

        res_add = self.client.post(
            reverse("server_create", args=["test-app_be"]),
            {"name": "app3", "address": "10.0.0.3", "port": 8080, "check": "enabled"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(res_add.status_code, 200)
        mock_add_srv.assert_called_once_with(
            backend_name="test-app_be",
            server_name="app3",
            address="10.0.0.3",
            port=8080,
            check=True,
        )

        res_state = self.client.post(
            reverse("server_state", args=["test-app_be"]),
            {"server_name": "app3", "admin_state": "drain"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(res_state.status_code, 200)
        mock_state_srv.assert_called_once_with("test-app_be", "app3", "drain")

        res_rename = self.client.post(
            reverse("server_rename", args=["test-app_be"]),
            {"old_name": "app3", "new_name": "app3-blue"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(res_rename.status_code, 200)
        mock_ren_srv.assert_called_once_with("test-app_be", "app3", "app3-blue")

        res_del = self.client.post(
            reverse("server_delete", args=["test-app_be"]),
            {"server_name": "app3-blue"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(res_del.status_code, 200)
        mock_del_srv.assert_called_once_with("test-app_be", "app3-blue")


class LiveStatsAndMetricsParsingTests(TestCase):
    @patch("core.haproxy_client.call_dataplane")
    def test_get_live_overview_stats_aggregation(self, mock_call):
        fake_res = MagicMock()
        fake_res.is_success = True
        fake_res.text = '{"stats": []}'
        fake_res.json.return_value = {
            "stats": [
                {
                    "type": "backend",
                    "name": "test-app_be",
                    "stats": {
                        "status": "UP",
                        "scur": 12,
                        "rate": 45,
                        "rate_max": 90,
                        "qcur": 2,
                        "stot": 1500,
                        "bin": 2048,
                        "bout": 1048576,
                        "hrsp_2xx": 1490,
                        "hrsp_4xx": 8,
                        "hrsp_5xx": 2,
                    },
                },
                {
                    "type": "server",
                    "name": "app1",
                    "backend_name": "test-app_be",
                    "stats": {
                        "status": "UP",
                        "scur": 7,
                        "smax": 15,
                        "rate": 25,
                        "qcur": 0,
                        "stot": 800,
                        "bin": 1024,
                        "bout": 524288,
                        "rtime": 4,
                    },
                },
                {
                    "type": "server",
                    "name": "app2",
                    "backend_name": "test-app_be",
                    "stats": {
                        "status": "MAINT",
                        "scur": 0,
                        "smax": 10,
                        "rate": 0,
                        "qcur": 0,
                        "stot": 700,
                        "bin": 1024,
                        "bout": 524288,
                        "rtime": 3,
                    },
                },
            ]
        }
        mock_call.return_value = fake_res

        stats = get_live_overview_stats()
        self.assertEqual(stats["backends_total"], 1)
        self.assertEqual(stats["backends_up"], 1)
        self.assertEqual(stats["servers_total"], 2)
        self.assertEqual(stats["servers_up"], 1)
        self.assertEqual(stats["servers_maint"], 1)
        self.assertEqual(stats["current_sessions"], 12)
        self.assertEqual(stats["session_rate"], 45)
        self.assertEqual(stats["queued_connections"], 2)
        self.assertEqual(stats["bytes_out_human"], "1.0 MB")
        self.assertEqual(len(stats["servers"]), 2)

    @patch("core.haproxy_client.httpx.get")
    def test_prometheus_metrics_parser(self, mock_get):
        sample_prom = """# HELP haproxy_frontend_current_sessions Current number of active sessions.
# TYPE haproxy_frontend_current_sessions gauge
haproxy_frontend_current_sessions{proxy="http"} 5
# HELP haproxy_frontend_sessions_total Total number of sessions.
# TYPE haproxy_frontend_sessions_total counter
haproxy_frontend_sessions_total{proxy="http"} 240
# HELP haproxy_backend_current_sessions Current number of active sessions.
# TYPE haproxy_backend_current_sessions gauge
haproxy_backend_current_sessions{proxy="test-app_be"} 4
"""
        fake_res = MagicMock()
        fake_res.is_success = True
        fake_res.text = sample_prom
        mock_get.return_value = fake_res

        parsed = get_prometheus_metrics()
        self.assertEqual(parsed["metrics_count"], 3)
        self.assertEqual(len(parsed["proxy_rows"]), 2)
        fe_row = next(r for r in parsed["proxy_rows"] if r["type"] == "frontend")
        self.assertEqual(fe_row["name"], "http")
        self.assertEqual(fe_row["current_sessions"], 5)
        self.assertEqual(fe_row["total_sessions"], 240)


class TemplateRenderingTests(TestCase):
    """Verify every page and HTMX partial template renders without template errors."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin",
            email="admin@haproxy.local",
            password="secret-password-123",
        )
        self.client.force_login(self.user)
        self.backend = Backend.objects.create(
            name="test-app_be",
            mode="http",
            balance_algorithm="roundrobin",
        )

    @patch("core.views.get_live_overview_stats")
    @patch("core.views.get_backend_details")
    @patch("core.views.get_backend_servers_enriched")
    @patch("core.views.get_diagnostics")
    @patch("core.views.ping_dataplane")
    @patch("core.views.get_prometheus_metrics")
    def test_all_pages_and_partials_render(
        self,
        mock_prom,
        mock_ping,
        mock_diag,
        mock_servers,
        mock_details,
        mock_live,
    ):
        mock_live.return_value = {
            "updated_at": "12:00:00 UTC",
            "backends_total": 1,
            "backends_up": 1,
            "servers_total": 1,
            "servers_up": 1,
            "servers_down": 0,
            "servers_maint": 0,
            "servers_drain": 0,
            "current_sessions": 4,
            "session_rate": 12,
            "max_session_rate": 30,
            "queued_connections": 0,
            "total_sessions": 500,
            "bytes_in": 1024,
            "bytes_out": 2048,
            "bytes_in_human": "1.0 KB",
            "bytes_out_human": "2.0 KB",
            "http_2xx": 495,
            "http_4xx": 5,
            "http_5xx": 0,
            "errors_total": 0,
            "servers": [
                {
                    "backend": "test-app_be",
                    "name": "app1",
                    "raw_status": "UP",
                    "operational_state": "up",
                    "admin_state": "ready",
                    "current_sessions": 4,
                    "max_sessions": 10,
                    "session_rate": 12,
                    "queued_connections": 0,
                    "total_sessions": 500,
                    "bytes_in_human": "1.0 KB",
                    "bytes_out_human": "2.0 KB",
                    "response_time_ms": 2,
                    "check_status": "L4OK",
                }
            ],
        }
        mock_details.return_value = {
            "name": "test-app_be",
            "exists_in_haproxy": True,
            "algorithm": "roundrobin",
            "mode": "http",
            "status": "UP",
            "current_sessions": 4,
            "max_sessions": 10,
            "queued_connections": 0,
            "total_sessions": 500,
            "session_rate": 12,
            "bytes_in": 1024,
            "bytes_out": 2048,
            "bytes_out_human": "2.0 KB",
        }
        mock_servers.return_value = [
            {
                "name": "app1",
                "address": "http-echo1",
                "port": 5678,
                "check": "enabled",
                "operational_state": "up",
                "operational_label": "UP",
                "admin_state": "ready",
                "admin_label": "READY",
                "current_sessions": 4,
                "max_sessions": 10,
                "total_sessions": 500,
                "session_rate": 12,
                "queued_connections": 0,
                "bytes_out": 2048,
                "bytes_out_human": "2.0 KB",
                "response_time_ms": 2,
                "check_status": "L4OK",
            }
        ]
        mock_diag.return_value = {
            "api_version": "v3.0.4",
            "build_date": "2026-01-01",
            "haproxy_version": "3.0.5",
            "health": "up",
            "haproxy_pid": 42,
            "haproxy_uptime": "1d 2h 3m 4s",
            "haproxy_processes": 1,
            "total_bytes_out": 1048576,
            "total_bytes_out_human": "1.0 MB",
        }
        mock_ping.return_value = {
            "ok": True,
            "status_code": 200,
            "latency_ms": 3.4,
            "timestamp": "12:00:00 UTC",
            "data": {"haproxy": "up"},
        }
        mock_prom.return_value = {
            "metrics_count": 1,
            "raw_length": 512,
            "raw_kb": "0.5",
            "proxy_rows": [
                {
                    "name": "test-app_be",
                    "type": "backend",
                    "current_sessions": 4,
                    "max_sessions": 10,
                    "total_sessions": 500,
                    "session_rate": "12.00",
                }
            ],
            "families": [
                {
                    "name": "haproxy_backend_current_sessions",
                    "type": "gauge",
                    "help": "Current sessions",
                    "sample_count": 1,
                    "samples": [
                        {
                            "name": "haproxy_backend_current_sessions",
                            "labels": {"proxy": "test-app_be"},
                            "labels_str": '{proxy="test-app_be"}',
                            "value": 4,
                        }
                    ],
                }
            ],
        }

        endpoints = [
            ("GET", reverse("dashboard")),
            ("GET", reverse("backend_edit", args=["test-app_be"])),
            ("GET", reverse("partial_live_stats")),
            ("GET", reverse("partial_backend_card", args=["test-app_be"])),
            ("GET", reverse("partial_backend_live", args=["test-app_be"])),
            ("GET", reverse("partial_diagnostics")),
            ("POST", reverse("partial_ping")),
            ("GET", reverse("partial_metrics_summary")),
            ("GET", reverse("partial_metrics_raw")),
        ]
        for method, url in endpoints:
            res = self.client.get(url) if method == "GET" else self.client.post(url)
            self.assertEqual(res.status_code, 200, f"Failed rendering {method} {url}")

