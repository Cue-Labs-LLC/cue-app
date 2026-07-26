# Technical Design Document: Cue Mobile App

**Status:** Draft
**Author:** Engineering
**Date:** 2026-03-06

---

## 1. Overview

Cue organizers currently have no mobile tool for managing attendee entry at live events,
and attendees have no native mobile experience for discovering events and managing tickets.

This document describes a cross-platform mobile app (iOS + Android) built to serve both user
types in a single codebase. **v1** ships organizer-only features. The app is architecturally
designed so that **v2 attendee features** (event discovery, ticket purchase, ticket wallet) can
be added without restructuring the project.

**v1 scope (this document):**
1. **Check-in scanning** — Organizer scans an attendee's QR code to verify and check in their order
2. **At-door sales** — Organizer sells a ticket on the spot via contactless tap-to-pay (Stripe Terminal)

**v2 scope (future, not designed here):**
1. **Event discovery** — Attendees browse public events
2. **Ticket purchase** — Attendees buy tickets in-app via Stripe
3. **Ticket wallet** — Attendees view and present their QR codes

---

## 2. Goals

- Allow organizers to scan attendee QR codes and see instant check-in status (valid / already scanned / not found)
- Allow organizers to sell tickets at the door via tap-to-pay (Apple Pay, Google Pay, contactless cards)
- Support both iOS and Android from a single codebase
- Integrate with the existing Cue Django backend (no separate backend required)
- Scope all organizer data to the organizer's organization (multi-tenancy preserved)
- **Architect the project so attendee-facing screens, API calls, and auth can be added in v2 without restructuring**

## 3. Non-Goals (v1)

- NFC wristband scanning (explicitly excluded)
- Attendee-facing features — deferred to v2
- Offline mode — internet connection required
- Self-service kiosk mode
- Analytics / reporting in the app (use the web dashboard)

---

## 4. Background

The existing Cue backend:
- Django 5.2, PostgreSQL (prod) / SQLite (dev)
- Session-based auth only (no JWT, no DRF)
- `TicketOrder` model with `order_number` (unique CharField) as the scannable identifier
- `Ticket` model (individual tickets within an order) — UUID PK, no check-in fields today
- `SaleableTicketType` model — used for direct ticket sales (has `quantity_sold`, `quantity_limit`)
- Stripe integration exists for web checkout (PaymentIntents); Stripe Terminal is not yet configured
- No mobile API layer exists

---

## 5. Architecture

The app uses a **role-based root navigator**: after login, the app routes to either the
Organizer stack or (in v2) the Attendee stack based on the `user_type` returned by the
auth endpoint. All shared infrastructure (API client, auth store, design system components)
lives at the root level, consumed by both feature trees.

API endpoints are namespaced by role (`/api/organizer/`, `/api/attendee/`) so organizer and
attendee concerns never share URL paths.

```
┌──────────────────────────────────────────┐       ┌──────────────────────────────────────┐
│           React Native App               │       │       Django Backend                  │
│       (iOS + Android, TypeScript)        │       │                                       │
│                                          │       │  tickets/api_views.py (new)           │
│  ┌── Auth ──────────────────────────┐   │       │  tickets/api_urls.py  (new)           │
│  │  LoginScreen (shared)            │──────────▶│  POST /api/auth/login/                │
│  └──────────────────────────────────┘   │       │                                       │
│                                          │       │  ── Organizer namespace ─────────     │
│  ┌── Organizer Stack (v1) ──────────┐   │       │  GET  /api/organizer/events/          │
│  │  EventListScreen                 │──────────▶│  GET  /api/organizer/events/<id>/     │
│  │  ScannerScreen                   │──────────▶│       ticket-types/                   │
│  │  CheckinResultScreen             │──────────▶│  POST /api/organizer/checkin/         │
│  │  SellScreen                      │──────────▶│  POST /api/organizer/sell/            │
│  │  PaymentScreen (Stripe Terminal) │──────────▶│  POST /api/stripe/connection-token/   │
│  │  SaleConfirmationScreen          │──────────▶│  POST /api/stripe/terminal-pi/        │
│  └──────────────────────────────────┘   │       │                                       │
│                                          │       │  ── Attendee namespace (v2) ──────    │
│  ┌── Attendee Stack (v2, future) ───┐   │       │  GET  /api/attendee/events/           │
│  │  EventDiscoveryScreen            │   │       │  POST /api/attendee/purchase/         │
│  │  EventDetailScreen               │   │       │  GET  /api/attendee/my-tickets/       │
│  │  PurchaseScreen                  │   │       │                                       │
│  │  TicketWalletScreen              │   │       └──────────────────────────────────────┘
│  └──────────────────────────────────┘   │                      │
└──────────────────────────────────────────┘             ┌───────▼──────┐
                                                          │    Stripe    │
                                                          │   Terminal   │
                                                          └──────────────┘
```

