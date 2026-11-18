"""
Recent ticket demand per market: average tickets sold per event.

For a chosen window of event dates, every non-cancelled event already held
(start_date <= today) counts as one event in its market. Each event's tickets
are counted with an isolated Subquery, so there is no join inflation. Events
that sold nothing stay in the denominator. The same numbers are computed for
the immediately preceding window of equal length, so each market shows how
demand moved.

The ticket filter (all / paid / free) narrows which tickets are counted, by
per-ticket price ($0 = free). It never changes which events count, so "avg paid
tickets per event" stays comparable across markets: a free-only show counts as
0 paid tickets rather than dropping out.

In-person orders and refunded orders are counted, matching the tickets metric
on Market Trends.

Ad spend is each event's marketing expenses (manual + Meta Ads lines, both
category='marketing'), summed per market. It is not affected by the ticket
filter: spend is per event, not per ticket.

Spend-adjusted demand
---------------------
Raw ticket counts mix organic demand with what ads bought: two shows that each
sold 200 tickets aren't equally in demand if one needed $1,000 of ads and the
other $100. The spend-adjusted view estimates what each event would have sold
at the org's typical ad spend:

    shift    = beta * (ln(1 + spend) - ln(1 + typical_spend))
    adjusted = clamp(sold - shift, sold * (1 - CAP), sold * (1 + CAP))

beta is fit per org (see ``fit_spend_effect``) from its own events over the last
few years: the within-market slope of tickets on ln(1 + spend). ln gives
diminishing returns (each doubling of spend adds ~beta * ln 2 tickets); fitting
within each market keeps big markets, which naturally get bigger budgets, from
inflating the effect.

typical_spend is the org's median ad spend per event over the same history.
Comparing against it (not $0) keeps the estimate inside the range of spend the
org actually uses: when every event spends thousands, "sales at $0" is pure
extrapolation of the log curve and wipes most events to zero. Events that spent
more than typical are docked, less than typical are credited. The shift is
capped at +/-CAP of the event's raw sales as a backstop against a beta inflated
by bigger shows getting bigger budgets. With too little history (or no spend
variation) the model is unavailable and the page shows raw counts.

Reducing size bias in beta: bigger shows tend to get bigger budgets AND sell
more, which a naive fit credits to ads. Two guards:

  * Venue fixed effects -- events are compared within the same venue (same
    room size) whenever that venue hosted 2+ of the fit events, and within the
    market otherwise.
  * Capacity control -- ln(capacity) (the event's, else its venue's) is a
    second regressor, so a bigger room or ticket allotment with a bigger budget
    isn't credited to the ads. Used when >= 80% of fit events have a capacity.
  * Meta attribution ceiling -- Meta's reported purchases over-credit ads
    (view-through etc.), so they are an upper bound on what ads caused. When
    enough fit events carry Meta-attributed purchases and beta implies ads drove
    more tickets than Meta claims for those events, beta is scaled down to match.
"""
import math
from datetime import date, timedelta

from django.db.models import Count, IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from tickets.models import EVENT_STATUS_CANCELLED, Event, EventExpense, Ticket, TicketOrder
from tickets.services.conversion import _marketing_spend_expr, _stats
from tickets.services.markets import NO_MARKET_LABEL


TICKET_FILTER_ALL = 'all'
TICKET_FILTER_PAID = 'paid'
TICKET_FILTER_FREE = 'free'
VALID_TICKET_FILTERS = (TICKET_FILTER_ALL, TICKET_FILTER_PAID, TICKET_FILTER_FREE)

DEMAND_MODE_RAW = 'raw'
DEMAND_MODE_ADJUSTED = 'adjusted'
VALID_DEMAND_MODES = (DEMAND_MODE_RAW, DEMAND_MODE_ADJUSTED)

