"""Views and HTMX partial handlers for HAProxy Web UI."""
from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    CreateBackendForm,
    CreateServerForm,
    LoginForm,
    RenameServerForm,
    ServerStateForm,
)
from .haproxy_client import (
    HAProxyError,
    create_backend,
    create_server,
    delete_backend,
    delete_server,
    get_backend_details,
    get_backend_servers_enriched,
    get_diagnostics,
    get_live_overview_stats,
    get_prometheus_metrics,
    list_configured_backends,
    ping_dataplane,
    rename_server,
    set_server_state,
)
from .models import Backend


def is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def root_redirect(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect("login")


def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = LoginForm(request=request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        next_url = request.POST.get("next") or request.GET.get("next") or reverse("dashboard")
        return redirect(next_url)

    return render(
        request,
        "core/login.html",
        {
            "form": form,
            "next": request.GET.get("next", ""),
        },
    )


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")


@login_required
@require_GET
def legacy_edit_redirect(request: HttpRequest) -> HttpResponse:
    backend_name = (request.GET.get("backend") or "").strip()
    if backend_name:
        return redirect("backend_edit", backend_name=backend_name)
    return redirect("dashboard")


def _build_backend_card_context(backend: Backend) -> dict[str, Any]:
    try:
        details = get_backend_details(backend.name)
        servers = get_backend_servers_enriched(backend.name) if details["exists_in_haproxy"] else []
        return {
            "backend": backend,
            "details": details,
            "servers": servers,
            "error": None,
        }
    except HAProxyError as exc:
        return {
            "backend": backend,
            "details": None,
            "servers": [],
            "error": exc.message,
        }


def _build_backend_workspace_context(
    backend: Backend,
    *,
    create_form: CreateServerForm | None = None,
    feedback: dict[str, str] | None = None,
) -> dict[str, Any]:
    card_ctx = _build_backend_card_context(backend)
    live_stats = None
    live_error = None
    try:
        live_stats = get_live_overview_stats(backend_filter=backend.name)
    except HAProxyError as exc:
        live_error = exc.message

    return {
        **card_ctx,
        "create_server_form": create_form or CreateServerForm(),
        "feedback": feedback,
        "live_stats": live_stats,
        "live_error": live_error,
    }


@login_required
@require_GET
def dashboard_view(request: HttpRequest) -> HttpResponse:
    backends = list(Backend.objects.all())
    return render(
        request,
        "core/dashboard.html",
        {
            "backends": backends,
            "create_backend_form": CreateBackendForm(),
        },
    )


@login_required
@require_POST
def backend_create_view(request: HttpRequest) -> HttpResponse:
    form = CreateBackendForm(request.POST)
    feedback: dict[str, str] | None = None

    if form.is_valid():
        name = form.cleaned_data["name"]
        mode = form.cleaned_data["mode"]
        algorithm = form.cleaned_data["balance_algorithm"]
        description = form.cleaned_data.get("description", "")

        if Backend.objects.filter(name=name).exists():
            feedback = {"type": "error", "text": f"Backend '{name}' already exists in MariaDB."}
        else:
            try:
                create_backend(name=name, mode=mode, algorithm=algorithm)
                Backend.objects.create(
                    name=name,
                    mode=mode,
                    balance_algorithm=algorithm,
                    description=description,
                )
                feedback = {"type": "success", "text": f"Backend '{name}' ({mode.upper()}) created in HAProxy and MariaDB."}
                form = CreateBackendForm()
            except HAProxyError as exc:
                feedback = {"type": "error", "text": exc.message}
            except IntegrityError:
                feedback = {"type": "error", "text": f"Backend '{name}' already exists in MariaDB."}
    else:
        errors = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        feedback = {"type": "error", "text": errors or "Please fix the errors in the form."}

    if is_htmx(request):
        response = render(
            request,
            "core/partials/create_backend_and_list.html",
            {
                "backends": list(Backend.objects.all()),
                "create_backend_form": form,
                "feedback": feedback,
            },
        )
        if feedback and feedback["type"] == "success":
            response["HX-Trigger"] = "backendsChanged"
        return response

    if feedback:
        level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
        messages.add_message(request, level, feedback["text"])
    return redirect("dashboard")


@login_required
@require_POST
def backends_sync_view(request: HttpRequest) -> HttpResponse:
    try:
        configured = list_configured_backends()
        for item in configured:
            Backend.objects.update_or_create(
                name=item["name"],
                defaults={
                    "mode": item["mode"],
                    "balance_algorithm": item["algorithm"],
                },
            )
        feedback = {
            "type": "success",
            "text": f"Synced {len(configured)} backend(s) from HAProxy configuration.",
        }
    except HAProxyError as exc:
        feedback = {"type": "error", "text": exc.message}

    if is_htmx(request):
        return render(
            request,
            "core/partials/backends_list.html",
            {
                "backends": list(Backend.objects.all()),
                "sync_feedback": feedback,
            },
        )

    level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
    messages.add_message(request, level, feedback["text"])
    return redirect("dashboard")


@login_required
@require_GET
def backend_edit_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    ctx = _build_backend_workspace_context(backend)
    return render(request, "core/backend_edit.html", ctx)


@login_required
@require_POST
def backend_delete_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    confirm_name = (request.POST.get("confirm_name") or "").strip()

    if confirm_name != backend.name:
        feedback = {
            "type": "error",
            "text": f"Confirmation name did not match '{backend.name}'.",
        }
        if is_htmx(request):
            ctx = _build_backend_workspace_context(backend, feedback=feedback)
            return render(request, "core/partials/backend_workspace.html", ctx)
        messages.error(request, feedback["text"])
        return redirect("backend_edit", backend_name=backend.name)

    try:
        delete_backend(backend.name, ignore_missing=True)
        backend.delete()
        messages.success(request, f"Backend '{backend_name}' deleted from HAProxy and MariaDB.")
        if is_htmx(request):
            res = HttpResponse(status=204)
            res["HX-Redirect"] = reverse("dashboard")
            return res
        return redirect("dashboard")
    except HAProxyError as exc:
        feedback = {"type": "error", "text": exc.message}
        if is_htmx(request):
            ctx = _build_backend_workspace_context(backend, feedback=feedback)
            return render(request, "core/partials/backend_workspace.html", ctx)
        messages.error(request, exc.message)
        return redirect("backend_edit", backend_name=backend.name)


@login_required
@require_POST
def server_create_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    form = CreateServerForm(request.POST)
    if form.is_valid():
        srv_name = form.cleaned_data["name"]
        address = form.cleaned_data["address"]
        port = form.cleaned_data["port"]
        check_enabled = form.cleaned_data["check"] == "enabled"
        try:
            create_server(
                backend_name=backend.name,
                server_name=srv_name,
                address=address,
                port=port,
                check=check_enabled,
            )
            feedback = {"type": "success", "text": f"Server '{srv_name}' ({address}:{port}) added to '{backend.name}'."}
            form = CreateServerForm()
        except HAProxyError as exc:
            feedback = {"type": "error", "text": exc.message}
    else:
        errors = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        feedback = {"type": "error", "text": errors}

    if is_htmx(request):
        ctx = _build_backend_workspace_context(backend, create_form=form, feedback=feedback)
        return render(request, "core/partials/backend_workspace.html", ctx)

    level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
    messages.add_message(request, level, feedback["text"])
    return redirect("backend_edit", backend_name=backend.name)


@login_required
@require_POST
def server_state_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    form = ServerStateForm(request.POST)
    if form.is_valid():
        srv_name = form.cleaned_data["server_name"]
        state = form.cleaned_data["admin_state"]
        state_labels = {"ready": "Ready", "drain": "Drain", "maint": "Maintenance"}
        try:
            set_server_state(backend.name, srv_name, state)
            feedback = {
                "type": "success",
                "text": f"Server '{srv_name}' administrative state set to {state_labels[state]}.",
            }
        except HAProxyError as exc:
            feedback = {"type": "error", "text": exc.message}
    else:
        feedback = {"type": "error", "text": "Invalid server state request."}

    if is_htmx(request):
        ctx = _build_backend_workspace_context(backend, feedback=feedback)
        return render(request, "core/partials/backend_workspace.html", ctx)

    level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
    messages.add_message(request, level, feedback["text"])
    return redirect("backend_edit", backend_name=backend.name)


@login_required
@require_POST
def server_rename_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    form = RenameServerForm(request.POST)
    if form.is_valid():
        old_name = form.cleaned_data["old_name"]
        new_name = form.cleaned_data["new_name"]
        if old_name == new_name:
            feedback = {"type": "error", "text": "New server name is identical to the current name."}
        else:
            try:
                rename_server(backend.name, old_name, new_name)
                feedback = {
                    "type": "success",
                    "text": f"Server renamed from '{old_name}' to '{new_name}'.",
                }
            except HAProxyError as exc:
                feedback = {"type": "error", "text": exc.message}
    else:
        errors = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        feedback = {"type": "error", "text": errors or "Invalid server name."}

    if is_htmx(request):
        ctx = _build_backend_workspace_context(backend, feedback=feedback)
        return render(request, "core/partials/backend_workspace.html", ctx)

    level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
    messages.add_message(request, level, feedback["text"])
    return redirect("backend_edit", backend_name=backend.name)


@login_required
@require_POST
def server_delete_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    srv_name = (request.POST.get("server_name") or "").strip()
    if not srv_name:
        feedback = {"type": "error", "text": "Server name is required."}
    else:
        try:
            delete_server(backend.name, srv_name)
            feedback = {"type": "success", "text": f"Server '{srv_name}' removed from '{backend.name}'."}
        except HAProxyError as exc:
            feedback = {"type": "error", "text": exc.message}

    if is_htmx(request):
        ctx = _build_backend_workspace_context(backend, feedback=feedback)
        return render(request, "core/partials/backend_workspace.html", ctx)

    level = messages.SUCCESS if feedback["type"] == "success" else messages.ERROR
    messages.add_message(request, level, feedback["text"])
    return redirect("backend_edit", backend_name=backend.name)


# --- HTMX Partial Views ---


@login_required
@require_GET
def partial_live_stats_view(request: HttpRequest) -> HttpResponse:
    try:
        stats = get_live_overview_stats()
        error = None
    except HAProxyError as exc:
        stats = None
        error = exc.message

    return render(
        request,
        "core/partials/live_stats.html",
        {"stats": stats, "error": error},
    )


@login_required
@require_GET
def partial_backend_card_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    ctx = _build_backend_card_context(backend)
    return render(request, "core/partials/backend_card_body.html", ctx)


@login_required
@require_GET
def partial_backend_live_view(request: HttpRequest, backend_name: str) -> HttpResponse:
    backend = get_object_or_404(Backend, name=backend_name)
    ctx = _build_backend_card_context(backend)
    try:
        ctx["live_stats"] = get_live_overview_stats(backend_filter=backend.name)
        ctx["live_error"] = None
    except HAProxyError as exc:
        ctx["live_stats"] = None
        ctx["live_error"] = exc.message
    return render(request, "core/partials/backend_live_details.html", ctx)


@login_required
@require_GET
def partial_diagnostics_view(request: HttpRequest) -> HttpResponse:
    try:
        diagnostics = get_diagnostics()
        error = None
    except HAProxyError as exc:
        diagnostics = None
        error = exc.message

    return render(
        request,
        "core/partials/diagnostics.html",
        {"diagnostics": diagnostics, "error": error},
    )


@login_required
@require_POST
def partial_ping_view(request: HttpRequest) -> HttpResponse:
    result = ping_dataplane()
    return render(
        request,
        "core/partials/ping_result.html",
        {
            "ping": result,
            "formatted_json": json.dumps(result["data"], indent=2),
        },
    )


@login_required
@require_GET
def partial_metrics_summary_view(request: HttpRequest) -> HttpResponse:
    try:
        metrics = get_prometheus_metrics()
        error = None
    except HAProxyError as exc:
        metrics = None
        error = exc.message

    return render(
        request,
        "core/partials/metrics_summary.html",
        {"metrics": metrics, "error": error},
    )


@login_required
@require_GET
def partial_metrics_raw_view(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "")
    try:
        metrics = get_prometheus_metrics(filter_query=q)
        error = None
    except HAProxyError as exc:
        metrics = None
        error = exc.message

    return render(
        request,
        "core/partials/metrics_raw.html",
        {"metrics": metrics, "error": error, "filter_query": q},
    )