---

## 6. Data Model Changes

### 6.1 `TicketOrder` (tickets/models.py)

Add two nullable fields to support check-in tracking:

```python
checked_in_at = models.DateTimeField(null=True, blank=True, db_index=True)
checked_in_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    null=True, blank=True,
    on_delete=models.SET_NULL,
    related_name='checkins'
)
```

**Rationale:** Check-in is per-order (one scan admits all tickets in the order). Storing on
`TicketOrder` is simpler than per-`Ticket` and matches the QR code scope (one QR per order).

**Migration:** Standard `makemigrations && migrate`. No data migration required (existing orders
will have `checked_in_at=NULL`, treated as not yet checked in).

### 6.2 QR Code Display (no new model)

QR codes encode `TicketOrder.order_number` (plain string). Generated server-side using the
`qrcode` Python library and embedded as a base64 PNG inline in HTML. No new DB fields needed.

---

## 7. API Design

### Authentication

All mobile API endpoints use **token authentication** via `djangorestframework` + `rest_framework.authtoken`.

- Login returns a `Token` keyed to the Django `User`
- Token sent as `Authorization: Token <token>` header on all subsequent requests
- Tokens do not expire (can add expiry later if needed)
- All organizer endpoints verify the token's user has an associated `UserProfile.organization`
- **v2 note:** Attendees authenticate via OTP (Django already has `PhoneOTP`/`EmailOTP` models and
  `unified_login_view`/`unified_verify_view` views). The attendee login flow will use a separate
  `POST /api/auth/otp/request/` + `POST /api/auth/otp/verify/` endpoint pair that wraps the
  existing OTP logic and returns the same token format so the app's auth store needs no changes.

### `user_type` field

The login response includes `user_type: "organizer" | "attendee"`. The root navigator uses this
to decide which stack to render. Organizers always have `UserProfile.organization`; attendees
may or may not. The app stores `user_type` in the auth store alongside the token.

### Endpoints

#### `POST /api/auth/login/`

**Auth:** None (public) — organizer email+password flow

Request:
```json
{ "email": "organizer@example.com", "password": "..." }
```

Response `200`:
```json
{
  "token": "abc123...",
  "user_type": "organizer",
  "user_name": "Jane Smith",
  "org_name": "Acme Events",
  "org_id": "uuid..."
}
```

Response `400`: `{ "error": "Invalid credentials" }`

---

#### `GET /api/organizer/events/`

**Auth:** Token required (organizer)

Returns events for the organizer's organization. Both direct and external (CSV-imported) events are included.

**Query params:**

| Param | Values | Default | Behavior |
|-------|--------|---------|----------|
| `status` | `upcoming` \| `past` \| `all` | `upcoming` | `upcoming` = `start_date` today or later, soonest-first (unchanged historical behavior). `past` = ended events (`start_date` before today), newest-first — use this to review scan data of previous events. `all` = everything, newest-first. Any unrecognized value falls back to `upcoming`. |