# Spend-effect model inputs: how far back to learn from, and the minimum history
# needed before the estimate is trusted enough to show.
SPEND_MODEL_LOOKBACK_YEARS = 3
SPEND_MODEL_MIN_EVENTS = 15
SPEND_MODEL_MIN_SPEND_EVENTS = 5
# Max share of an event's raw sales the spend adjustment may add or remove.
SPEND_ADJUSTMENT_CAP = 0.5
# Fit events carrying Meta-attributed purchases needed before they cap beta.
SPEND_MODEL_MIN_CALIBRATION_EVENTS = 5
# Share of fit events that must have a capacity before it's used as a control.
SPEND_MODEL_CAPACITY_COVERAGE = 0.8


def normalize_ticket_filter(value):
    """Return a valid ticket filter key, falling back to 'all'."""
    return value if value in VALID_TICKET_FILTERS else TICKET_FILTER_ALL


def normalize_demand_mode(value):
    """Return a valid demand mode, defaulting to spend-adjusted."""
    return value if value in VALID_DEMAND_MODES else DEMAND_MODE_ADJUSTED


def _pct_change(current, prior):
    """Percent change from prior to current, or None when there is no usable prior."""
    if current is None or not prior:
        return None
    return round((current - prior) / prior * 100, 1)


def _spend_x(spend):
    return math.log1p(max(float(spend), 0.0))


def _fe_key(row, venue_counts):
    """Fixed-effect group: the venue when it hosted 2+ fit events, else the market."""
    venue = row.get('venue_id')
    if venue is not None and venue_counts.get(venue, 0) >= 2:
        return ('venue', venue)
    return ('market', row['market_id'])


