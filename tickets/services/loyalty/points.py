"""Loyalty points wallet service.

Mirrors ``tickets/services/sms_credits.py``: every mutation runs in
``transaction.atomic()`` with ``select_for_update()`` on the balance-holding
row (here: Customer), writes exactly one signed ``LoyaltyPointsTransaction``
ledger row with post-mutation snapshots, and is idempotent per ticket order
via the ``loyaltypoints_one_per_order_kind`` partial unique constraint.

Pipeline (Phase 1 — earn + status spine):

    EARN (4 creation sites)                       REVOKE
    Stripe webhook ──┐                            full refund ───┐ (swallow)
    free checkout ───┼─ award_points_for_order    event cancel ──┘
    in-person sale ──┤   (swallow failures)       upload delete ──┐
    CSV chunk ───────┘                            CSV reprocess ──┼ (PROPAGATE:
                                                  event delete ───┘  abort delete)

Key invariants:
  - One EARN + one REVOKE per order, enforced by the DB.
  - REVOKE.amount is the APPLIED balance delta (-min(earn, balance)), so
    SUM(amount) per customer == Customer.points_balance stays true even after
    Phase-2 spending introduces clamping.
  - Revokes are ledger-driven (negate the STORED earn), never recomputed from
    the current rate/basis.
  - Placeholder-email customers (the CSV importer's shared in-person account)
    never earn: guarded here so every call site is protected.
"""
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count