Response `200`:
```json
[
  {
    "id": "uuid...",
    "name": "Summer Gala 2026",
    "start_date": "2026-06-15",
    "start_time": "19:00:00",
    "status": "upcoming",
    "ticketing_type": "direct",
    "venue": "Rooftop Terrace",
    "city": "Portland",
    "total_tickets": 250,
    "checked_in_count": 47,
    "total_revenue": "6250.00"
  }
]
```

- `start_date` is a date-only ISO string; `start_time` is a separate time-only ISO string (or `null`).
- `status` is per-event: `"upcoming"` if `start_date` is today or later, else `"past"`.
- `ticketing_type` is `"direct"` (sold in-app) or `"external"` (CSV-imported). External events have no in-app sell/Tap-to-Pay path — the client should render them as review-only.
- `checked_in_count` = count of `Ticket` records with `scanned_at__isnull=False` for this event (per-ticket, the single source of truth — matches the web dashboard and the check-in-stats endpoints; counts live scans and CSV-imported scans, including partially-scanned orders).
- `total_tickets` = count of `Ticket` records for this event (via order relationship). Directly comparable to `checked_in_count`.
- `total_revenue` = sum of non-refunded `TicketOrder.total_amount` (stringified decimal).

---

#### `GET /api/organizer/events/<uuid:event_id>/ticket-types/`

**Auth:** Token required (organizer)

Returns active `SaleableTicketType` records for at-door sales.

Response `200`:
```json
[
  {
    "id": "uuid...",
    "name": "General Admission",
    "price": "25.00",
    "remaining": 53
  }
]
```

`remaining` = `quantity_limit - quantity_sold` (null if no limit).
Filters: `is_active=True`, `is_on_sale()=True`, excludes password-protected types.

---

#### `POST /api/organizer/checkin/`

**Auth:** Token required (organizer)

Request:
```json
{
  "order_number": "EF-00123",
  "event_id": "uuid..."
}
```

Response `200` (first check-in):
```json
{
  "status": "checked_in",
  "order_number": "EF-00123",
  "customer_name": "John Doe",
  "ticket_count": 2,
  "ticket_types": ["General Admission", "General Admission"],
  "checked_in_at": "2026-06-15T19:43:11Z"
}
```

Response `200` (already checked in):
```json
{
  "status": "already_checked_in",
  "order_number": "EF-00123",
  "customer_name": "John Doe",
  "ticket_count": 2,
  "checked_in_at": "2026-06-15T19:30:00Z"
}
```

Response `404`: `{ "status": "not_found" }`

Response `400` (refunded order): `{ "status": "refunded" }`

**Implementation notes:**
- Scope lookup: `TicketOrder.objects.filter(organization=org, event_id=event_id, order_number=order_number)`
- Use `select_for_update()` in a transaction to prevent double check-in race condition
- Set `checked_in_at = timezone.now()`, `checked_in_by = request.user`

---

#### `POST /api/stripe/connection-token/`

**Auth:** Token required (organizer)

Called on app launch to initialize Stripe Terminal SDK.

Response `200`:
```json
{ "secret": "pctx_..." }
```

Implementation calls `stripe.terminal.connection_tokens.create()`.

---

#### `POST /api/stripe/terminal-payment-intent/`

**Auth:** Token required (organizer)

Request:
```json
{
  "event_id": "uuid...",
  "line_items": [
    { "ticket_type_id": "uuid...", "quantity": 2 }
  ]
}
```

Response `200`:
```json
{
  "client_secret": "pi_xxx_secret_xxx",
  "payment_intent_id": "pi_xxx",
  "amount_cents": 5000,
  "currency": "usd"
}
```

Creates a Stripe `PaymentIntent` with `payment_method_types: ["card_present"]` and
`capture_method: "automatic"`. Applies platform fee if configured.

