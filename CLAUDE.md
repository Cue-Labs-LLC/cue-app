# CLAUDE.md — Eventflow (enhanced-ltv-updater)

## Project Overview

Eventflow is a **multi-tenant event ticketing and customer analytics** Django application. It ingests CSV ticket order data, tracks customer lifetime value, and provides analytics (RFM segmentation, cohort retention, sales forecasting).

**Stack:** Django 5.2 · Python 3.12 · Pandas · Celery + Redis · PostgreSQL (prod) / SQLite (dev) · Bootstrap 5 · Chart.js · Render (hosting)

---

## Quick Reference

```
# Run dev server
python manage.py runserver

# Run Celery worker (needs Redis)
celery -A ltv_updater worker --loglevel=info

# Migrations
python manage.py makemigrations && python manage.py migrate

# Collect static files
python manage.py collectstatic --noinput

# Run tests
python manage.py test tickets
```

---

## Project Structure

```
ltv_updater/          # Django project config (settings, urls, celery, wsgi)
tickets/              # Main (and only) Django app
├── models.py         # All models (BaseModel → UUID PKs, AuditBaseModel → soft delete + versioning)
├── views.py          # All views — function-based only
├── urls.py           # URL patterns (app_name = 'tickets')
├── forms.py          # Crispy forms (Bootstrap 5)
├── tasks.py          # Celery shared_tasks
├── utils.py          # get_organization(), require_org decorator
├── csv_processor.py  # CSV ingestion pipeline (CSVProcessor class)
├── admin.py          # Django admin registration
├── tests.py          # Test cases
├── services/         # Business logic layer (see below)
│   ├── segmentation/     # RFM scoring (RFMCalculator)
│   ├── cohort_analysis/  # Repeat customers, cohort retention
│   ├── forecasting/      # Sales curve forecasting
│   └── google_docs/      # External Google Docs sync
├── templates/tickets/    # Django templates (extends base.html)
├── static/tickets/css/   # dashboard.css (custom design system)
├── templatetags/         # Custom template filters
├── management/commands/  # Management commands
└── migrations/           # 29+ migrations
```

---

## Architecture Rules

### Multi-Tenancy (Organization Scoping)

**Every query must be scoped to the current user's organization.** This is the single most important rule.

```python
# CORRECT — always filter by org
org = get_organization(request)
Event.objects.filter(organization=org)
TicketOrder.objects.filter(customer__organization=org)

# WRONG — leaks data across tenants
Event.objects.all()
```

- Views use `@require_org` decorator + `get_organization(request)` from `tickets/utils.py`
- Services accept `organization` in `__init__` and scope all queries internally
- Never expose unscoped querysets

### Models

- **Base classes:** `BaseModel` (UUID pk, timestamps) and `AuditBaseModel` (adds created_by, updated_by, version, soft delete)
- **All PKs are UUIDs** — use `uuid.uuid4`, never auto-increment integers
- **Soft delete:** `AuditBaseModel` has `deleted_at`; call `delete()` for soft, `hard_delete()` for permanent
- **Currency:** Always `DecimalField(max_digits=10, decimal_places=2)` — never use floats for money
- **Indexing:** Add `db_index=True` on fields used in filters/ordering (dates, foreign keys, email)
- No new models unless absolutely necessary — derive insights from existing data when possible

### Views

- **Function-based views only** (no CBVs except Django auth views)
- Every authenticated view gets `@login_required` + `@require_org`
- Pattern: get org → query scoped data → build context → render template
- JSON endpoints return `JsonResponse` (see `forecast_api`)
- Use `get_object_or_404(Model.objects.filter(organization=org), id=pk)` for detail views

### Query Performance

- **Always use `select_related()`** for ForeignKey fields accessed in templates (e.g., `order.customer.name`, `event.venue.city`). Missing this causes an extra query per row.
- **Always use `prefetch_related()`** for reverse FK / M2M sets iterated in templates.
- **Never call `.count()` or `.aggregate()` inside a Python loop** over a queryset — this is an N+1 pattern. Instead, annotate the queryset with `Count()` / `Sum()` so the DB does it in one pass.
- **Combine multiple aggregations** on the same table into a single `.aggregate()` call to reduce DB roundtrips (e.g., `Count('id')` + `Sum('total_amount')` together).
- **Beware of join inflation with mixed annotations:** When a queryset annotates both `Count('related__nested')` and `Sum('related__amount')`, the join through `nested` multiplies rows and inflates the `Sum`. Use a `Subquery` to isolate the `Sum`, or use `distinct=True` on `Count`. See `event_list` view for the canonical pattern.
- **Use `.values()` + `.annotate()`** for group-by aggregation instead of loading full model instances into Python.
- **Annotate counts instead of calling `.count()` in templates:** `{{ order.tickets.count }}` triggers a query per row. Annotate `ticket_count=Count('tickets')` in the view and use `{{ order.ticket_count }}`.
- **Paginate before accessing querysets** — never call `.all()` unbounded when results go to a template.

### Service Layer (`tickets/services/`)

- Complex business logic lives in services, not views
- Pattern: **Calculator class** with `__init__(self, organization)` and a `calculate()` method returning a dict
- Services do ORM queries internally — views just call `.calculate()` and pass results to templates
- Use Pandas for aggregation when SQL would be unwieldy (already a dependency)
- Keep services synchronous unless processing is truly slow (>5s) — then use Celery

### Celery Tasks

