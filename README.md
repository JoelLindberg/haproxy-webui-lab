# HAProxy Web UI

Minimal HAProxy Web UI for manual backend and server maintenance tasks, built with **Django**, **HTMX**, **Alpine.js**, **Tailwind CSS**, and **MariaDB**.

This web application consumes live runtime and configuration data from the **HAProxy Data Plane API v3** and scrapes metrics from HAProxy's built-in **Prometheus Exporter**. It is intended as a personal learning and lab project.

---

## Features

- **Live Traffic & Health Statistics**: Auto-refreshing (5s HTMX polling with pause/resume) cluster KPI cards and per-server session rates, active sessions (`scur`), queued connections (`qcur`), HTTP status breakdowns, throughput, and response times.
- **Backend Management**: Create, sync, inspect, and delete HAProxy backends in both the HAProxy Data Plane API and MariaDB (`http` and `tcp` modes, configurable balance algorithms).
- **Server Maintenance**: Add/remove backend servers, rename servers inline, and switch runtime administrative states (`Ready`, `Drain`, `Maintenance`) with one click.
- **Diagnostics & Prometheus Explorer**: Inspect HAProxy runtime info, ping the Data Plane API, view frontend/backend Prometheus session summaries, and filter raw Prometheus metric families.
- **Built-in Authentication & User Admin**: Powered by Django's session authentication (`email` or `username` sign-in) and Django Admin (`/django-admin/`).

---

## Tech Stack

- **Backend**: Python 3 / Django 6 (`haproxy_webui/`)
- **Database**: MariaDB (persisted in a Docker named volume `haproxy_webui_mariadb_data`)
- **Frontend**: Django Templates + HTMX 2 + Alpine.js 3 + Tailwind CSS 4 (standalone CLI via `pytailwindcss` — no Node.js required)
- **Upstream APIs**: HAProxy Data Plane API (`:5555/v3`) & HAProxy Prometheus Exporter (`:8405/metrics`)

---

## Development Setup

### 1. Configure HAProxy Data Plane API Password

Create a `.env` file in the repository root (next to `docker-compose.yml`):

```ini
DATAPLANE_PASSWORD=your-secure-dataplaneapi-password
```

### 2. Start HAProxy, Echo Backends, and MariaDB

```bash
docker compose up -d
```

> If you modify files under `haproxy/` or change `DATAPLANE_PASSWORD`, rebuild the HAProxy image:
> ```bash
> docker compose up -d --build
> ```

Note that MariaDB stores its data in the `haproxy_webui_mariadb_data` Docker volume and is exposed directly on port `3306`.

### 3. Set Up Python Virtual Environment & Dependencies

From the repository root:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r haproxy_webui/requirements.txt
```

### 4. Configure `haproxy_webui/.env`

Copy the example environment file and set `HAPROXY_DATAPLANE_PASS` to match `DATAPLANE_PASSWORD` from step 1:

```bash
cp haproxy_webui/.env.example haproxy_webui/.env
```

Key variables in `haproxy_webui/.env`:

```ini
# Django
DJANGO_SECRET_KEY=replace-me-with-a-long-random-secret-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

# MariaDB Database
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=haproxy_web_user
DB_PASSWORD=haproxy_web_password
DB_NAME=haproxy_webui

# HAProxy Data Plane API
HAPROXY_DATAPLANE_BASE_URL=http://localhost:5555
HAPROXY_DATAPLANE_USER=admin
HAPROXY_DATAPLANE_PASS=your-secure-dataplaneapi-password

# HAProxy Prometheus Exporter
HAPROXY_EXPORTER_PORT=8405
HAPROXY_METRICS_URL=http://localhost:8405/metrics

# Initial Admin User (used by `python manage.py seed`)
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@haproxy.local
ADMIN_PASSWORD=admin123
```

### 5. Run Migrations, Seed Initial Data, and Start Django

```bash
cd haproxy_webui
python manage.py migrate
python manage.py seed
python manage.py runserver 8000
```

- Open **http://localhost:8000** (or **http://localhost:8000/admin/**) and sign in with `ADMIN_EMAIL` (or `ADMIN_USERNAME`) and `ADMIN_PASSWORD`.
- `python manage.py seed` creates/updates the initial superuser and automatically syncs any backends already configured in HAProxy (`test-app_be`).

### 6. Rebuilding Tailwind CSS (When Editing Templates/Styles)

`haproxy_webui/static/css/app.css` is pre-compiled. After adding new Tailwind utility classes in `haproxy_webui/templates/` or editing `haproxy_webui/static/css/input.css`, rebuild the stylesheet with:

```bash
tailwindcss -i haproxy_webui/static/css/input.css -o haproxy_webui/static/css/app.css --minify
```

### 7. Managing Users

You can add or manage users in two ways:
- **Web UI**: Visit **http://localhost:8000/django-admin/** while signed in as a staff/superuser.
- **CLI**: Run `python manage.py createsuperuser` inside `haproxy_webui/`.

### 8. Running Tests

```bash
cd haproxy_webui
DB_ENGINE=sqlite python manage.py test
```

---

## Generating Load Through HAProxy

Spin up Locust to generate HTTP traffic against HAProxy (`http://haproxy:80`):

```bash
docker compose -f docker-compose-locust.yml up
```

Watch the **Live Traffic & Health Statistics** panel in the dashboard update active sessions (`scur`), session rate (`/s`), and total throughput in real time.

---

## HAProxy Reference Notes

- **Data Plane API Docs (local Redoc)**: `http://localhost:5555/v3/docs`
- **Prometheus Metrics Endpoint**: `http://localhost:8405/metrics`
- **Data Plane API Upstream Docs**: https://www.haproxy.com/documentation/dataplaneapi/

### Example `curl` Commands Against Data Plane API v3

```bash
curl -u admin http://localhost:5555/v3/info
curl -u admin http://localhost:5555/v3/health | jq
curl -u admin http://localhost:5555/v3/services/haproxy/runtime/info | jq

# Configuration & Version
curl -u admin http://localhost:5555/v3/services/haproxy/configuration/version
curl -u admin http://localhost:5555/v3/services/haproxy/configuration/backends | jq
curl -u admin http://localhost:5555/v3/services/haproxy/configuration/backends/test-app_be/servers | jq

# Native Stats
curl -u admin http://localhost:5555/v3/services/haproxy/stats/native | jq
```

### Operational Status vs. Administrative State

- **Status (`operational_state`)** — What HAProxy observes from health checks:
  - `UP` (green): Server is passing health checks and reachable.
  - `DOWN` (red): Server failed health checks or is offline.
  - `STOPPING` (amber): Server is draining active connections before stopping.
- **State (`admin_state`)** — What you instruct HAProxy to do:
  - `READY`: Accept new traffic normally.
  - `DRAIN`: Stop sending *new* connections while allowing existing sessions to finish.
  - `MAINT`: Remove server from load balancing completely for maintenance.

### Sessions (`scur`) vs. Queued Connections (`qcur`)

- **`scur` (Active Sessions)**: Active HTTP/TCP sessions currently being processed by the backend or server.
- **`qcur` (Queued Requests)**: Connections waiting in the queue because backend servers are at capacity—a sustained `qcur > 0` signals backpressure.