def fit_spend_effect(rows):
    """Estimate how many tickets ad spend buys per event, from past event rows.

    Each row needs 'market_id', 'sold' and 'ad_spend'; optional 'venue_id',
    'fit_capacity', 'orders' and 'meta_attributed_orders' enable the size-bias
    guards. Fits ``sold = group baseline + beta * ln(1 + spend)
    [+ gamma * ln(capacity)]`` by OLS with group fixed
    effects (the "within" estimator: demean x and y inside each group, then
    regress), where a group is the venue when it repeats, else the market.
    Returns a dict with:

      available  -- True when there is enough history to trust beta
      beta       -- tickets per unit of ln(1 + spend), clamped >= 0 and capped
                    by Meta-attributed purchases when available
      tickets_per_doubling -- beta * ln 2, the plain-language effect size
      reference_spend -- median ad spend per event (all rows, $0 included):
                         the "typical spend" adjusted sales are normalized to
      venue_events -- fit events compared within their venue (vs. market)
      capacity_controlled -- True when ln(capacity) entered the fit as a control
      calibrated -- True when the Meta ceiling lowered beta
      uncalibrated_beta -- beta before the Meta ceiling
      events / spend_events -- sample sizes behind the fit
      reason     -- why it's unavailable ('' when available)
    """
    events = len(rows)
    spend_events = sum(1 for r in rows if r['ad_spend'] and r['ad_spend'] > 0)
    spends = sorted(max(float(r['ad_spend'] or 0), 0.0) for r in rows)
    reference_spend = 0.0
    if spends:
        mid = len(spends) // 2
        reference_spend = spends[mid] if len(spends) % 2 else (spends[mid - 1] + spends[mid]) / 2
    result = {
        'available': False, 'beta': 0.0, 'tickets_per_doubling': 0.0,
        'reference_spend': round(reference_spend, 2),
        'venue_events': 0, 'capacity_controlled': False,
        'calibrated': False, 'uncalibrated_beta': 0.0,
        'events': events, 'spend_events': spend_events, 'reason': '',
    }
    if events < SPEND_MODEL_MIN_EVENTS or spend_events < SPEND_MODEL_MIN_SPEND_EVENTS:
        result['reason'] = 'not_enough_history'
        return result

    # Capacity control: used when (nearly) every fit event has a capacity. Rows
    # missing one are left out of the fit rather than guessed.
    with_capacity = [r for r in rows if r.get('fit_capacity') and r['fit_capacity'] > 0]
    use_capacity = len(with_capacity) >= SPEND_MODEL_CAPACITY_COVERAGE * len(rows)
    fit_rows = with_capacity if use_capacity else rows

    venue_counts = {}
    for r in fit_rows:
        if r.get('venue_id') is not None:
            venue_counts[r['venue_id']] = venue_counts.get(r['venue_id'], 0) + 1

    groups = {}
    for r in fit_rows:
        key = _fe_key(r, venue_counts)
        z = math.log(r['fit_capacity']) if use_capacity else 0.0
        groups.setdefault(key, []).append((_spend_x(r['ad_spend']), z, float(r['sold'])))
    result['venue_events'] = sum(len(pts) for key, pts in groups.items() if key[0] == 'venue')

    # Within-group sums of squares / cross-products for y ~ x (+ z), where
    # x = ln(1 + spend) and z = ln(capacity).
    sxx = sxz = szz = sxy = szy = 0.0
    capacity_varies = False
    for points in groups.values():
        if len(points) < 2:
            continue  # a single-event group says nothing about spend within it
        n = len(points)
        zs = [p[1] for p in points]
        if max(zs) - min(zs) > 1e-9:
            capacity_varies = True
        x_mean = sum(p[0] for p in points) / n
        z_mean = sum(p[1] for p in points) / n
        y_mean = sum(p[2] for p in points) / n
        for x, z, y in points:
            dx, dz, dy = x - x_mean, z - z_mean, y - y_mean
            sxx += dx * dx
            sxz += dx * dz
            szz += dz * dz
            sxy += dx * dy
            szy += dz * dy
    if sxx <= 0:
        # Spend never varies within a venue/market, so its effect can't be
        # separated from the venue/market itself.
        result['reason'] = 'no_spend_variation'
        return result

    det = sxx * szz - sxz * sxz
    if use_capacity and capacity_varies and det > 1e-9 * sxx * szz:
        # Two-regressor OLS: spend's effect with capacity held constant, so
        # bigger rooms getting bigger budgets aren't credited to the ads.
        raw_beta = (szz * sxy - sxz * szy) / det
        result['capacity_controlled'] = True
    else:
        # Capacity doesn't vary within venues/markets (already absorbed by the
        # fixed effects) or isn't available: single-regressor fit.
        raw_beta = sxy / sxx

    # Ads can't plausibly reduce sales; a negative slope means spend tracks
    # weak shows (organizers boosting slow sellers), so treat it as no effect.
    beta = max(raw_beta, 0.0)
    result['uncalibrated_beta'] = beta

    # Meta ceiling: what the model says ads drove (vs. $0) on events where Meta
    # reported purchases must not exceed what Meta itself claims.
    calib = [
        r for r in rows
        if r.get('meta_attributed_orders') is not None and r['ad_spend'] and r['ad_spend'] > 0
    ]
    if beta > 0 and len(calib) >= SPEND_MODEL_MIN_CALIBRATION_EVENTS:
        claimed = 0.0
        for r in calib:
            orders = r.get('orders') or 0
            tickets_per_order = float(r['sold']) / orders if orders else 1.0
            claimed += r['meta_attributed_orders'] * tickets_per_order
        implied = sum(beta * _spend_x(r['ad_spend']) for r in calib)
        if implied > claimed:
            beta *= claimed / implied
            result['calibrated'] = True

    result.update(
        available=True,
        beta=beta,
        tickets_per_doubling=round(beta * math.log(2), 1),
    )
    return result


def _order_count_expr():
    """Orders per event, as an isolated Subquery (for tickets-per-order)."""
    return Subquery(
        TicketOrder.objects.filter(event=OuterRef('pk'))
        .order_by().values('event').annotate(c=Count('id')).values('c')[:1],
        output_field=IntegerField(),
    )


