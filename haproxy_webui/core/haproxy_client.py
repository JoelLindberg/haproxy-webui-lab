"""Client for the HAProxy Data Plane API v3 and Prometheus Exporter."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from prometheus_client.parser import text_string_to_metric_families


class HAProxyError(Exception):
    """Raised when the HAProxy Data Plane API or Exporter returns an error."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def format_bytes(num_bytes: int | float) -> str:
    """Format byte counts into a compact human-readable string."""
    value = float(max(num_bytes or 0, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_uptime(uptime_seconds: int | float | None) -> str:
    """Format seconds into 'Xd Xh Xm Xs'."""
    seconds = int(uptime_seconds or 0)
    if seconds <= 0:
        return "Unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m {secs}s"


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize Data Plane API v2/v3 list responses."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def _extract_object(payload: Any) -> dict[str, Any]:
    """Normalize Data Plane API v2/v3 single-object responses."""
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload
    return {}


def call_dataplane(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: float = 5.0,
) -> httpx.Response:
    """Perform an authenticated HTTP request to the HAProxy Data Plane API."""
    base_url = settings.HAPROXY_DATAPLANE_BASE_URL.rstrip("/")
    url = f"{base_url}/{path.lstrip('/')}"
    auth = httpx.BasicAuth(
        username=settings.HAPROXY_DATAPLANE_USER,
        password=settings.HAPROXY_DATAPLANE_PASS,
    )
    try:
        return httpx.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            auth=auth,
            timeout=httpx.Timeout(timeout, connect=1.5),
        )
    except httpx.RequestError as exc:
        raise HAProxyError(f"Unable to reach HAProxy Data Plane API at {base_url}: {exc}", status_code=502) from exc


def _json_or_empty(res: httpx.Response) -> Any:
    if not res.text.strip():
        return {}
    try:
        return res.json()
    except ValueError as exc:
        raise HAProxyError(f"Invalid JSON from HAProxy Data Plane API: {exc}", status_code=502) from exc


def _ensure_ok(res: httpx.Response, action: str) -> Any:
    if not res.is_success:
        detail = res.text.strip()
        try:
            body = res.json()
            if isinstance(body, dict) and body.get("message"):
                detail = body["message"]
        except ValueError:
            pass
        raise HAProxyError(f"{action} (HTTP {res.status_code}): {detail}", status_code=res.status_code)
    return _json_or_empty(res)


def get_configuration_version() -> int:
    """Fetch current HAProxy configuration version integer."""
    res = call_dataplane("GET", "/v3/services/haproxy/configuration/version")
    data = _ensure_ok(res, "Failed to read HAProxy configuration version")
    if isinstance(data, int):
        return data
    if isinstance(data, str) and data.strip().isdigit():
        return int(data.strip())
    if isinstance(data, dict):
        return int(data.get("version") or data.get("_version") or 1)
    return 1


def list_configured_backends() -> list[dict[str, Any]]:
    """Return all backends configured in HAProxy."""
    res = call_dataplane("GET", "/v3/services/haproxy/configuration/backends")
    data = _ensure_ok(res, "Failed to list HAProxy backends")
    backends = []
    for item in _extract_list(data):
        balance = item.get("balance") or {}
        backends.append(
            {
                "name": item.get("name", ""),
                "mode": item.get("mode") or "http",
                "algorithm": balance.get("algorithm") or "roundrobin",
            }
        )
    return [b for b in backends if b["name"]]


def create_backend(name: str, mode: str = "http", algorithm: str = "roundrobin") -> dict[str, Any]:
    """Create a backend in HAProxy configuration."""
    version = get_configuration_version()
    res = call_dataplane(
        "POST",
        "/v3/services/haproxy/configuration/backends",
        params={"version": version},
        json_body={
            "name": name,
            "mode": mode,
            "balance": {"algorithm": algorithm},
        },
    )
    return _ensure_ok(res, f"Failed to create backend '{name}'")


def delete_backend(name: str, ignore_missing: bool = False) -> dict[str, Any]:
    """Delete a backend from HAProxy configuration."""
    version = get_configuration_version()
    encoded = quote(name, safe="")
    res = call_dataplane(
        "DELETE",
        f"/v3/services/haproxy/configuration/backends/{encoded}",
        params={"version": version},
    )
    if ignore_missing and res.status_code == 404:
        return {}
    return _ensure_ok(res, f"Failed to delete backend '{name}'")


def get_backend_details(name: str) -> dict[str, Any]:
    """Fetch backend details from native stats with fallback to configuration."""
    encoded = quote(name, safe="")
    stats_res = call_dataplane(
        "GET",
        "/v3/services/haproxy/stats/native",
        params={"type": "backend", "name": name},
    )
    stats_entry: dict[str, Any] | None = None
    if stats_res.is_success:
        stats_payload = _json_or_empty(stats_res)
        stats_list = stats_payload.get("stats", []) if isinstance(stats_payload, dict) else []
        if stats_list:
            stats_entry = stats_list[0]

    config_res = call_dataplane("GET", f"/v3/services/haproxy/configuration/backends/{encoded}")
    if stats_entry is None and config_res.status_code == 404:
        return {
            "name": name,
            "exists_in_haproxy": False,
            "algorithm": None,
            "mode": None,
            "status": None,
            "current_sessions": 0,
            "queued_connections": 0,
            "total_sessions": 0,
            "session_rate": 0,
            "bytes_out_human": "0 B",
        }

    cfg = _extract_object(_json_or_empty(config_res)) if config_res.is_success else {}
    s = (stats_entry or {}).get("stats") or {}
    cfg_balance = cfg.get("balance") or {}

    return {
        "name": (stats_entry or {}).get("name") or cfg.get("name") or name,
        "exists_in_haproxy": True,
        "algorithm": s.get("algo") or cfg_balance.get("algorithm") or "—",
        "mode": s.get("mode") or cfg.get("mode") or "—",
        "status": s.get("status") or "UNKNOWN",
        "current_sessions": int(s.get("scur") or 0),
        "max_sessions": int(s.get("smax") or 0),
        "queued_connections": int(s.get("qcur") or 0),
        "total_sessions": int(s.get("stot") or 0),
        "session_rate": int(s.get("rate") or 0),
        "bytes_in": int(s.get("bin") or 0),
        "bytes_out": int(s.get("bout") or 0),
        "bytes_out_human": format_bytes(s.get("bout") or 0),
    }


def get_backend_servers_enriched(backend_name: str) -> list[dict[str, Any]]:
    """Fetch merged configuration, runtime state, and live traffic stats for a backend's servers."""
    encoded = quote(backend_name, safe="")
    cfg_res = call_dataplane("GET", f"/v3/services/haproxy/configuration/backends/{encoded}/servers")
    if cfg_res.status_code == 404:
        return []
    cfg_servers = _extract_list(_ensure_ok(cfg_res, f"Failed to fetch servers for '{backend_name}'"))

    runtime_map: dict[str, dict[str, Any]] = {}
    rt_res = call_dataplane("GET", f"/v3/services/haproxy/runtime/backends/{encoded}/servers")
    if rt_res.is_success:
        for rt in _extract_list(_json_or_empty(rt_res)):
            if rt.get("name"):
                runtime_map[rt["name"]] = rt

    stats_map: dict[str, dict[str, Any]] = {}
    st_res = call_dataplane(
        "GET",
        "/v3/services/haproxy/stats/native",
        params={"type": "server", "parent": backend_name},
    )
    if st_res.is_success:
        st_payload = _json_or_empty(st_res)
        for entry in (st_payload.get("stats") or []) if isinstance(st_payload, dict) else []:
            if entry.get("name"):
                stats_map[entry["name"]] = entry.get("stats") or {}

    enriched: list[dict[str, Any]] = []
    for srv in cfg_servers:
        srv_name = srv.get("name", "")
        rt = runtime_map.get(srv_name, {})
        st = stats_map.get(srv_name, {})

        op_state = (rt.get("operational_state") or st.get("status") or "unknown").lower()
        admin_state = (rt.get("admin_state") or "unknown").lower()
        bout = int(st.get("bout") or 0)

        enriched.append(
            {
                "name": srv_name,
                "address": srv.get("address") or rt.get("address") or "—",
                "port": srv.get("port") or rt.get("port") or "—",
                "check": srv.get("check") or "disabled",
                "operational_state": op_state,
                "operational_label": op_state.upper(),
                "admin_state": admin_state,
                "admin_label": "MAINT" if admin_state == "maint" else admin_state.upper(),
                "current_sessions": int(st.get("scur") or 0),
                "max_sessions": int(st.get("smax") or 0),
                "total_sessions": int(st.get("stot") or 0),
                "session_rate": int(st.get("rate") or 0),
                "queued_connections": int(st.get("qcur") or 0),
                "bytes_out": bout,
                "bytes_out_human": format_bytes(bout),
                "response_time_ms": int(st.get("rtime") or 0),
                "check_status": st.get("check_status") or "",
            }
        )
    return enriched


def create_server(
    backend_name: str,
    server_name: str,
    address: str,
    port: int,
    check: bool = True,
) -> dict[str, Any]:
    """Create a server inside a backend in HAProxy configuration."""
    version = get_configuration_version()
    encoded_be = quote(backend_name, safe="")
    res = call_dataplane(
        "POST",
        f"/v3/services/haproxy/configuration/backends/{encoded_be}/servers",
        params={"version": version},
        json_body={
            "name": server_name,
            "address": address,
            "port": int(port),
            "check": "enabled" if check else "disabled",
        },
    )
    return _ensure_ok(res, f"Failed to create server '{server_name}'")


def delete_server(backend_name: str, server_name: str) -> dict[str, Any]:
    """Delete a server from a backend in HAProxy configuration."""
    version = get_configuration_version()
    encoded_be = quote(backend_name, safe="")
    encoded_srv = quote(server_name, safe="")
    res = call_dataplane(
        "DELETE",
        f"/v3/services/haproxy/configuration/backends/{encoded_be}/servers/{encoded_srv}",
        params={"version": version},
    )
    return _ensure_ok(res, f"Failed to delete server '{server_name}'")


def rename_server(backend_name: str, old_name: str, new_name: str) -> dict[str, Any]:
    """Rename an existing server in HAProxy configuration via PUT replacement."""
    version = get_configuration_version()
    encoded_be = quote(backend_name, safe="")
    encoded_old = quote(old_name, safe="")

    get_res = call_dataplane(
        "GET",
        f"/v3/services/haproxy/configuration/backends/{encoded_be}/servers/{encoded_old}",
    )
    existing = _extract_object(_ensure_ok(get_res, f"Failed to read server '{old_name}'"))
    updated = {**existing, "name": new_name}
    # Strip any metadata keys if present
    updated.pop("_version", None)

    put_res = call_dataplane(
        "PUT",
        f"/v3/services/haproxy/configuration/backends/{encoded_be}/servers/{encoded_old}",
        params={"version": version},
        json_body=updated,
    )
    return _ensure_ok(put_res, f"Failed to rename server '{old_name}' to '{new_name}'")


def set_server_state(backend_name: str, server_name: str, admin_state: str) -> dict[str, Any]:
    """Update a server's runtime administrative state (ready, drain, maint)."""
    if admin_state not in ("ready", "drain", "maint"):
        raise HAProxyError(f"Invalid administrative state: {admin_state}", status_code=400)
    encoded_be = quote(backend_name, safe="")
    encoded_srv = quote(server_name, safe="")
    res = call_dataplane(
        "PUT",
        f"/v3/services/haproxy/runtime/backends/{encoded_be}/servers/{encoded_srv}",
        json_body={"admin_state": admin_state},
    )
    return _ensure_ok(res, f"Failed to set server '{server_name}' state to '{admin_state}'")


def get_diagnostics() -> dict[str, Any]:
    """Fetch Data Plane API and HAProxy runtime diagnostic metrics."""
    info_res = call_dataplane("GET", "/v3/info")
    info_data = _ensure_ok(info_res, "Failed to fetch Data Plane API info")

    health_status = "unknown"
    health_res = call_dataplane("GET", "/v3/health")
    if health_res.is_success:
        health_data = _json_or_empty(health_res)
        if isinstance(health_data, dict):
            health_status = str(health_data.get("haproxy") or "unknown").lower()

    haproxy_version = "Unknown"
    haproxy_pid = None
    haproxy_uptime = "Unknown"
    haproxy_processes = 0
    total_bytes_out = 0

    runtime_res = call_dataplane("GET", "/v3/services/haproxy/runtime/info")
    if runtime_res.is_success:
        rt_payload = _json_or_empty(runtime_res)
        # Runtime info may be a dict or a single-item list depending on Data Plane API version
        if isinstance(rt_payload, list) and rt_payload:
            rt_info = rt_payload[0].get("info") or rt_payload[0]
        elif isinstance(rt_payload, dict):
            rt_info = rt_payload.get("info") or rt_payload
        else:
            rt_info = {}

        haproxy_version = rt_info.get("version") or "Unknown"
        haproxy_pid = rt_info.get("pid")
        haproxy_uptime = format_uptime(rt_info.get("uptime"))
        haproxy_processes = int(rt_info.get("processes") or 0)
        total_bytes_out = int(rt_info.get("total_bytes_out") or 0)

    api_info = info_data.get("api") or {} if isinstance(info_data, dict) else {}
    return {
        "api_version": api_info.get("version") or "Unknown",
        "build_date": api_info.get("build_date") or "—",
        "haproxy_version": haproxy_version,
        "health": health_status,
        "haproxy_pid": haproxy_pid,
        "haproxy_uptime": haproxy_uptime,
        "haproxy_processes": haproxy_processes,
        "total_bytes_out": total_bytes_out,
        "total_bytes_out_human": format_bytes(total_bytes_out),
    }


def ping_dataplane() -> dict[str, Any]:
    """Ping HAProxy Data Plane API health endpoint and measure round-trip latency."""
    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    try:
        res = call_dataplane("GET", "/v3/health", timeout=3.0)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        try:
            payload = res.json()
        except ValueError:
            payload = {"raw": res.text}
        return {
            "ok": res.is_success,
            "status_code": res.status_code,
            "latency_ms": elapsed_ms,
            "timestamp": timestamp,
            "data": payload,
        }
    except HAProxyError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": False,
            "status_code": exc.status_code or 502,
            "latency_ms": elapsed_ms,
            "timestamp": timestamp,
            "data": {"error": exc.message},
        }