from tickets.models import (
    Customer,
    LoyaltyPointsTransaction,
    LoyaltyProgram,
    Ticket,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_EMAIL_SUFFIX = '@placeholder.local'

POINTS_UPDATE_FIELDS = ['points_balance', 'lifetime_points']


def get_points_program(organization):
    """Return the org's active, non-deleted, points-enabled program or None.

    Returns None when the org's loyalty feature flag is off — the single
    choke point that stops all earning for unflagged orgs. Revokes stay
    ledger-driven and unaffected: clawbacks of past earns must still apply
    even after a flag is turned off.
    """
    if not organization.loyalty_feature_enabled:
        return None
    return LoyaltyProgram.objects.filter(
        organization=organization,
        is_active=True,
        deleted_at__isnull=True,
        points_enabled=True,
    ).first()


def points_for_order(order, program, *, ticket_count=None):
    """Integer points an order earns under the program's basis/rate (floor).

    Uses gross ``total_amount`` (what the buyer paid) for per-dollar — LTV's
    platform-fee subtraction is organizer-facing; points are customer-facing.
    Bulk callers pass a precomputed ``ticket_count`` so this never triggers a
    per-order COUNT query in a loop.
    """
    if program.points_basis == LoyaltyProgram.PointsBasis.PER_DOLLAR:
        return max(0, int(program.points_rate * order.total_amount))
    if ticket_count is None:
        ticket_count = order.tickets.count()
    return max(0, int(program.points_rate * Decimal(ticket_count)))


def _insert_earn(customer, order, points, description, created_by=None):
    """Write the EARN row + balance updates for a locked customer row."""
    customer.points_balance += points
    customer.lifetime_points += points
    customer.save(update_fields=POINTS_UPDATE_FIELDS)
    LoyaltyPointsTransaction.objects.create(
        organization=customer.organization,
        customer=customer,
        ticket_order=order,
        kind=LoyaltyPointsTransaction.Kind.EARN,
        amount=points,
        balance_after=customer.points_balance,
        lifetime_after=customer.lifetime_points,
        description=description,
        created_by=created_by,
    )


def award_points_for_order(order, program=None, *, description='', created_by=None):
    """Award points for one order. Idempotent; returns points awarded (0 on no-op).

    No-ops when the org has no active points-enabled program, when the
    customer is the CSV importer's placeholder account, when the order already
    has an EARN row, or when the computed points are 0.
    """
    customer = order.customer
    if customer is None or customer.email.endswith(PLACEHOLDER_EMAIL_SUFFIX):
        return 0
    if program is None:
        program = get_points_program(customer.organization)
    if program is None or not program.points_enabled:
        return 0

    points = points_for_order(order, program)
    if points <= 0:
        return 0

    try:
        with transaction.atomic():
            locked = Customer.objects.select_for_update().get(id=customer.id)
            # Check under the lock; the partial unique constraint is the backstop.
            if LoyaltyPointsTransaction.objects.filter(
                ticket_order=order, kind=LoyaltyPointsTransaction.Kind.EARN,
            ).exists():
                return 0
            _insert_earn(locked, order, points, description, created_by)
            return points
    except IntegrityError:
        # Concurrent award won the race — already recorded.
        return 0


def award_points_for_orders(orders, program, *, description=''):
    """Bulk award for CSV chunks / backfill batches. Returns total points awarded.

    Batched: one grouped ticket-count query + one existing-EARN query per call
    (never a COUNT per order — see CLAUDE.md aggregation rules). Locks each
    customer once, sorted by id (the deterministic-lock-order convention), one
    balance write per customer. Per-customer failures are logged and skipped so
    one bad row doesn't sink the batch.
    """
    if program is None or not program.points_enabled:
        return 0
    orders = [
        o for o in orders
        if o.customer_id and not o.customer.email.endswith(PLACEHOLDER_EMAIL_SUFFIX)
    ]
    if not orders:
        return 0

    order_ids = [o.id for o in orders]
    ticket_counts = dict(
        Ticket.objects.filter(ticket_order_id__in=order_ids)
        .values_list('ticket_order')
        .annotate(c=Count('id'))
    )
    already_earned = set(
        LoyaltyPointsTransaction.objects.filter(
            ticket_order_id__in=order_ids,
            kind=LoyaltyPointsTransaction.Kind.EARN,
        ).values_list('ticket_order_id', flat=True)
    )

    by_customer = {}
    for order in orders:
        if order.id in already_earned:
            continue
        points = points_for_order(order, program, ticket_count=ticket_counts.get(order.id, 0))
        if points > 0:
            by_customer.setdefault(order.customer_id, []).append((order, points))

    total = 0
    for customer_id in sorted(by_customer, key=str):
        try:
            with transaction.atomic():
                locked = Customer.objects.select_for_update().get(id=customer_id)
                for order, points in by_customer[customer_id]:
                    _insert_earn(locked, order, points, description)
                    total += points
        except IntegrityError:
            logger.info("Points already awarded concurrently for customer %s", customer_id)
        except Exception:
            logger.exception("Bulk points award failed for customer %s", customer_id)
    return total


def reset_points_for_organization(organization):
    """Hard-reset an org's loyalty points back to scratch. Returns a summary dict.

    The sanctioned escape hatch for "undo the backfill / start the points over":
    deletes the org's entire points ledger, zeroes every customer's
    points_balance and lifetime_points, and clears loyalty_points_backfilled_at
    so a fresh backfill can run cleanly afterward. Irreversible.

    Unlike revoke (which appends signed ledger rows), this DELETES history — the
    one place balances are mutated by truncation rather than a row — because
    "from scratch" must also clear the per-order EARN rows that would otherwise
    block a future re-backfill via the loyaltypoints_one_per_order_kind
    constraint. Run only when no backfill/recalc is in flight.

    Returns ``{'transactions_deleted': int, 'customers_reset': int}``.
    """
    with transaction.atomic():
        deleted, _ = LoyaltyPointsTransaction.objects.filter(
            organization=organization,
        ).delete()
        customers_reset = (
            Customer.objects.filter(organization=organization)
            .exclude(points_balance=0, lifetime_points=0)
            .update(points_balance=0, lifetime_points=0)
        )
        if organization.loyalty_points_backfilled_at is not None:
            organization.loyalty_points_backfilled_at = None
            organization.save(update_fields=['loyalty_points_backfilled_at'])
    logger.info(
        "Reset loyalty points for org %s: deleted %s ledger rows, zeroed %s customers",
        organization.id, deleted, customers_reset,
    )
    return {'transactions_deleted': deleted, 'customers_reset': customers_reset}


def _apply_revoke(locked_customer, earn, description, created_by=None):
    """Write the REVOKE row for one EARN against a locked customer row.

    The REVOKE amount is the APPLIED balance delta — -min(earn, balance) — so
    the per-customer ledger sum always equals points_balance (sum-auditable
    even when Phase-2 spending makes the clamp engage). Lifetime clamps
    independently via its own snapshot.
    """
    applied = min(earn.amount, locked_customer.points_balance)
    note = description
    if applied < earn.amount:
        note = (description or 'Revoke') + f' (clamped: earn was {earn.amount}, balance {locked_customer.points_balance})'
    locked_customer.points_balance -= applied
    locked_customer.lifetime_points = max(0, locked_customer.lifetime_points - earn.amount)
    locked_customer.save(update_fields=POINTS_UPDATE_FIELDS)
    LoyaltyPointsTransaction.objects.create(
        organization=locked_customer.organization,
        customer=locked_customer,
        ticket_order=earn.ticket_order,
        kind=LoyaltyPointsTransaction.Kind.REVOKE,
        amount=-applied,
        balance_after=locked_customer.points_balance,
        lifetime_after=locked_customer.lifetime_points,
        description=note[:255],
        created_by=created_by,
    )
    return applied


def revoke_points_for_order(order, *, description='', created_by=None):
    """Claw back one order's earned points. Idempotent; returns points revoked.

    Ledger-driven: negates the STORED earn amount (never recomputed — the
    rate/basis may have changed since). Works even if the program was since
    disabled or deleted, because the earn row is the source of truth.
    """
    earn = LoyaltyPointsTransaction.objects.filter(
        ticket_order=order, kind=LoyaltyPointsTransaction.Kind.EARN,
    ).first()
    if earn is None:
        return 0
    try:
        with transaction.atomic():
            locked = Customer.objects.select_for_update().get(id=earn.customer_id)
            if LoyaltyPointsTransaction.objects.filter(
                ticket_order=order, kind=LoyaltyPointsTransaction.Kind.REVOKE,
            ).exists():
                return 0
            return _apply_revoke(locked, earn, description, created_by)
    except IntegrityError:
        return 0


def revoke_points_for_orders(orders_or_ids, *, description=''):
    """Bulk revoke for the hard-delete sites (upload delete, CSV reprocess,
    event delete). Bulk twin of ``award_points_for_orders``: grouped, id-sorted
    customer locks, one balance write per customer.

    IMPORTANT: failures here must PROPAGATE — callers run this inside the
    delete transaction precisely so a failed revoke aborts the delete instead
    of stranding points (eng review D4). No try/except swallowing.
    """
    order_ids = [getattr(o, 'id', o) for o in orders_or_ids]
    if not order_ids:
        return 0
    earns = list(
        LoyaltyPointsTransaction.objects.filter(
            ticket_order_id__in=order_ids,
            kind=LoyaltyPointsTransaction.Kind.EARN,
        )
    )
    if not earns:
        return 0
    already_revoked = set(
        LoyaltyPointsTransaction.objects.filter(
            ticket_order_id__in=order_ids,
            kind=LoyaltyPointsTransaction.Kind.REVOKE,
        ).values_list('ticket_order_id', flat=True)
    )

    by_customer = {}
    for earn in earns:
        if earn.ticket_order_id in already_revoked:
            continue
        by_customer.setdefault(earn.customer_id, []).append(earn)

    total = 0
    for customer_id in sorted(by_customer, key=str):
        with transaction.atomic():
            locked = Customer.objects.select_for_update().get(id=customer_id)
            for earn in by_customer[customer_id]:
                total += _apply_revoke(locked, earn, description)
    return total