- Use `@shared_task(bind=True, max_retries=2, default_retry_delay=30)` pattern
- Import models inside the task function (avoid circular imports)
- Pass `organization_id` (string) as argument, not ORM objects
- Set progress flags on Organization when applicable (e.g., `rfm_recalc_in_progress`)
- Dev without Redis: tasks run eagerly via `CELERY_TASK_ALWAYS_EAGER`

### Templates

- All templates extend `tickets/base.html`
- Use blocks: `{% block title %}`, `{% block content %}`, `{% block extra_head %}`, `{% block extra_js %}`
- Charts: Chart.js 4.4.1 loaded via CDN in `extra_head`
- Pass chart data as `json.dumps()` in a `<script type="application/json" id="...">` tag, parse in JS
- Respect dark mode: read `data-theme` attribute for chart colors (`isDark ? ... : ...`)
- CSS classes from `dashboard.css`: `.page-header`, `.stat-card`, `.stat-card--accent`, `.stat-card--success`, `.stat-card--info`, `.empty-state`, `.card`, `.table-responsive`
- Empty states: use `.empty-state` div with icon, message, and CTA button
- Sidebar links: add in `base.html` under the appropriate section, use `{% if '/path/' in request.path %} active{% endif %}` for highlighting
- No JS bundling — CDN + inline `<script>` blocks

### URLs

- App namespace: `tickets` — all URL names prefixed with `tickets:` in templates
- RESTful path structure: `/resource/`, `/resource/<uuid:id>/`, `/resource/<uuid:id>/action/`
- Analytics paths: `/analytics/<feature-slug>/`
- Use `path()` with typed converters (`uuid:`, not bare strings)

### Forms

- Use `django-crispy-forms` with `crispy_bootstrap5` template pack
- ModelForm with `FormHelper` layout for consistent styling
- Validate at form level (`clean()` / `clean_<field>()`) before hitting the database

### CSV Processing

- `CSVProcessor` handles all CSV ingestion (column mapping, validation, chunked parsing)
- Chunk size: 500 rows per batch
- Customer LTV is recalculated on every import
- Duplicate detection via `order_number` uniqueness

---

## Frontend Conventions

- **Framework:** Bootstrap 5.3 (CDN) + custom `dashboard.css`
- **Icons:** Bootstrap Icons
- **Fonts:** Outfit (display), Sora (body)
- **Dark mode:** Toggle stores in `localStorage`, reads `data-theme` attribute on `<html>`
- **Charts:** Chart.js 4.4.1 — use `getContext('2d')`, responsive + `maintainAspectRatio: false`
- **Color semantics:** Blue = new/primary, Green = success/returning, Red = danger/VIP, Yellow = warning/at-risk
- **Tables:** `.table.table-hover` inside `.card-body.p-0 > .table-responsive`
- **Stat cards:** `.stat-card` with accent variants (`.stat-card--accent`, `--success`, `--info`)
- **No emojis** in UI or code unless explicitly requested
- **Third-party scripts:** Always load with `async` or `defer` — never block rendering on external domains
- **CDN libraries (Chart.js, etc.):** Load with `defer` in `{% block extra_head %}` and wrap chart initialization in `DOMContentLoaded` to ensure the library is available. Never load libraries synchronously in `<head>`.
- **Google Fonts:** Use `rel="preload"` + `media="print" onload="this.media='all'"` pattern so fonts don't block first paint. Always include `display=swap` in the font URL.
- **Keep embedded JSON payloads small** — for large datasets, consider paginating server-side or fetching via API instead of embedding everything in the HTML.

---

## Database

- **Dev:** SQLite (`db.sqlite3`)
- **Prod:** PostgreSQL via `DATABASE_URL` env var (dj-database-url, conn pooling 600s)
- All migrations committed — never edit or squash without coordinating
- Use `select_related()` / `prefetch_related()` for N+1 prevention
- Use `.values()` / `.annotate()` for aggregation queries — avoid loading full model instances when only stats are needed
- **Subquery pattern for isolated aggregation:** When annotating both `Count` (through a nested relation) and `Sum` on the same queryset, use `Subquery` for the `Sum` to prevent join inflation. See `event_list` view for the canonical example.

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SECRET_KEY` | Prod | Django secret key |
| `DATABASE_URL` | Prod | PostgreSQL connection string |
| `CELERY_BROKER_URL` | Prod | Redis URL for Celery |
| `ALLOWED_HOSTS` | Prod | Comma-separated hostnames |
| `AWS_STORAGE_BUCKET_NAME` | Optional | S3 bucket for media uploads |
| `AWS_ACCESS_KEY_ID` | With S3 | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | With S3 | AWS credentials |
| `GOOGLE_DOC_ID` | Optional | Google Docs event sync |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional | Google service account |
| `TIME_ZONE` | Optional | Default: America/Los_Angeles |

---

## Testing

- Tests live in `tickets/tests.py`
- Use `django.test.TestCase` with `setUp()` to create org, user, and test data
- Test views via Django test `Client` — assert status codes, redirects, and context
- Run: `python manage.py test tickets`

---

## Deployment (Render)

- Config in `render.yaml` — web service + Redis + Celery worker
- Build: `collectstatic` → `migrate` → `create_initial_superuser`
- Runtime: Python 3.12, Gunicorn (120s timeout)
- Static: WhiteNoise with compressed manifest
- Health check: `GET /health/`

---

## Code Style

- No linter config enforced — follow Django/PEP 8 conventions
- Imports: stdlib → Django → third-party → local (grouped with blank lines)
- Use `logger = logging.getLogger(__name__)` for logging
- Prefer explicit over clever — readable code over terse one-liners
- Keep views thin — push logic into services
- Don't over-abstract: a few repeated lines are fine; premature DRY causes coupling