---

#### `POST /api/organizer/sell/`

**Auth:** Token required (organizer)

Called after Stripe Terminal payment succeeds to record the sale in Django.

Request:
```json
{
  "event_id": "uuid...",
  "payment_intent_id": "pi_xxx",
  "buyer_name": "Walk-in Attendee",
  "buyer_email": "walkin@example.com",
  "line_items": [
    { "ticket_type_id": "uuid...", "name": "General Admission", "price": "25.00", "quantity": 2 }
  ]
}
```

Response `201`:
```json
{
  "order_number": "EF-00456",
  "order_id": "uuid...",
  "total_amount": "50.00",
  "ticket_count": 2
}
```

Implementation:
- Verify `PaymentIntent` status with Stripe API before creating order
- Create `Customer` (get_or_create by email + org)
- Create `TicketOrder` with `is_in_person=True`
- Create `Ticket` records for each line item
- Increment `SaleableTicketType.quantity_sold`
- Mark order as checked in immediately (`checked_in_at = timezone.now()`)

---

## 8. QR Code Generation

### Library
`qrcode[pil]` (add to `requirements.txt`)

### Where displayed
1. `checkout_success.html` — shown after web purchase
2. `my_tickets.html` — for authenticated buyers reviewing past orders

### Implementation
In the view, generate a base64 PNG:
```python
import qrcode, io, base64

def generate_qr_b64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()
```

Pass `qr_code = generate_qr_b64(order.order_number)` in context.
Template: `<img src="data:image/png;base64,{{ qr_code }}" alt="Ticket QR Code" width="200">`

---

## 9. React Native App

### 9.1 Project Setup

The project uses a **feature-based folder structure** so organizer and attendee code are
fully separated, share a common API client and auth store, and can evolve independently.

```
cueup-app/                  # Separate git repo (renamed from cueup-organizer)
├── src/
│   ├── api/
│   │   ├── client.ts           # Axios instance — base URL + token interceptor (shared)
│   │   ├── organizer.ts        # Typed API calls for /api/organizer/* endpoints
│   │   └── attendee.ts         # Typed API calls for /api/attendee/* endpoints (v2, stub)
│   │
│   ├── store/
│   │   └── useAuthStore.ts     # Zustand: token, user_type, user_name, org (shared)
│   │
│   ├── navigation/
│   │   ├── RootNavigator.tsx   # Role-based root: reads user_type → OrganizerStack | AttendeeStack
│   │   ├── AuthStack.tsx       # LoginScreen (shared pre-auth)
│   │   ├── OrganizerStack.tsx  # Organizer tab navigator (v1)
│   │   └── AttendeeStack.tsx   # Attendee tab navigator (v2 stub — placeholder screen)
│   │
│   ├── features/
│   │   ├── organizer/          # All organizer screens + components
│   │   │   ├── EventListScreen.tsx
│   │   │   ├── ScannerScreen.tsx
│   │   │   ├── CheckinResultScreen.tsx
│   │   │   ├── SellScreen.tsx
│   │   │   ├── PaymentScreen.tsx
│   │   │   ├── SaleConfirmationScreen.tsx
│   │   │   └── components/
│   │   │       ├── CheckinBadge.tsx    # Green / yellow / red overlay
│   │   │       └── TicketTypeRow.tsx
│   │   │
│   │   └── attendee/           # All attendee screens + components (v2 stubs)
│   │       ├── EventDiscoveryScreen.tsx  # Placeholder
│   │       ├── EventDetailScreen.tsx     # Placeholder
│   │       ├── PurchaseScreen.tsx        # Placeholder
│   │       └── TicketWalletScreen.tsx    # Placeholder
│   │
│   └── components/             # Truly shared UI primitives
│       ├── Button.tsx
│       └── LoadingSpinner.tsx
│
├── android/
├── ios/
└── package.json
```