def get_live_overview_stats(backend_filter: str | None = None) -> dict[str, Any]:
    """
    Aggregate live traffic & health statistics from HAProxy native stats.
    Replaces realtime charts with instant, live-polled KPI cards and per-server metrics.
    """
    params: dict[str, Any] = {}
    if backend_filter:
        params["parent"] = backend_filter

    res = call_dataplane("GET", "/v3/services/haproxy/stats/native", params=params or None)
    payload = _ensure_ok(res, "Failed to fetch live HAProxy statistics")
    entries = payload.get("stats", []) if isinstance(payload, dict) else []

    # Also grab runtime server states if we want accurate ready/drain/maint badges
    backends_total = 0
    backends_up = 0
    servers_total = 0
    servers_up = 0
    servers_down = 0
    servers_maint = 0
    servers_drain = 0

    current_sessions = 0
    session_rate = 0
    max_session_rate = 0
    queued_connections = 0
    total_sessions = 0
    bytes_in = 0
    bytes_out = 0
    http_2xx = 0
    http_4xx = 0
    http_5xx = 0
    errors_total = 0

    server_rows: list[dict[str, Any]] = []

    for item in entries:
        entry_type = item.get("type")
        name = item.get("name") or ""
        backend_name = item.get("backend_name") or ""
        st = item.get("stats") or {}

        if backend_filter and entry_type == "backend" and name != backend_filter:
            continue
        if backend_filter and entry_type == "server" and backend_name != backend_filter:
            continue

        if entry_type == "backend":
            backends_total += 1
            if str(st.get("status") or "").upper() == "UP":
                backends_up += 1
            current_sessions += int(st.get("scur") or 0)
            session_rate += int(st.get("rate") or 0)
            max_session_rate = max(max_session_rate, int(st.get("rate_max") or 0))
            queued_connections += int(st.get("qcur") or 0)
            total_sessions += int(st.get("stot") or 0)
            bytes_in += int(st.get("bin") or 0)
            bytes_out += int(st.get("bout") or 0)
            http_2xx += int(st.get("hrsp_2xx") or 0)
            http_4xx += int(st.get("hrsp_4xx") or 0)
            http_5xx += int(st.get("hrsp_5xx") or 0)
            errors_total += int(st.get("econ") or 0) + int(st.get("eresp") or 0)

        elif entry_type == "server":
            servers_total += 1
            raw_status = str(st.get("status") or "UNKNOWN").upper()
            if raw_status.startswith("UP"):
                servers_up += 1
                op_state = "up"
                admin_state = "drain" if "DRAIN" in raw_status else "ready"
            elif "DRAIN" in raw_status:
                servers_up += 1
                servers_drain += 1
                op_state = "up"
                admin_state = "drain"
            elif "MAINT" in raw_status:
                servers_maint += 1
                op_state = "down"
                admin_state = "maint"
            elif "NOLB" in raw_status or "STOP" in raw_status:
                op_state = "stopping"
                admin_state = "drain"
            else:
                servers_down += 1
                op_state = "down"
                admin_state = "ready"

            if admin_state == "drain" and "DRAIN" not in raw_status:
                servers_drain += 1

            srv_bout = int(st.get("bout") or 0)
            srv_bin = int(st.get("bin") or 0)
            server_rows.append(
                {
                    "backend": backend_name,
                    "name": name,
                    "raw_status": raw_status,
                    "operational_state": op_state,
                    "admin_state": admin_state,
                    "current_sessions": int(st.get("scur") or 0),
                    "max_sessions": int(st.get("smax") or 0),
                    "session_rate": int(st.get("rate") or 0),
                    "queued_connections": int(st.get("qcur") or 0),
                    "total_sessions": int(st.get("stot") or 0),
                    "bytes_in_human": format_bytes(srv_bin),
                    "bytes_out_human": format_bytes(srv_bout),
                    "response_time_ms": int(st.get("rtime") or 0),
                    "check_status": st.get("check_status") or "—",
                }
            )

    # If filtering to a single backend and stats/native didn't include the backend aggregate row,
    # sum from server rows so KPI cards remain accurate.
    if backend_filter and backends_total == 0 and server_rows:
        current_sessions = sum(r["current_sessions"] for r in server_rows)
        session_rate = sum(r["session_rate"] for r in server_rows)
        queued_connections = sum(r["queued_connections"] for r in server_rows)
        total_sessions = sum(r["total_sessions"] for r in server_rows)

    server_rows.sort(key=lambda r: (r["backend"], r["name"]))

    return {
        "updated_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "backends_total": backends_total,
        "backends_up": backends_up,
        "servers_total": servers_total,
        "servers_up": servers_up,
        "servers_down": servers_down,
        "servers_maint": servers_maint,
        "servers_drain": servers_drain,
        "current_sessions": current_sessions,
        "session_rate": session_rate,
        "max_session_rate": max_session_rate,
        "queued_connections": queued_connections,
        "total_sessions": total_sessions,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "bytes_in_human": format_bytes(bytes_in),
        "bytes_out_human": format_bytes(bytes_out),
        "http_2xx": http_2xx,
        "http_4xx": http_4xx,
        "http_5xx": http_5xx,
        "errors_total": errors_total,
        "servers": server_rows,
    }