def _meta_attributed_orders_expr():
    """Meta-reported purchases across an event's marketing expenses (NULL when
    no expense carries Meta API attribution)."""
    return Subquery(
        EventExpense.objects.filter(
            event=OuterRef('pk'), category='marketing', deleted_at__isnull=True,
            api_attributed_orders__isnull=False,
        )
        .order_by().values('event').annotate(t=Sum('api_attributed_orders')).values('t')[:1],
        output_field=IntegerField(),
    )


def adjusted_sold(sold, ad_spend, beta, reference_spend=0.0, cap=SPEND_ADJUSTMENT_CAP):
    """Estimated tickets an event would have sold at the reference (typical) spend.

    Spending above the reference docks tickets, below it credits them. The shift
    is capped at +/- ``cap`` of the event's raw sales (so it also never goes
    negative, and an event that sold nothing stays at 0).
    """
    sold = float(sold)
    shift = beta * (_spend_x(ad_spend) - _spend_x(reference_spend))
    limit = sold * cap
    return sold - max(-limit, min(limit, shift))


class MarketDemandCalculator:
    def __init__(self, organization, start_date, end_date, ticket_filter=TICKET_FILTER_ALL,
                 demand_mode=DEMAND_MODE_ADJUSTED):
        self.organization = organization
        self.ticket_filter = normalize_ticket_filter(ticket_filter)
        self.demand_mode = normalize_demand_mode(demand_mode)
        today = date.today()
        self.today = today
        # Upcoming events haven't finished selling; clamp the window to today.
        self.start_date = start_date
        self.end_date = min(end_date, today)
        # Prior window: same length, ending the day before this window starts.
        length = (self.end_date - self.start_date).days + 1
        self.prior_end = self.start_date - timedelta(days=1)
        self.prior_start = self.prior_end - timedelta(days=max(length, 1) - 1)

    def _ticket_queryset(self):
        tickets = Ticket.objects.filter(ticket_order__event=OuterRef('pk'))
        if self.ticket_filter == TICKET_FILTER_FREE:
            tickets = tickets.filter(price=0)
        elif self.ticket_filter == TICKET_FILTER_PAID:
            tickets = tickets.filter(price__gt=0)
        return tickets

    def _event_rows(self, start, end):
        """One row per held, non-cancelled event in [start, end] with its ticket count."""
        if start > end:
            return []
        sold_subq = Subquery(
            self._ticket_queryset()
            .order_by()
            .values('ticket_order__event')
            .annotate(c=Count('id'))
            .values('c')[:1],
            output_field=IntegerField(),
        )
        return list(
            Event.objects.filter(
                organization=self.organization,
                start_date__gte=start,
                start_date__lte=end,
            )
            .exclude(status=EVENT_STATUS_CANCELLED)
            # Each stat is its own Subquery, so tickets and spend never inflate each other.
            .annotate(
                sold=Coalesce(sold_subq, 0),
                ad_spend=_marketing_spend_expr(),
                orders=Coalesce(_order_count_expr(), 0),
                meta_attributed_orders=_meta_attributed_orders_expr(),
                # The event's own ticket capacity, else its venue's.
                fit_capacity=Coalesce('capacity', 'venue__capacity'),
            )
            .values(
                'id', 'name', 'start_date', 'market_id', 'market__name', 'venue_id',
                'sold', 'ad_spend', 'orders', 'meta_attributed_orders', 'fit_capacity',
            )
        )

    def _fit_rows(self):
        """Past events the spend model learns from (same ticket filter as the view)."""
        try:
            start = self.today.replace(year=self.today.year - SPEND_MODEL_LOOKBACK_YEARS)
        except ValueError:  # Feb 29
            start = self.today.replace(year=self.today.year - SPEND_MODEL_LOOKBACK_YEARS, day=28)
        return self._event_rows(start, self.today)

    @staticmethod
    def _group(rows):
        """Group per-event rows by market id -> {'label', 'sold', 'adjusted', 'rows'}."""
        groups = {}
        for row in rows:
            key = row['market_id']
            label = (row['market__name'] or '').strip() or NO_MARKET_LABEL
            g = groups.setdefault(key, {'label': label, 'sold': [], 'adjusted': [], 'rows': []})
            g['sold'].append(row['sold'])
            g['adjusted'].append(row['adjusted'])
            g['rows'].append(row)
        return groups

    def calculate(self):
        current_rows = self._event_rows(self.start_date, self.end_date)
        prior_rows = self._event_rows(self.prior_start, self.prior_end)
        model = fit_spend_effect(self._fit_rows()) if current_rows else fit_spend_effect([])
        beta = model['beta'] if model['available'] else 0.0
        for row in current_rows + prior_rows:
            row['adjusted'] = adjusted_sold(row['sold'], row['ad_spend'], beta, model['reference_spend'])

        # Fall back to raw counts when the model can't be trusted.
        adjusted = self.demand_mode == DEMAND_MODE_ADJUSTED and model['available']
        mode = DEMAND_MODE_ADJUSTED if adjusted else DEMAND_MODE_RAW
        shown = 'adjusted' if adjusted else 'sold'

        current = self._group(current_rows)
        prior = self._group(prior_rows)

        markets = []
        for key, g in current.items():
            raw = _stats(g['sold'], 1)
            adj = _stats(g['adjusted'], 1)
            disp = adj if adjusted else raw
            prior_disp = _stats(prior[key][shown], 1)['avg'] if key in prior else None
            top = max(g['rows'], key=lambda r: (r['sold'], r['start_date']))
            ad_spend = float(sum(r['ad_spend'] for r in g['rows']))
            tickets_sold = sum(g['sold'])
            markets.append({
                'key': str(key) if key else 'none',
                'label': g['label'],
                'event_count': len(g['sold']),
                'tickets_sold': tickets_sold,
                # Raw tickets per event, always available.
                'avg_per_event': raw['avg'],
                'median_per_event': raw['median'],
                # Spend-adjusted tickets per event (equals raw when the model is off).
                'adjusted_avg': adj['avg'],
                'adjusted_median': adj['median'],
                # What the page leads with, per the active mode.
                'display_avg': disp['avg'],
                'display_median': disp['median'],
                'prior_avg': prior_disp,
                'prior_event_count': len(prior[key]['sold']) if key in prior else 0,
                'change_pct': _pct_change(disp['avg'], prior_disp),
                # Floats (2dp) so the payload stays JSON-safe for the chart blob.
                'ad_spend': round(ad_spend, 2),
                'ad_spend_per_event': round(ad_spend / len(g['rows']), 2),
                'cost_per_ticket': (
                    round(ad_spend / tickets_sold, 2) if ad_spend > 0 and tickets_sold > 0 else None
                ),
                'top_event': {
                    'id': str(top['id']),
                    'name': top['name'],
                    'sold': top['sold'],
                    'start_date': top['start_date'],
                },
            })
        markets.sort(key=lambda m: (-m['display_avg'], m['label']))

        current_disp = [r[shown] for r in current_rows]
        prior_disp_all = [r[shown] for r in prior_rows]
        overall = _stats(current_disp, 1)['avg']
        prior_overall = _stats(prior_disp_all, 1)['avg']
        summary = {
            'display_avg': overall,
            'raw_avg': _stats([r['sold'] for r in current_rows], 1)['avg'],
            'prior_avg': prior_overall,
            'change_pct': _pct_change(overall, prior_overall),
            'event_count': len(current_rows),
            'tickets_sold': sum(r['sold'] for r in current_rows),
            'market_count': len(markets),
        }
        return {
            'markets': markets,
            'summary': summary,
            'demand_mode': mode,
            'spend_model': model,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'prior_start': self.prior_start,
            'prior_end': self.prior_end,
        }