**Key architectural decisions:**
- `RootNavigator` is the only place that reads `user_type` — no role checks scattered in screens
- `OrganizerStack` and `AttendeeStack` are fully self-contained; adding v2 attendee screens
  means only modifying `AttendeeStack.tsx` and `src/features/attendee/`
- `useAuthStore` stores `user_type` so the navigator re-renders automatically on login/logout
- `src/api/attendee.ts` is created as a stub in v1 so the import structure is established

### 9.2 Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `react-native` | 0.74+ | Framework |
| `typescript` | 5.x | Language |
| `@stripe/stripe-terminal-react-native` | latest | Tap-to-pay |
| `react-native-vision-camera` | 4.x | Camera |
| `vision-camera-code-scanner` | latest | QR detection (MLKIT/ZXing) |
| `@react-navigation/native` | 6.x | Navigation |
| `@react-navigation/stack` | 6.x | Stack navigator |
| `axios` | 1.x | HTTP client |
| `zustand` | 4.x | State management |
| `react-native-keychain` | latest | Secure token storage |

### 9.3 Navigation Structure

```
RootNavigator (reads useAuthStore.user_type)
│
├── AuthStack              (user_type == null — not logged in)
│   └── LoginScreen
│
├── OrganizerStack         (user_type == "organizer")
│   ├── Tab: Events
│   │   └── EventListScreen
│   ├── Tab: Scan
│   │   ├── ScannerScreen
│   │   └── CheckinResultScreen
│   └── Tab: Sell
│       ├── SellScreen
│       ├── PaymentScreen
│       └── SaleConfirmationScreen
│
└── AttendeeStack          (user_type == "attendee" — v2 stub in v1)
    ├── Tab: Discover       → EventDiscoveryScreen (placeholder)
    ├── Tab: My Tickets     → TicketWalletScreen   (placeholder)
    └── Tab: Account        → AccountScreen        (placeholder)
```

`RootNavigator` is a simple switch:
```typescript
const { user_type } = useAuthStore();
if (!user_type) return <AuthStack />;
if (user_type === 'organizer') return <OrganizerStack />;
return <AttendeeStack />;  // v2
```

### 9.4 Check-in Scanner UX

- Full-screen camera with a centered scan target rectangle
- QR detected → vibrate + pause camera → show `CheckinBadge` overlay
  - **Green:** "Checked In — John Doe, 2 tickets" (dismiss after 2s, resume scanning)
  - **Yellow:** "Already Scanned — 7:30 PM" (dismiss after 3s, resume scanning)
  - **Red:** "Not Found" or "Refunded" (dismiss after 3s)
- Manual entry fallback: text input for `order_number` at bottom of screen

### 9.5 At-Door Sales UX

1. `SellScreen`: FlatList of `SaleableTicketType` rows with `+/-` quantity controls and running total
2. Tap "Charge $XX.XX" → `PaymentScreen`
3. `PaymentScreen`: "Hold near reader" prompt, Stripe Terminal handles tap detection
4. On success: `SaleConfirmationScreen` with order number

### 9.6 Stripe Terminal Initialization

```typescript
// On app launch (after login), initialize Terminal
const { initialize } = useStripeTerminal();

await initialize({
  fetchConnectionToken: async () => {
    const res = await api.post('/api/stripe/connection-token/');
    return res.data.secret;
  },
  onUnexpectedReaderDisconnect: () => {
    // Show toast: "Reader disconnected"
  },
});
```

For iOS + Android development/testing, use Stripe's **simulated reader** (no hardware needed).

---

## 10. Security Considerations