def get_prometheus_metrics(filter_query: str = "") -> dict[str, Any]:
    """Fetch and parse HAProxy Prometheus exporter metrics."""
    url = settings.HAPROXY_METRICS_URL
    try:
        res = httpx.get(url, timeout=5.0)
    except httpx.RequestError as exc:
        raise HAProxyError(f"Unable to reach Prometheus exporter at {url}: {exc}", status_code=502) from exc

    if not res.is_success:
        raise HAProxyError(
            f"Prometheus exporter returned HTTP {res.status_code}",
            status_code=res.status_code,
        )

    raw_text = res.text
    raw_length = len(raw_text.encode("utf-8"))

    # Lookup tables for proxy summary table
    fe_curr: dict[str, int] = {}
    fe_max: dict[str, int] = {}
    fe_tot: dict[str, int] = {}
    fe_rate: dict[str, float] = {}

    be_curr: dict[str, int] = {}
    be_max: dict[str, int] = {}
    be_tot: dict[str, int] = {}
    be_rate: dict[str, float] = {}

    families_list: list[dict[str, Any]] = []
    q = filter_query.strip().lower()

    for family in text_string_to_metric_families(raw_text):
        # prometheus_client strips '_total' from Counter family.name; inspect samples for exact metric names
        sample_items: list[dict[str, Any]] = []
        display_name = family.name
        if family.type == "counter" and family.samples:
            # Find primary counter sample name (excluding _created)
            for s in family.samples:
                if not s.name.endswith("_created"):
                    display_name = s.name
                    break

        for s in family.samples:
            if s.name.endswith("_created"):
                continue
            proxy = s.labels.get("proxy", "")
            val = s.value

            if proxy:
                if s.name == "haproxy_frontend_current_sessions":
                    fe_curr[proxy] = int(val)
                elif s.name == "haproxy_frontend_max_sessions":
                    fe_max[proxy] = int(val)
                elif s.name == "haproxy_frontend_sessions_total":
                    fe_tot[proxy] = int(val)
                elif s.name == "haproxy_frontend_current_session_rate":
                    fe_rate[proxy] = float(val)
                elif s.name == "haproxy_backend_current_sessions":
                    be_curr[proxy] = int(val)
                elif s.name == "haproxy_backend_max_sessions":
                    be_max[proxy] = int(val)
                elif s.name == "haproxy_backend_sessions_total":
                    be_tot[proxy] = int(val)
                elif s.name == "haproxy_backend_current_session_rate":
                    be_rate[proxy] = float(val)

            labels_str = (
                "{" + ", ".join(f'{k}="{v}"' for k, v in s.labels.items()) + "}"
                if s.labels
                else "(no labels)"
            )
            sample_val = int(val) if float(val).is_integer() else round(float(val), 4)
            sample_items.append(
                {
                    "name": s.name,
                    "labels": s.labels,
                    "labels_str": labels_str,
                    "value": sample_val,
                }
            )

        if not q or q in display_name.lower() or q in (family.documentation or "").lower():
            families_list.append(
                {
                    "name": display_name,
                    "type": family.type,
                    "help": family.documentation or "",
                    "sample_count": len(sample_items),
                    "samples": sample_items,
                }
            )

    proxy_rows: list[dict[str, Any]] = []
    for name in sorted(fe_curr.keys()):
        proxy_rows.append(
            {
                "name": name,
                "type": "frontend",
                "current_sessions": fe_curr.get(name, 0),
                "max_sessions": fe_max.get(name, 0),
                "total_sessions": fe_tot.get(name, 0),
                "session_rate": f"{fe_rate.get(name, 0.0):.2f}",
            }
        )
    for name in sorted(be_curr.keys()):
        proxy_rows.append(
            {
                "name": name,
                "type": "backend",
                "current_sessions": be_curr.get(name, 0),
                "max_sessions": be_max.get(name, 0),
                "total_sessions": be_tot.get(name, 0),
                "session_rate": f"{be_rate.get(name, 0.0):.2f}",
            }
        )

    return {
        "metrics_count": len(families_list),
        "raw_length": raw_length,
        "raw_kb": f"{raw_length / 1024:.1f}",
        "proxy_rows": proxy_rows,
        "families": families_list,
    }
