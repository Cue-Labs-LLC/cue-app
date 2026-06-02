# Staging Environment

Cue runs a parallel `-staging` stack on Render so we can validate changes against a real Postgres + Redis + Stripe Connect before promoting to production. Production lives on `main`; staging lives on a long-lived `staging` branch you fast-forward from `main` on demand.

---

## Architecture

| Component | Production | Staging |
|---|---|---|
| Web | `ltv-updater` (pro plan) | `ltv-updater-staging` (starter) |
| Worker | `ltv-updater-worker` (standard) | `ltv-updater-worker-staging` (starter) |
| Redis | `ltv-updater-redis` (standard) | `ltv-updater-redis-staging` (starter) |
| Cron | `ltv-updater-ai-opportunities` | — (skipped to avoid duplicate OpenAI cost) |
| Postgres | Manual, prod DB | Manual, separate staging DB |
| S3 bucket | Prod bucket | Separate staging bucket (or unset for local storage) |
| Stripe | Live mode | Test mode (separate keys + webhook) |
| Branch | `main` | `staging` |
| Domain | `app.cueup.co` | `staging.cueup.co` |

All staging services are defined in `render.yaml` alongside production.

---

## One-time setup

### 1. Create the staging branch

```bash
git checkout main
git pull
git checkout -b staging
git push -u origin staging
```

### 2. Apply the Blueprint in Render

In the Render dashboard, re-sync the Blueprint (`render.yaml`). Render will detect the new services (`ltv-updater-staging`, `ltv-updater-worker-staging`, `ltv-updater-redis-staging`) and create them. The web/worker will fail their first build until the manual env vars below are set — that's expected.

### 3. Create the staging Postgres

In Render → New → PostgreSQL. Name it `ltv-updater-db-staging`, use the cheapest plan that fits, same region as the web service. Copy the **Internal Database URL**.

Paste it into `ltv-updater-staging` and `ltv-updater-worker-staging` as `DATABASE_URL`.

### 4. Set the staging domain

In Render → `ltv-updater-staging` → Settings → Custom Domains, add `staging.cueup.co`. Add the CNAME at your DNS provider (Render shows the target).

Set on `ltv-updater-staging` and `ltv-updater-worker-staging`:

| Var | Value |
|---|---|
| `SITE_URL` | `https://staging.cueup.co` |
| `ALLOWED_HOSTS` | `staging.cueup.co` |
| `CSRF_TRUSTED_ORIGINS` | `https://staging.cueup.co` |

### 5. Stripe Connect — test mode

In Stripe Dashboard, toggle to **test mode** (top right).

- Copy `STRIPE_SECRET_KEY` (starts with `sk_test_…`) and `STRIPE_PUBLISHABLE_KEY` (`pk_test_…`).
- Create a webhook endpoint pointing at `https://staging.cueup.co/stripe/webhook/` (or wherever your prod webhook URL maps) — copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
- If you use Connect connected-account webhooks, repeat for that endpoint and set `STRIPE_CONNECTED_ACCOUNT_WEBHOOK_SECRET`.

Stripe Connect test mode shares your platform account but creates separate test connected accounts — sellers will need to onboard a test account on staging (use Stripe's test SSN/routing numbers).

### 6. Other per-env credentials

Set on both staging services as needed:

| Var | Notes |
|---|---|
| `SENDGRID_API_KEY` | Either a separate SendGrid key (recommended) or reuse prod's; staging will send real emails either way, so prefer a sandboxed key or a verified sender like `staging@cueup.co`. |
| `DEFAULT_FROM_EMAIL` | e.g. `staging@cueup.co` |
| `AWS_*` | Either create `cue-staging` S3 bucket, or **unset all four `AWS_*` vars** to fall back to local disk storage on the dyno (uploads won't persist across deploys but it works for QA). |
| `OPENAI_API_KEY` | Reuse prod, or a separate key with a low spend cap. |
| `GOOGLE_MAPS_API_KEY` | Reuse prod; restrict the key to `staging.cueup.co` and `*.cueup.co`. |
| `TWILIO_*` | Reuse prod; staging will send real SMS. Set `E2E_TEST_MODE=True` if you want to skip Twilio entirely. |
| `FACEBOOK_*`, `MAILCHIMP_*` | Create separate dev apps in those platforms, or reuse with separate redirect URIs. |
| `SENTRY_DSN` | Reuse prod — Sentry events will be tagged `environment=staging` thanks to `DJANGO_ENV`. |

### 7. First deploy + seed

Once env vars are set, trigger a manual deploy of `ltv-updater-staging`. The build will run `migrate` and `create_initial_superuser`. Then open the Render shell on the staging web service and seed demo data:

```bash
python manage.py seed_staging --force
```

The seed command refuses to run unless `DJANGO_ENV=staging` — defense in depth so it cannot ever fire on prod.

Default login (from seed):
- Email: `info@cueup.co` / `password123`

---

## Daily workflow

### Promote main → staging

Staging is fast-forwarded from `main` on demand:

```bash
git checkout staging
git fetch origin
git merge --ff-only origin/main
git push
```

If the fast-forward fails, someone has pushed directly to staging — investigate before forcing. Avoid `--force` pushes to `staging`.

### Re-seed a fresh staging DB

```bash
# In Render shell on ltv-updater-staging
python manage.py flush --no-input          # drop all data, keep schema
python manage.py seed_staging --force
```

Or drop and recreate the Postgres DB from the Render dashboard if migrations are in a weird state.

### Verify before promoting staging → prod

After QA on `staging.cueup.co` passes, merge `staging` → `main` (or merge your feature PR to `main` directly — `staging` is a snapshot, not the source of truth):

```bash
git checkout main
git merge --ff-only staging   # or just merge the PR
git push
```

---

## Gotchas

- **Stripe webhooks are environment-scoped.** Test-mode webhooks only fire in test mode, live in live. Don't mix.
- **Apple Pay domain association.** If you re-enable Apple Pay on staging, you'll need a separate `APPLE_PAY_DOMAIN_ASSOCIATION` file hosted at `https://staging.cueup.co/.well-known/apple-developer-merchantid-domain-association` registered with Stripe.
- **Background tasks run on staging too.** Survey emails, OTP, etc. will fire for any customer records you create. Don't seed real emails — the seed command uses `@example.test` addresses (which can't deliver) by design.
- **Sentry quota.** Staging shares the prod DSN. If staging spams errors, prod's quota suffers. Tag-based filtering on `environment` lets you mute staging alerts.
- **Cron skipped on staging.** The AI opportunities scan only runs on prod. Test it on staging manually: `python manage.py generate_ai_opportunities`.