- **Token storage:** Use `react-native-keychain` (Keychain on iOS, Keystore on Android) — never `AsyncStorage`
- **Multi-tenancy:** All API views call `get_organization(request)` and scope queries — same pattern as web views
- **Check-in race condition:** `select_for_update()` in atomic transaction prevents double check-in if two organizers scan simultaneously
- **Stripe webhook:** At-door sales bypass the webhook flow — Django records the order directly after the app confirms Terminal payment success. Verify `PaymentIntent` status with Stripe API server-side before creating the order.
- **Refunded orders:** Check `TicketOrder.refunded_at` in checkin endpoint and return `status: "refunded"`
- **HTTPS only:** API base URL must be HTTPS in production (Render deployment uses HTTPS by default)

---

## 11. New Files Summary

### Backend (Django project)
| File | Description |
|------|-------------|
| `tickets/api_views.py` | All mobile API views |
| `tickets/api_urls.py` | URL patterns for `/api/` prefix |

### Modified Files (Django)
| File | Change |
|------|--------|
| `requirements.txt` | Add `djangorestframework`, `qrcode[pil]` |
| `tickets/models.py` | Add `checked_in_at`, `checked_in_by` to `TicketOrder` |
| `ltv_updater/settings.py` | Add `rest_framework`, `rest_framework.authtoken` to INSTALLED_APPS; add `REST_FRAMEWORK` config; add `STRIPE_TERMINAL_LOCATION_ID` env var |
| `ltv_updater/urls.py` | Add `path('api/', include('tickets.api_urls'))` |
| `tickets/views.py` | Update `checkout_success` and `my_tickets` to pass `qr_code` in context |
| `tickets/templates/tickets/checkout_success.html` | Display QR code image |
| `tickets/templates/tickets/my_tickets.html` | Display QR code per order |

### New Repo (React Native)
| Directory/File | Description |
|----------------|-------------|
| `cueup-app/` | New React Native project (separate repo, replaces `cueup-organizer/` name) |

---

## 12. Testing Strategy

### Backend
- Unit tests in `tickets/tests.py`:
  - `test_checkin_valid_order` — POST valid order_number, assert `checked_in_at` set
  - `test_checkin_already_scanned` — POST twice, assert `status: already_checked_in`
  - `test_checkin_wrong_org` — POST order from different org, assert 404
  - `test_checkin_refunded` — POST refunded order, assert `status: refunded`
  - `test_sell_creates_order` — POST `/api/sell/`, assert `TicketOrder` + `Ticket` records created
  - `test_token_auth_required` — unauthenticated request returns 401

### React Native
- Jest + React Native Testing Library for component/screen unit tests
- Stripe Terminal simulated reader for end-to-end payment flow in dev

### Manual Verification
1. Create test order via web → confirm QR on success page → decode QR = `order_number`
2. Use app to scan QR → confirm green check-in result → DB `checked_in_at` set
3. Scan same QR again → confirm yellow "already checked in" with timestamp
4. Scan unknown order_number → confirm red "not found"
5. Use Stripe simulated reader: sell a ticket at door → confirm `TicketOrder` in Django admin

---

## 13. Open Questions / Risks

| Question | Notes |
|----------|-------|
| Does the organizer's account need a special permission/role, or can any staff user log in? | Current model: any user with `UserProfile.organization` can use the organizer stack. May want an `is_organizer` flag in v2 when attendees also have accounts. |
| How will attendee auth work in v2? | Django already has `PhoneOTP`/`EmailOTP` models and `unified_login_view`. The v2 attendee login will wrap these into `/api/auth/otp/request/` + `/api/auth/otp/verify/` endpoints returning the same token format. No auth store changes needed. |
| How will attendee ticket discovery work — public events only, or org-specific? | Not designed yet. The `/api/attendee/events/` endpoint will need a public events filter. |
| Should at-door sales skip Stripe for cash payments? | Not in scope for v1. Can add a "cash sale" path later. |
| What happens when `SaleableTicketType.quantity_limit` is reached? | API returns 400 with `sold_out` status. App shows error. |
| Email confirmation for at-door sales? | Not in scope for v1. Optional in v2. |
| App distribution? | TestFlight (iOS) + Google Play internal track for initial rollout. |
