"""
Local development seed command.

Populates a fresh database with one organization, an owner user, supporting users,
venues, events, customers, orders/tickets, RFM segments, expenses, surveys,
direct-ticketing products, Stripe checkout sessions, promo codes, and tracking
links — enough to click through every dashboard and analytics page.

Refuses to run unless DEBUG=True. Refuses to run if data already exists, unless
--force is passed.

Login after seeding:
    Email:  info@cueup.co  /  password123
    Phone:  +15555550199  (fake; set E2E_TEST_MODE=True to use OTP code 000000 locally)
"""
import random
import string
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from tickets.models import (
    CSVFormat,
    Customer,
    CustomerTag,
    Event,
    EventDailyPageView,
    EventExpense,
    EventIncome,
    EventSMSCampaign,
    EVENT_STATUS_CANCELLED,
    EVENT_STATUS_DRAFT,
    EVENT_STATUS_ENDED,
    EVENT_STATUS_LIVE,
    ExternalSurveyResponse,
    ExternalSurveyUpload,
    IncomeSource,
    LoyaltyProgram,
    LoyaltyTier,
    Market,
    MARKET_GEOGRAPHY_CITY,
    OrderCounter,
    Organization,
    OrganizationMembership,
    PhoneSuppression,
    PromoCode,
    SaleableTicketType,
    SaleableTicketTypeTier,
    SMSCampaign,
    SMSMessageRecipient,
    StripeCheckoutSession,
    SurveyInvitation,
    SurveyQuestion,
    Ticket,
    TicketOrder,
    TICKETING_TYPE_DIRECT,
    TICKETING_TYPE_EXTERNAL,
    TrackingLink,
    UploadedFile,
    UserProfile,
    Venue,
    WaitlistEntry,
    _generate_tracking_token,
)
from tickets.services.segmentation import recalculate_customer_segments

User = get_user_model()

OWNER_EMAIL = "info@cueup.co"
OWNER_PHONE = "+15555550199"
OWNER_PASSWORD = "password123"

# A live, upcoming event seeded with no ticket orders yet (freshly announced,
# nothing sold). Skipped by _create_orders_and_tickets so it stays empty.
LIVE_EVENT_WITHOUT_ORDERS = "Familiar Faces — Just Announced"

# A minimal fixture for exercising the survey-send flow: an ENDED direct event
# with exactly one ticket sold to one attendee, so "Send survey" has someone to
# email and the survey can be filled out end-to-end.
SURVEY_TEST_EVENT_NAME = "Survey Test Night — 1 sale, ended"
# Attendee is the owner's own address so the survey-send test actually lands in
# a real inbox the developer controls.
SURVEY_TEST_CUSTOMER_EMAIL = OWNER_EMAIL

# A fixture for the "survey sent, but no responses yet" state: an ENDED direct
# event whose attendees have all been emailed the survey (SurveyInvitation.sent_at
# set) but nobody has answered. The Surveys tab should show the green
# "Survey sent to N attendees · last sent …" confirmation and the
# "responses will appear here as they come in" empty state.
SURVEY_SENT_NO_RESPONSES_EVENT_NAME = "Survey Sent — awaiting responses"
SURVEY_SENT_NO_RESPONSES_ATTENDEES = 8


def build_survey_test_event(org, venue, owner, *, when=None, market=None):
    """Idempotently create an ENDED direct-ticketing event with exactly one
    ticket sold. Returns the Event. Safe to call repeatedly (keyed on org+name).

    Used by the seed command and runnable standalone to drop the fixture into an
    already-populated dev database.
    """
    now = when or timezone.now()
    event, created = Event.objects.get_or_create(
        organization=org,
        name=SURVEY_TEST_EVENT_NAME,
        defaults=dict(
            summary="Tiny fixture: one attendee, event over — ready to send a survey.",
            venue=venue,
            market=market,
            start_date=(now - timedelta(days=3)).date(),
            end_date=(now - timedelta(days=3)).date(),
            start_time=now.time().replace(microsecond=0),
            capacity=50,
            max_tickets_per_customer=4,
            ticketing_type=TICKETING_TYPE_DIRECT,
            status=EVENT_STATUS_ENDED,
            timezone="America/Los_Angeles",
            created_by=owner,
        ),
    )
    if not created:
        return event

    ticket_type = SaleableTicketType.objects.create(
        event=event,
        name="General Admission",
        price=_decimal(25),
        quantity_limit=50,
        max_per_customer=4,
        quantity_sold=1,
        order=1,
        sale_start=now - timedelta(days=21),
        sale_end=now - timedelta(days=3),
        description="Standing room. First come, first served.",
    )

    customer, _ = Customer.objects.get_or_create(
        organization=org,
        email=SURVEY_TEST_CUSTOMER_EMAIL,
        defaults=dict(name="Owen Barton"),
    )

    order = TicketOrder.objects.create(
        customer=customer,
        event=event,
        order_number=f"ORD-SURVEYTEST-{OrderCounter.next():06d}",
        order_date=now - timedelta(days=4),
        total_amount=ticket_type.price,
        is_in_person=False,
        created_by=owner,
    )
    Ticket.objects.create(
        ticket_order=order,
        ticket_type=ticket_type.name,
        price=ticket_type.price,
    )
    customer.update_lifetime_value()
    return event


def build_survey_sent_no_responses_event(org, venue, owner, *, when=None, market=None):
    """Idempotently create an ENDED direct event whose survey has already been
    emailed to every attendee, with zero responses recorded. Returns the Event.
    Safe to call repeatedly (keyed on org+name).

    Exercises the "survey sent — awaiting responses" UI: the Surveys tab shows the
    green "Survey sent to N attendees · last sent …" confirmation and the
    "responses will appear here as they come in" empty state (no NPS/star cards,
    since nobody has answered). Used by the seed command and runnable standalone to
    drop the fixture into an already-populated dev database.
    """
    now = when or timezone.now()
    event, created = Event.objects.get_or_create(
        organization=org,
        name=SURVEY_SENT_NO_RESPONSES_EVENT_NAME,
        defaults=dict(
            summary="Survey went out to every attendee — waiting on the first response.",
            venue=venue,
            market=market,
            start_date=(now - timedelta(days=5)).date(),
            end_date=(now - timedelta(days=5)).date(),
            start_time=now.time().replace(microsecond=0),
            capacity=50,
            max_tickets_per_customer=4,
            ticketing_type=TICKETING_TYPE_DIRECT,
            status=EVENT_STATUS_ENDED,
            timezone="America/Los_Angeles",
            created_by=owner,
        ),
    )
    if not created:
        return event

    ticket_type = SaleableTicketType.objects.create(
        event=event,
        name="General Admission",
        price=_decimal(25),
        quantity_limit=50,
        max_per_customer=4,
        quantity_sold=SURVEY_SENT_NO_RESPONSES_ATTENDEES,
        order=1,
        sale_start=now - timedelta(days=21),
        sale_end=now - timedelta(days=5),
        description="Standing room. First come, first served.",
    )

    # One attendee per invitation, each already emailed the survey. The most recent
    # send is 1 day ago so "last sent" reads as a real, recent timestamp.
    for i in range(SURVEY_SENT_NO_RESPONSES_ATTENDEES):
        customer, _ = Customer.objects.get_or_create(
            organization=org,
            email=f"survey-sent-{i:02d}@example.test",
            defaults=dict(name=f"Survey Sent Attendee {i + 1}"),
        )
        order = TicketOrder.objects.create(
            customer=customer,
            event=event,
            order_number=f"ORD-SURVEYSENT-{OrderCounter.next():06d}",
            order_date=now - timedelta(days=6),
            total_amount=ticket_type.price,
            is_in_person=False,
            created_by=owner,
        )
        Ticket.objects.create(
            ticket_order=order,
            ticket_type=ticket_type.name,
            price=ticket_type.price,
        )
        SurveyInvitation.objects.create(
            event=event,
            customer=customer,
            organization=org,
            email=customer.email,
            # Newest send 1 day ago; older ones staggered before it.
            sent_at=now - timedelta(days=1, hours=i),
        )
        customer.update_lifetime_value()

    return event

FIRST_NAMES = [
    "Maya", "Jordan", "Avery", "Quinn", "Reese", "Logan", "Skyler", "Rowan",
    "Hayden", "Marlowe", "Ezra", "Kai", "Sasha", "Indigo", "Wren", "Soren",
    "Iris", "Otis", "Naya", "Theo", "Lila", "Cy", "Vera", "Dash",
]
LAST_NAMES = [
    "Cole", "Nguyen", "Park", "Okafor", "Reyes", "Khan", "Levine", "Brooks",
    "Tanaka", "Ortiz", "Mancini", "Chen", "Rivera", "Solis", "Patel",
    "Bauer", "Holm", "Vaziri", "Singh", "Diaz",
]


def _gen_name(rng):
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _decimal(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Seed the local DB with demo data for development. DEBUG-only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even if data already exists (still requires DEBUG=True).",
        )
        parser.add_argument(
            "--if-empty",
            action="store_true",
            help="No-op (exit 0) when DB already has customers/events. Safe for setup scripts.",
        )

    def _guard(self):
        if not settings.DEBUG:
            raise CommandError("Refusing to seed: DEBUG is False.")

    def handle(self, *args, **options):
        self._guard()

        force = options["force"]
        already_populated = Customer.objects.exists() or Event.objects.exists()
        if already_populated and not force:
            if options["if_empty"]:
                self.stdout.write("DB already populated — skipping seed.")
                return
            raise CommandError(
                "DB already has data (customers/events present). "
                "Drop db.sqlite3 and re-run migrate, pass --force, or pass --if-empty to skip."
            )

        rng = random.Random(42)
        now = timezone.now()
        today = now.date()

        with transaction.atomic():
            org = self._create_org()
            owner, staff_users = self._create_users(org)
            venues = self._create_venues(org)
            # Collect all cities (venue cities + market-trend cities) so every
            # event can be attached to its market.
            venue_cities = [city for _, city, *_ in [
                ("The Echo", "Los Angeles"), ("Baby's All Right", "Brooklyn"), ("Mohawk", "Austin"),
            ]]
            trend_cities = [city for city, *_ in self.MARKET_TREND_SPECS]
            all_cities = sorted(set(venue_cities + trend_cities))
            markets = self._create_markets(org, all_cities)
            csv_formats = self._create_csv_formats(org, owner)
            uploads = self._create_uploads(org, csv_formats, owner)
            tags = self._create_tags(org)
            customers = self._create_customers(org, tags, rng)
            events = self._create_events(org, venues, markets, owner, today, rng)
            self._create_page_views(events, today, rng)
            promo_codes = self._create_promo_codes(org, events, now)
            # Direct-ticketing catalog must exist before orders so direct events
            # sell against their real SaleableTicketTypes (keeps quantity_sold and
            # the underlying Ticket rows consistent).
            self._create_direct_ticketing(events, now, rng)
            self._create_orders_and_tickets(events, customers, uploads, promo_codes, owner, today, rng)
            self._create_market_trend_history(org, markets, owner, today, rng)
            self._create_stripe_sessions(org, events, customers, now, rng)
            self._create_tracking_links(org, events, rng)
            self._create_expenses_and_income(org, events, owner, rng)
            self._create_surveys(org, events, customers, owner, rng)
            self._create_external_survey_responses(org, events, owner, rng)
            self._create_sms_broadcasts(org, events, customers, owner, now, rng)
            self._create_sms_compliance_fixtures(org)

            # Minimal fixture for exercising the survey-send flow end-to-end.
            survey_test_event = build_survey_test_event(
                org, venues[0], owner, when=now, market=markets.get("Los Angeles"),
            )
            self.stdout.write(self.style.SUCCESS(f"Survey test event: {survey_test_event.name}"))

            # Fixture for the "survey sent, no responses yet" confirmation state.
            survey_sent_event = build_survey_sent_no_responses_event(
                org, venues[0], owner, when=now, market=markets.get("Los Angeles"),
            )
            self.stdout.write(self.style.SUCCESS(f"Survey sent (no responses) event: {survey_sent_event.name}"))

            for customer in Customer.objects.filter(organization=org):
                customer.update_lifetime_value()

            recalculate_customer_segments(org)

            # Loyalty last: tier assignment reads fresh lifetime_value + scans.
            self._create_loyalty_program(org)

        self._print_summary(org)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _create_org(self):
        org = Organization.objects.create(
            name="Familiar Faces",
            slug="familiar-faces",
            description="Independent music + nightlife collective. Local seed data.",
            website="https://cueup.co",
            waitlist_feature_enabled=True,
            sms_marketing_enabled=True,
            loyalty_feature_enabled=True,
        )
        # Seed a prepaid SMS credit balance via the wallet service so the ledger
        # invariant holds (every balance change writes an SMSCreditTransaction).
        from tickets.services.sms_credits import credit
        from tickets.models import SMSCreditTransaction
        credit(
            org.id, 2500, kind=SMSCreditTransaction.Kind.ADJUSTMENT,
            description="Seed starter balance",
        )
        org.refresh_from_db(fields=["sms_credit_balance_cents"])
        self.stdout.write(self.style.SUCCESS(f"Org: {org.name}"))
        return org

    def _create_loyalty_program(self, org):
        """Seed "The Circle" — an attendance-based status program.

        Tiers key on distinct events *attended* (scanned in), so free-RSVP
        no-shows never earn status. Points stay off; status is the reward.
        """
        from tickets.services.loyalty import assign_loyalty_tiers

        program = LoyaltyProgram.objects.create(
            organization=org,
            name="The Circle",
            description="Attendance-based status program for Familiar Faces regulars.",
            is_active=True,
            points_enabled=False,
        )
        # Base tier (no rules) sits lowest; higher ranks need more events attended.
        LoyaltyTier.objects.create(
            program=program, name="Regular", rank=0, color="blue",
            perks="Presale access to every FF drop, member-only lineup reveals, birthday shoutout.",
        )
        # Thresholds are demo-scaled to the seed's sparse attendance (customers
        # attend at most ~2 distinct events) so all three tiers populate. Insider
        # = attended at least once, which cleanly separates real attendees from
        # RSVP-only no-shows. A real program would use higher bars (see the
        # SP-T439 design doc: 3 / 6 events attended).
        LoyaltyTier.objects.create(
            program=program, name="Insider", rank=1, color="green",
            min_events_attended=1,
            perks="24h early-bird presale, standing discount code, skip-the-line entry.",
        )
        LoyaltyTier.objects.create(
            program=program, name="Legend", rank=2, color="red",
            min_events_attended=2, attended_within_days=120,
            perks="Comp +1 guest list, exclusive merch, first dibs on limited events.",
        )
        assigned = assign_loyalty_tiers(program)
        self.stdout.write(self.style.SUCCESS(f"Loyalty: {program.name} ({assigned} members assigned)"))
        return program

    def _create_users(self, org):
        owner = User.objects.create_user(
            username="owen",
            email=OWNER_EMAIL,
            password=OWNER_PASSWORD,
            first_name="Owen",
            last_name="Barton",
            is_staff=True,
            is_superuser=True,
        )
        UserProfile.objects.create(
            user=owner,
            organization=org,
            role=UserProfile.Role.ORGANIZER,
            org_role=UserProfile.OrgRole.OWNER,
            phone_number=OWNER_PHONE,
            marketing_opt_in=True,
            terms_accepted_at=timezone.now(),
        )
        OrganizationMembership.objects.create(
            user=owner, organization=org, org_role=UserProfile.OrgRole.OWNER
        )

        staff = []
        for username, role, phone in [
            ("ada-host", UserProfile.OrgRole.HOST, "+13105550101"),
            ("ben-doorman", UserProfile.OrgRole.DOORMAN, "+13105550102"),
            ("cleo-admin", UserProfile.OrgRole.ADMIN, "+13105550103"),
        ]:
            user = User.objects.create_user(
                username=username,
                email=f"{username}@cueup.co",
                password=OWNER_PASSWORD,
                first_name=username.split("-")[0].capitalize(),
            )
            UserProfile.objects.create(
                user=user,
                organization=org,
                role=UserProfile.Role.ORGANIZER,
                org_role=role,
                phone_number=phone,
                terms_accepted_at=timezone.now(),
            )
            OrganizationMembership.objects.create(user=user, organization=org, org_role=role)
            staff.append(user)

        self.stdout.write(self.style.SUCCESS(f"Users: 1 owner + {len(staff)} staff"))
        return owner, staff

    def _create_markets(self, org, cities):
        """Create one city-level Market per city name. Returns {city: Market}."""
        markets = {}
        for city in cities:
            market = Market.objects.create(
                organization=org,
                name=city,
                geography_level=MARKET_GEOGRAPHY_CITY,
                geography_value=city,
            )
            markets[city] = market
        self.stdout.write(self.style.SUCCESS(f"Markets: {len(markets)}"))
        return markets

    def _create_venues(self, org):
        data = [
            ("The Echo", "Los Angeles", "1822 Sunset Blvd", "CA", "90026", 350),
            ("Baby's All Right", "Brooklyn", "146 Broadway", "NY", "11211", 280),
            ("Mohawk", "Austin", "912 Red River St", "TX", "78701", 800),
        ]
        venues = []
        for name, city, street, state, postal, capacity in data:
            venues.append(
                Venue.objects.create(
                    organization=org,
                    name=name,
                    city=city,
                    street_address=street,
                    state=state,
                    postal_code=postal,
                    country="USA",
                    capacity=capacity,
                )
            )
        return venues

    def _create_csv_formats(self, org, owner):
        formats = []
        formats.append(
            CSVFormat.objects.create(
                organization=org,
                name="Basic Eventbrite Export",
                description="Standard Eventbrite-style export.",
                is_default=True,
                column_mapping={
                    "order_number": "Order #",
                    "customer_name": ["First Name", "Last Name"],
                    "customer_email": "Email",
                    "event_name": "Event Name",
                    "event_date": "Event Date",
                    "ticket_type": "Ticket Type",
                    "ticket_price": "Ticket Price",
                    "order_date": "Order Date",
                },
                created_by=owner,
            )
        )
        formats.append(
            CSVFormat.objects.create(
                organization=org,
                name="Tiered Pricing Export",
                description="Manual-pricing format with tiered pricing.",
                requires_manual_pricing=True,
                uses_tiers=True,
                column_mapping={
                    "order_number": "Confirmation #",
                    "customer_email": "Buyer Email",
                    "customer_name": "Buyer",
                    "ticket_type": "Type",
                    "quantity": "Qty",
                },
                created_by=owner,
            )
        )
        return formats

    def _create_uploads(self, org, formats, owner):
        uploads = []
        uploads.append(
            UploadedFile.objects.create(
                organization=org,
                csv_format=formats[0],
                filename="echo-feb-export.csv",
                description="Eventbrite export from Feb show",
                source="Eventbrite",
                status="completed",
                total_rows=120,
                processed_rows=120,
                metadata={"row_errors": 0, "duplicates_skipped": 4},
                created_by=owner,
            )
        )
        uploads.append(
            UploadedFile.objects.create(
                organization=org,
                csv_format=formats[1],
                filename="mohawk-march-export.csv",
                description="Manual-pricing export, Austin show",
                source="Custom",
                status="completed",
                total_rows=240,
                processed_rows=240,
                metadata={"row_errors": 1, "duplicates_skipped": 0},
                created_by=owner,
            )
        )
        return uploads

    def _create_tags(self, org):
        names_colors = [
            ("VIP", "red"), ("Press", "purple"), ("Comp", "yellow"),
            ("Repeat", "green"), ("Industry", "blue"),
            ("Plus One", "orange"), ("Influencer", "purple"),
        ]
        return [
            CustomerTag.objects.create(organization=org, name=n, color=c)
            for n, c in names_colors
        ]

    def _create_customers(self, org, tags, rng):
        customers = []
        for i in range(120):
            name = _gen_name(rng)
            email = f"{slugify(name)}.{i:03d}@example.test"
            sms_opt = rng.random() < 0.55
            phone = ""
            if rng.random() < 0.6:
                phone = f"+1213555{4000 + i:04d}"
            cust = Customer.objects.create(
                organization=org,
                email=email,
                name=name,
                phone=phone,
                sms_opt_in=sms_opt,
                sms_opt_in_date=timezone.now() if sms_opt else None,
            )
            if rng.random() < 0.18:
                cust.tags.add(rng.choice(tags))
            if rng.random() < 0.05:
                cust.tags.add(tags[0])  # VIP
            customers.append(cust)
        self.stdout.write(self.style.SUCCESS(f"Customers: {len(customers)}"))
        return customers

    def _create_events(self, org, venues, markets, owner, today, rng):
        specs = [
            # (name, days_offset, status, ticketing, capacity, summary)
            ("Late Bloom — Winter", -120, EVENT_STATUS_ENDED, TICKETING_TYPE_EXTERNAL, 320, "Sold out winter showcase."),
            ("Open Decks Vol. 4",   -45,  EVENT_STATUS_ENDED, TICKETING_TYPE_EXTERNAL, 280, "Local DJs spinning all night."),
            ("Bloom — Last Weekend", -2, EVENT_STATUS_ENDED, TICKETING_TYPE_DIRECT,    350, "Closed out the weekend at The Echo. Sold via Cue direct."),
            ("Bloom — Spring Edition",  14, EVENT_STATUS_LIVE, TICKETING_TYPE_DIRECT,   400, "Six-act bill at The Echo."),
            ("Daylight: Brooklyn Pop-Up", 60, EVENT_STATUS_LIVE, TICKETING_TYPE_DIRECT,  300, "Day-into-night warehouse party."),
            (LIVE_EVENT_WITHOUT_ORDERS,   30, EVENT_STATUS_LIVE, TICKETING_TYPE_DIRECT,  250, "On sale now — no tickets sold yet."),
            ("Solstice Festival",         110, EVENT_STATUS_DRAFT, TICKETING_TYPE_DIRECT, 800, "TBA lineup. Save the date."),
            ("Postponed: Acoustic Sessions", -10, EVENT_STATUS_CANCELLED, TICKETING_TYPE_EXTERNAL, 150, "Cancelled due to artist illness."),
        ]
        events = []
        for i, (name, offset, status, ticketing, cap, summary) in enumerate(specs):
            start_date = today + timedelta(days=offset)
            venue = venues[i % len(venues)]
            event = Event.objects.create(
                organization=org,
                name=name,
                summary=summary,
                description=f"{summary}\n\nFollow @familiar.faces for the lineup and last-minute drops.",
                venue=venue,
                market=markets.get(venue.city),
                start_date=start_date,
                end_date=start_date,
                start_time=timezone.now().time().replace(microsecond=0),
                capacity=cap,
                max_tickets_per_customer=8,
                ticketing_type=ticketing,
                status=status,
                timezone="America/Los_Angeles",
                scanner_pin=f"{100000 + i + 1}",
                created_by=owner,
                public_buy_page_views=rng.randint(50, 1500) if status == EVENT_STATUS_LIVE else 0,
            )
            events.append(event)
        self.stdout.write(self.style.SUCCESS(f"Events: {len(events)}"))
        return events

    def _create_page_views(self, events, today, rng):
        """Daily public buy-page view rows for direct events.

        Feeds the Overview "Views" series and the Analytics tab's Page Views
        comparison chart. Only direct-ticketing events have a public buy page, so
        only they get rows. Traffic ramps toward the event date (a slow early
        trickle building as the show approaches) over a 90-day pre-sale window,
        capped at ``today`` so upcoming events only have views up to now. The
        event's cumulative ``public_buy_page_views`` counter is set to the row sum
        so the counter, the conversion rate, and the daily chart stay consistent.
        """
        horizon = 90
        total_rows = 0
        seeded_events = 0
        for event in events:
            if event.ticketing_type != TICKETING_TYPE_DIRECT:
                continue
            if event.status not in (EVENT_STATUS_LIVE, EVENT_STATUS_ENDED):
                continue
            if not event.start_date:
                continue
            window_start = event.start_date - timedelta(days=horizon)
            window_end = min(today, event.start_date)
            if window_end < window_start:
                continue  # upcoming event whose pre-sale window hasn't opened yet
            rows = []
            running_total = 0
            span_days = (window_end - window_start).days
            for offset in range(span_days + 1):
                day = window_start + timedelta(days=offset)
                days_before = (event.start_date - day).days
                # Proximity 0 (far out) -> ~1 (event day): views rise toward the show.
                proximity = 1.0 - min(days_before / horizon, 1.0)
                base = 4 + proximity * 40  # ~4/day early, ~44/day near the event
                count = max(0, int(rng.gauss(base, base * 0.35)))
                if count == 0:
                    continue
                rows.append(EventDailyPageView(event=event, date=day, view_count=count))
                running_total += count
            if not rows:
                continue
            EventDailyPageView.objects.bulk_create(rows)
            event.public_buy_page_views = running_total
            event.save(update_fields=["public_buy_page_views"])
            total_rows += len(rows)
            seeded_events += 1
        self.stdout.write(self.style.SUCCESS(
            f"Page views: {total_rows} daily rows across {seeded_events} direct events"
        ))

    def _create_promo_codes(self, org, events, now):
        codes = []
        live_or_past = [e for e in events if e.status in (EVENT_STATUS_LIVE, EVENT_STATUS_ENDED)]
        if not live_or_past:
            return codes
        codes.append(PromoCode.objects.create(
            organization=org, event=live_or_past[0], code="EARLY20",
            discount_type=PromoCode.PERCENTAGE, discount_value=_decimal(20),
            max_uses=100, times_used=14, expires_at=now + timedelta(days=30),
        ))
        codes.append(PromoCode.objects.create(
            organization=org, event=live_or_past[1 % len(live_or_past)], code="FRIENDS5",
            discount_type=PromoCode.FIXED, discount_value=_decimal(5),
            times_used=8,
        ))
        codes.append(PromoCode.objects.create(
            organization=org, event=live_or_past[-1], code="PRESS",
            discount_type=PromoCode.PERCENTAGE, discount_value=_decimal(100),
            max_uses=20, times_used=3,
        ))
        return codes

    def _create_orders_and_tickets(self, events, customers, uploads, promos, owner, today, rng):
        # External (CSV-style) events draw from a generic ticket-type list.
        external_ticket_types = [
            ("General Admission", 25, 35),
            ("Tier 2", 35, 45),
            ("VIP", 60, 95),
            ("Comp", 0, 0),
        ]
        orders_count = 0
        tickets_count = 0
        for event in events:
            if event.status == EVENT_STATUS_DRAFT:
                continue
            if event.name == LIVE_EVENT_WITHOUT_ORDERS:
                continue  # freshly announced — intentionally has no orders yet
            # Direct events sell against their real SaleableTicketType catalog so
            # quantity_sold and the underlying Ticket rows stay consistent. Track
            # remaining capacity so a limited type is never oversold.
            is_direct = event.ticketing_type == TICKETING_TYPE_DIRECT
            direct_types = list(event.saleable_ticket_types.all()) if is_direct else None
            direct_sold = {tt.id: 0 for tt in direct_types} if is_direct else None
            if event.status == EVENT_STATUS_CANCELLED:
                num_orders = 5
            else:
                num_orders = rng.randint(28, 55)
            upload = uploads[0] if event.ticketing_type == TICKETING_TYPE_EXTERNAL else None
            for _ in range(num_orders):
                customer = rng.choice(customers)
                qty = rng.choices([1, 2, 3, 4, 5, 6], weights=[35, 30, 15, 10, 6, 4])[0]
                if is_direct:
                    pick = self._pick_direct_type(direct_types, direct_sold, qty, rng)
                    if pick is None:
                        break  # catalog fully sold out
                    tt, qty = pick
                    tt_name = tt.name
                    price = tt.price
                else:
                    tt_name, tt_low, tt_high = rng.choice(external_ticket_types)
                    price = _decimal(rng.randint(tt_low, tt_high)) if tt_high else _decimal(0)
                total = price * qty
                # Spread order_date across the 8 weeks before the event
                days_before = rng.randint(2, 56)
                order_dt = timezone.make_aware(timezone.datetime.combine(
                    event.start_date - timedelta(days=days_before),
                    timezone.datetime.min.time(),
                )) + timedelta(hours=rng.randint(8, 22), minutes=rng.randint(0, 59))
                refunded_at = order_dt + timedelta(days=rng.randint(1, 14)) if rng.random() < 0.05 else None
                in_person = rng.random() < 0.10 and event.status == EVENT_STATUS_ENDED
                checked_in_at = None
                if event.status == EVENT_STATUS_ENDED and rng.random() < 0.7:
                    checked_in_at = timezone.make_aware(timezone.datetime.combine(
                        event.start_date, timezone.datetime.min.time()
                    )) + timedelta(hours=rng.randint(19, 23), minutes=rng.randint(0, 59))
                promo = None
                discount = None
                if promos and rng.random() < 0.12:
                    promo = rng.choice([p for p in promos if p.event_id == event.id] or [None])
                    if promo:
                        if promo.discount_type == PromoCode.PERCENTAGE:
                            discount = (total * promo.discount_value / 100).quantize(Decimal("0.01"))
                        else:
                            discount = min(total, promo.discount_value)
                        total = max(_decimal(0), total - discount)
                order = TicketOrder.objects.create(
                    customer=customer,
                    event=event,
                    uploaded_file=upload,
                    order_number=f"ORD-{OrderCounter.next():06d}",
                    external_order_number=f"EB-{rng.randint(100000, 999999)}" if upload else "",
                    order_date=order_dt,
                    total_amount=total,
                    refunded_at=refunded_at,
                    is_in_person=in_person,
                    checked_in_at=checked_in_at,
                    checked_in_by=owner if checked_in_at else None,
                    promo_code=promo,
                    discount_amount=discount,
                    created_by=owner,
                )
                orders_count += 1
                for _ in range(qty):
                    Ticket.objects.create(
                        ticket_order=order,
                        ticket_type=tt_name,
                        price=price,
                        # Mirror the order's check-in onto each ticket so the
                        # attendance-based loyalty tiers (which read scanned_at)
                        # have data. Free-RSVP no-shows keep scanned_at=None.
                        scanned_at=checked_in_at,
                    )
                    tickets_count += 1
                if is_direct:
                    direct_sold[tt.id] += qty
            # Persist the true sold count for direct types from the tickets created.
            if is_direct:
                for tt in direct_types:
                    if tt.quantity_sold != direct_sold[tt.id]:
                        tt.quantity_sold = direct_sold[tt.id]
                        tt.save(update_fields=["quantity_sold"])
        self.stdout.write(self.style.SUCCESS(f"Orders: {orders_count} | Tickets: {tickets_count}"))

    @staticmethod
    def _pick_direct_type(types, sold, qty, rng):
        """Choose a SaleableTicketType for one order, respecting remaining capacity.

        Returns (ticket_type, adjusted_qty) or None if the whole catalog is sold
        out. GA-style types are weighted more popular than VIP. qty is clamped to
        the chosen type's remaining allotment so a limited type is never oversold.
        """
        available, weights = [], []
        for tt in types:
            limit = tt.quantity_limit
            remaining = None if limit is None else max(limit - sold[tt.id], 0)
            if remaining == 0:
                continue
            available.append((tt, remaining))
            weights.append(30 if "VIP" in tt.name else 70)
        if not available:
            return None
        tt, remaining = rng.choices(available, weights=weights)[0]
        if remaining is not None:
            qty = min(qty, remaining)
        return tt, qty

    # Demo markets for the Market Trends analytics page. Each market is a distinct
    # city with one event per spec slot across several past quarters, so the page
    # can fit a real per-market turnout trend. The per-quarter tuples are
    # (tickets_per_event, returning_pct) and are tuned to produce one market of
    # each trend shape / dominant decline driver. EVENTS_PER_QUARTER events are
    # created per slot. Verified against MarketTrendCalculator; see the unit tests
    # in tickets/tests.py (MarketTrendCalculatorTests).
    MARKET_TREND_SPECS = [
        # city, venue, quarter tuples (oldest -> newest)
        # Denver: turnout sliding as loyal regulars stop coming back (retention).
        ("Denver", "Larimer Lounge", [
            (32, 0), (30, 55), (28, 45), (24, 30), (20, 18), (16, 8),
        ]),
        # Nashville: turnout sliding as new-buyer acquisition dries up.
        ("Nashville", "The Basement East", [
            (34, 0), (28, 35), (22, 45), (17, 52), (13, 58), (10, 62),
        ]),
        # Memphis: turnout sliding on softening demand; a pure first-timer market
        # that never builds a returning base, so the drop is all about demand.
        ("Memphis", "Growlers", [
            (32, 0), (28, 0), (23, 0), (18, 0), (14, 0), (11, 0),
        ]),
        # Seattle: turnout climbing — the bright spot.
        ("Seattle", "Neumos", [
            (12, 0), (16, 30), (21, 38), (26, 42), (31, 45), (36, 48),
        ]),
        # Portland: holding steady, no action needed.
        ("Portland", "Doug Fir Lounge", [
            (24, 0), (25, 35), (23, 40), (25, 42), (24, 41), (25, 43),
        ]),
        # Sacramento: ticket VOLUME holds steady but average price erodes — looks
        # stable by tickets, declining by revenue (dominant driver = price). The
        # optional 3rd tuple element pins the per-ticket price for that quarter.
        ("Sacramento", "Ace of Spades", [
            (28, 35, 42), (28, 40, 40), (27, 38, 36), (28, 42, 30), (28, 40, 24), (28, 43, 18),
        ]),
        # Tucson: tickets AND price steady, but cost per event keeps rising — looks
        # stable by tickets and revenue, declining by PROFIT (dominant driver =
        # costs). The optional 4th tuple element pins the per-event cost.
        ("Tucson", "Club Congress", [
            (28, 0, 35, 300), (28, 38, 35, 380), (28, 40, 35, 480),
            (28, 39, 35, 600), (28, 41, 35, 740), (28, 40, 35, 900),
        ]),
        # Boise: just two shows on the books — not enough history to read a trend.
        ("Boise", "Neurolux", [
            (28, 0), (22, 30),
        ]),
    ]
    EVENTS_PER_QUARTER = 2

    def _past_quarter_dates(self, today, n):
        """Return `n` dates, oldest first, each in a distinct fully-past quarter.

        Anchored on the 15th of each quarter's first month so every date is
        comfortably before `today` (the current, in-progress quarter is skipped).
        """
        cy, cq = today.year, (today.month - 1) // 3  # 0-based current quarter
        pairs = []
        for _ in range(n):
            cq -= 1
            if cq < 0:
                cq, cy = 3, cy - 1
            pairs.append((cy, cq))
        pairs.reverse()
        return [date(yy, qq * 3 + 1, 15) for yy, qq in pairs]

    # Default per-event cost as a fraction of that event's ticket revenue, so
    # profit tracks revenue for most markets (~45% margin). Markets with an
    # explicit 4th tuple element override this with a fixed per-event cost.
    DEFAULT_COST_FRACTION = 0.55

    # Per-market promoter-share trajectory (start -> end across the quarters) used
    # to seed NPS survey responses so the NPS lens has real trends. Most markets
    # are flat (stable NPS); Nashville's sentiment sours (declining NPS, driven by
    # more detractors) and Seattle's improves (growing NPS).
    NPS_PROMOTER_TRAJECTORY = {
        'Nashville': (0.55, 0.20),
        'Seattle': (0.30, 0.62),
    }
    NPS_DEFAULT_PROMOTER = 0.45
    NPS_PASSIVE_P = 0.30
    NPS_RESPONSES_PER_EVENT = (8, 16)   # rng range

    def _create_market_trend_history(self, org, markets, owner, today, rng):
        from tickets.models import (
            EVENT_STATUS_ENDED, TICKETING_TYPE_EXTERNAL,
            ExternalSurveyResponse, ExternalSurveyUpload,
        )

        nps_upload = ExternalSurveyUpload.objects.create(
            organization=org,
            filename='market-trends-nps.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
            created_by=owner,
        )

        markets_made = events_made = orders_made = expenses_made = nps_made = 0
        # Running counter for unique trend-buyer phone numbers (415 area, distinct
        # from the main pool's 213 numbers) so each market has an SMS-reachable
        # audience — lets an AI campaign plan default to the venue's market instead
        # of "All SMS subscribers" when tested on any market-backed event.
        phone_seq = 0
        for city, venue_name, quarters in self.MARKET_TREND_SPECS:
            n_quarters = len(quarters)
            prom_start, prom_end = self.NPS_PROMOTER_TRAJECTORY.get(
                city, (self.NPS_DEFAULT_PROMOTER, self.NPS_DEFAULT_PROMOTER)
            )
            venue = Venue.objects.create(
                organization=org, name=venue_name, city=city,
                state="", country="USA",
                capacity=max(q[0] for q in quarters) * self.EVENTS_PER_QUARTER + 50,
            )
            anchors = self._past_quarter_dates(today, len(quarters))
            seen = []   # customers with at least one prior order in this market
            cust_seq = 0
            for qi, q in enumerate(quarters):
                # Quarter tuple is (tickets_per_event, returning_pct[, fixed_price[, fixed_cost]]).
                tickets_per_event, ret_pct = q[0], q[1]
                quarter_price = q[2] if len(q) > 2 else None
                quarter_cost = q[3] if len(q) > 3 else None
                anchor = anchors[qi]
                # Spread this quarter's events across its first two months.
                q_events = []
                for ei in range(self.EVENTS_PER_QUARTER):
                    start_date = anchor + timedelta(days=ei * 35)
                    if start_date >= today:
                        start_date = today - timedelta(days=3)
                    q_events.append(Event.objects.create(
                        organization=org,
                        name=f"{city} Nights — Q{qi + 1} #{ei + 1}",
                        summary=f"{city} show.",
                        venue=venue,
                        market=markets.get(city),
                        start_date=start_date,
                        end_date=start_date,
                        start_time=timezone.now().time().replace(microsecond=0),
                        capacity=venue.capacity,
                        ticketing_type=TICKETING_TYPE_EXTERNAL,
                        status=EVENT_STATUS_ENDED,
                        timezone="America/Los_Angeles",
                        created_by=owner,
                    ))
                events_made += len(q_events)

                total = tickets_per_event * len(q_events)
                returning_target = min(round(ret_pct / 100 * total), len(seen))
                new_target = total - returning_target

                returning_buyers = rng.sample(seen, returning_target) if returning_target else []
                new_buyers = []
                for _ in range(new_target):
                    name = _gen_name(rng)
                    # Mirror the main pool's opt-in/phone rates so ~a third of each
                    # market's buyers are SMS-reachable (opted in + has a phone).
                    sms_opt = rng.random() < 0.55
                    phone = ""
                    if rng.random() < 0.6:
                        phone = f"+1415555{phone_seq:04d}"
                        phone_seq += 1
                    cust = Customer.objects.create(
                        organization=org,
                        email=f"{slugify(city)}.{slugify(name)}.{cust_seq:04d}@example.test",
                        name=name,
                        phone=phone,
                        sms_opt_in=sms_opt,
                        sms_opt_in_date=timezone.now() if sms_opt else None,
                    )
                    cust_seq += 1
                    new_buyers.append(cust)

                # Each buyer places one 1-ticket order this quarter, spread over the
                # quarter's events. New buyers' first-ever order is this quarter
                # (counted "new"); returning buyers debuted earlier ("returning").
                buyers = returning_buyers + new_buyers
                rng.shuffle(buyers)
                event_revenue = {e.id: Decimal("0.00") for e in q_events}
                for bi, cust in enumerate(buyers):
                    event = q_events[bi % len(q_events)]
                    price = _decimal(quarter_price if quarter_price is not None
                                     else rng.choice([25, 30, 35, 40]))
                    order_dt = timezone.make_aware(timezone.datetime.combine(
                        event.start_date - timedelta(days=rng.randint(3, 30)),
                        timezone.datetime.min.time(),
                    )) + timedelta(hours=rng.randint(9, 21))
                    order = TicketOrder.objects.create(
                        customer=cust,
                        event=event,
                        order_number=f"ORD-{OrderCounter.next():06d}",
                        order_date=order_dt,
                        total_amount=price,
                        created_by=owner,
                    )
                    Ticket.objects.create(
                        ticket_order=order, ticket_type="General Admission", price=price,
                    )
                    event_revenue[event.id] += price
                    orders_made += 1

                # One expense per event so the profitability lens is meaningful:
                # a fixed per-event cost when specified, else ~55% of revenue.
                for event in q_events:
                    if quarter_cost is not None:
                        cost = _decimal(quarter_cost)
                    else:
                        cost = _decimal(
                            float(event_revenue[event.id]) * self.DEFAULT_COST_FRACTION
                        )
                    EventExpense.objects.create(
                        event=event,
                        category="production",
                        description="Production & venue",
                        amount=cost,
                        expense_date=event.start_date - timedelta(days=7),
                        created_by=owner,
                    )
                    expenses_made += 1

                # NPS survey responses per event so the NPS lens has trend data.
                # Promoter share interpolates across the quarters per the market's
                # trajectory; responded_at sits just after the show.
                frac = qi / (n_quarters - 1) if n_quarters > 1 else 0.0
                promoter_p = prom_start + (prom_end - prom_start) * frac
                for event in q_events:
                    for _ in range(rng.randint(*self.NPS_RESPONSES_PER_EVENT)):
                        roll = rng.random()
                        if roll < promoter_p:
                            nps = rng.randint(9, 10)
                        elif roll < promoter_p + self.NPS_PASSIVE_P:
                            nps = rng.randint(7, 8)
                        else:
                            nps = rng.randint(0, 6)
                        responded_at = timezone.make_aware(timezone.datetime.combine(
                            event.start_date + timedelta(days=rng.randint(1, 14)),
                            timezone.datetime.min.time(),
                        )) + timedelta(hours=rng.randint(8, 22))
                        ExternalSurveyResponse.objects.create(
                            organization=org,
                            upload=nps_upload,
                            event=event,
                            responded_at=responded_at,
                            email=f"nps{nps_made:05d}@example.test",
                            nps_score=None if rng.random() < 0.12 else nps,
                            city=city,
                        )
                        nps_made += 1

                # New buyers can return in later quarters.
                seen.extend(new_buyers)
            markets_made += 1

        nps_upload.row_count = nps_made
        nps_upload.save(update_fields=['row_count'])
        self.stdout.write(self.style.SUCCESS(
            f"Market trend history: {markets_made} markets, "
            f"{events_made} events, {orders_made} orders, {expenses_made} expenses, "
            f"{nps_made} NPS responses"
        ))

    def _create_direct_ticketing(self, events, now, rng):
        direct_events = [
            e for e in events
            if e.ticketing_type == TICKETING_TYPE_DIRECT
            and e.status in (EVENT_STATUS_LIVE, EVENT_STATUS_ENDED)
        ]
        for i, event in enumerate(direct_events):
            no_orders = event.name == LIVE_EVENT_WITHOUT_ORDERS
            # quantity_sold starts at 0; _create_orders_and_tickets sells real
            # orders against these types and writes back the true count.
            ga = SaleableTicketType.objects.create(
                event=event, name="General Admission", price=_decimal(30),
                quantity_limit=200, max_per_customer=6,
                quantity_sold=0,
                order=1, sale_start=now - timedelta(days=14),
                description="Standing room. First come, first served.",
            )
            vip = SaleableTicketType.objects.create(
                event=event, name="VIP Lounge", price=_decimal(85),
                quantity_limit=40, max_per_customer=4,
                quantity_sold=0,
                order=2, sale_start=now - timedelta(days=14),
                description="Reserved table + welcome drink.",
            )
            if no_orders:
                # Just announced: ticket types are on sale, but no tier history
                # or waitlist has built up yet.
                continue
            if i == 0:
                # Tiered pricing on first live direct event
                SaleableTicketTypeTier.objects.create(
                    ticket_type=ga, name="Early Bird", price=_decimal(20), allotment=60, quantity_sold=60, order=1,
                )
                SaleableTicketTypeTier.objects.create(
                    ticket_type=ga, name="Advance", price=_decimal(28), allotment=80, quantity_sold=40, order=2,
                )
                SaleableTicketTypeTier.objects.create(
                    ticket_type=ga, name="Door", price=_decimal(35), allotment=60, quantity_sold=0, order=3,
                )
            # Waitlist entry on the VIP type
            WaitlistEntry.objects.create(
                ticket_type=vip,
                email="waiting@example.test",
                name="Hopeful Friend",
                position=1,
            )

    def _create_stripe_sessions(self, org, events, customers, now, rng):
        direct_events = [
            e for e in events
            if e.ticketing_type == TICKETING_TYPE_DIRECT
            and e.status in (EVENT_STATUS_LIVE, EVENT_STATUS_ENDED)
        ]
        if not direct_events:
            return
        # Pick distinct orders so the OneToOne on ticket_order doesn't collide.
        candidate_orders = list(
            TicketOrder.objects.filter(event__in=direct_events)
            .order_by("?")[:12]
        )
        for order in candidate_orders[:8]:
            event = order.event
            customer = order.customer
            qty = max(1, order.tickets.count())
            unit_cents = rng.choice([3000, 5000, 8500])
            amount_cents = unit_cents * qty
            fee_cents = max(150, int(amount_cents * 0.029) + 30)
            StripeCheckoutSession.objects.create(
                event=event,
                organization=org,
                stripe_session_id=f"cs_test_seed_{uuid.uuid4().hex[:24]}",
                stripe_payment_intent_id=f"pi_test_seed_{uuid.uuid4().hex[:24]}",
                buyer_email=customer.email,
                buyer_name=customer.name,
                status=StripeCheckoutSession.Status.COMPLETED,
                line_items_snapshot=[{
                    "name": "General Admission",
                    "price": f"{unit_cents/100:.2f}",
                    "quantity": qty,
                }],
                amount_total_cents=amount_cents,
                platform_fee_cents=fee_cents,
                ticket_order=order,
                fulfilled_at=now - timedelta(days=rng.randint(0, 10)),
                available_on=now - timedelta(days=rng.randint(0, 5)),
                sms_opt_in=rng.random() < 0.5,
            )

    def _create_tracking_links(self, org, events, rng):
        names = ["Instagram Story", "Newsletter", "Influencer Drop", "Door List"]
        live_direct = [e for e in events if e.ticketing_type == TICKETING_TYPE_DIRECT]
        for event in live_direct:
            for n in rng.sample(names, k=2):
                TrackingLink.objects.create(
                    organization=org,
                    event=event,
                    name=n,
                    token=_generate_tracking_token(),
                    click_count=rng.randint(20, 800),
                )

    def _create_expenses_and_income(self, org, events, owner, rng):
        sources = []
        for i, n in enumerate(["Bar Splits", "Merch", "Sponsorship"]):
            sources.append(IncomeSource.objects.create(organization=org, name=n, order=i))

        expense_specs = [
            ("talent",    "Headliner fee",       (1500, 3500)),
            ("venue",     "Venue rental",        (800, 2500)),
            ("marketing", "Instagram ads",       (200, 600)),
            ("staffing",  "Security + door",     (300, 800)),
            ("production","Sound engineer",      (400, 900)),
        ]
        for event in events:
            if event.status not in (EVENT_STATUS_ENDED, EVENT_STATUS_LIVE):
                continue
            chosen = rng.sample(expense_specs, k=rng.randint(3, 5))
            for cat, desc, (lo, hi) in chosen:
                EventExpense.objects.create(
                    event=event, category=cat, description=desc,
                    amount=_decimal(rng.randint(lo, hi)),
                    expense_date=event.start_date - timedelta(days=rng.randint(1, 21)),
                    created_by=owner,
                )
            for src in rng.sample(sources, k=2):
                EventIncome.objects.create(
                    event=event, income_source=src,
                    amount=_decimal(rng.randint(150, 1200)),
                    income_date=event.start_date,
                    created_by=owner,
                )

    def _create_surveys(self, org, events, customers, owner, rng):
        past = [e for e in events if e.status == EVENT_STATUS_ENDED]
        if not past:
            return
        SurveyQuestion.objects.create(
            organization=org, event=past[0],
            question_text="How would you rate tonight overall?",
            question_type="star_rating", position=1, is_required=True,
        )
        SurveyQuestion.objects.create(
            organization=org, event=past[0],
            question_text="How likely are you to recommend us to a friend?",
            question_type="nps", position=2,
        )
        for cust in rng.sample(customers, k=12):
            event = rng.choice(past)
            SurveyInvitation.objects.get_or_create(
                event=event,
                customer=cust,
                defaults={
                    "organization": org,
                    "email": cust.email,
                    "sent_at": timezone.now() - timedelta(days=rng.randint(1, 30)),
                },
            )

    def _create_external_survey_responses(self, org, events, owner, rng):
        """Seed ExternalSurveyResponse rows so the Survey Analytics page has
        NPS + city + over-time data to render."""
        past_events = [
            e for e in events
            if e.status == EVENT_STATUS_ENDED and e.venue and e.venue.city
        ]
        if not past_events:
            return

        upload = ExternalSurveyUpload.objects.create(
            organization=org,
            filename='typeform-nps-export.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
            created_by=owner,
        )

        # Per-city NPS bias so the city filter feels meaningful in the demo.
        city_bias = {
            'Los Angeles': (0.58, 0.27),   # promoter %, passive % (detractor = remainder)
            'Brooklyn':    (0.46, 0.32),
            'Austin':      (0.38, 0.30),
        }
        default_bias = (0.45, 0.32)

        rating_choices = ['Loved it', 'Great', 'Good', 'OK', 'Meh']
        enjoyed_pool = ['Music', 'Vibe', 'Crowd', 'Lighting', 'Venue', 'DJs', 'Drinks']
        genres_pool = ['House', 'Techno', 'Disco', 'Indie', 'Hip-Hop', 'Soul']
        improvements_pool = ['Sound', 'Lines at bar', 'Ventilation', 'Bathrooms', 'Set times']
        crowd_vibes = ['Friendly', 'Energetic', 'Chill', 'Mixed']
        venue_feels = ['Intimate', 'Spacious', 'Cramped', 'Iconic']
        found_outs = ['Instagram', 'Friend', 'Newsletter', 'Walked by', 'Reddit', 'TikTok']
        text_pool = [
            '', '', '', '', '',
            'Best night out in months.',
            'Sound got muddy near the end.',
            'Crowd was incredible.',
            'Would come back. Bring more local DJs.',
            'Bathroom lines were rough.',
            'Loved the lighting design.',
            'Drink prices crept up. Music made up for it.',
            'Door was slow but worth the wait.',
        ]

        now = timezone.now()
        total = 0
        # 8 months of data so the time-series chart has a real trend
        for months_ago in range(8):
            month_count = rng.randint(18, 42)
            for _ in range(month_count):
                days_offset = months_ago * 30 + rng.randint(0, 29)
                responded_at = now - timedelta(
                    days=days_offset,
                    hours=rng.randint(0, 23),
                    minutes=rng.randint(0, 59),
                )
                event = rng.choice(past_events)
                city = event.venue.city
                promoter_p, passive_p = city_bias.get(city, default_bias)

                roll = rng.random()
                if roll < promoter_p:
                    nps = rng.randint(9, 10)
                elif roll < promoter_p + passive_p:
                    nps = rng.randint(7, 8)
                else:
                    nps = rng.randint(0, 6)
                # 12% of rows leave NPS blank, like real surveys
                nps_value = None if rng.random() < 0.12 else nps

                ExternalSurveyResponse.objects.create(
                    organization=org,
                    upload=upload,
                    event=event,
                    responded_at=responded_at,
                    email=f"guest{total:04d}@example.test",
                    overall_rating=rng.choice(rating_choices) if rng.random() < 0.85 else '',
                    nps_score=nps_value,
                    city=city,
                    enjoyed=rng.sample(enjoyed_pool, k=rng.randint(1, 3)),
                    genres=rng.sample(genres_pool, k=rng.randint(1, 2)),
                    improvements=rng.sample(improvements_pool, k=rng.randint(0, 2)),
                    crowd_vibe=rng.choice(crowd_vibes) if rng.random() < 0.6 else '',
                    venue_feel=rng.choice(venue_feels) if rng.random() < 0.6 else '',
                    found_out_how=rng.choice(found_outs) if rng.random() < 0.7 else '',
                    text_feedback=rng.choice(text_pool),
                )
                total += 1

        upload.row_count = total
        upload.save(update_fields=['row_count'])
        self.stdout.write(self.style.SUCCESS(f"External survey responses: {total}"))

    def _create_sms_broadcasts(self, org, events, customers, owner, now, rng):
        """Native SMS campaigns (with per-recipient delivery) + external SlickText
        broadcasts, so Marketing -> SMS has audience-over-time points, a by-market
        breakdown, delivery/click stats, and a top-broadcasts table.

        Native campaigns grow their audience over time (so the chart reads as
        audience growth); some are event-scoped (market = the event's venue city)
        and some are general (no market). SlickText broadcasts are confirmed metric
        records linked to ended events with bigger audiences.
        """
        contactable = [c for c in customers if c.phone and c.sms_opt_in]
        eventful = [e for e in events if e.venue and e.venue.city]

        def _event(idx):
            return eventful[idx % len(eventful)] if eventful else None

        # (name, days_ago, target_audience, link, event)
        native_specs = [
            ("Winter list warm-up",      150, 12, "",                            None),
            ("Late Bloom presale",       120, 18, "https://cueup.co/late-bloom", _event(0)),
            ("Open Decks reminder",       90, 24, "https://cueup.co/open-decks", _event(1)),
            ("VIP early access",          60, 30, "https://cueup.co/vip",        None),
            ("Bloom weekend blast",       30, 36, "https://cueup.co/bloom",      _event(2)),
            ("This weekend: doors 8pm",    7, 40, "https://cueup.co/doors",      _event(0)),
        ]
        native_count = 0
        for name, days_ago, target, link, event in native_specs:
            sent_at = now - timedelta(days=days_ago)
            pool = contactable[:]
            rng.shuffle(pool)
            recips = pool[:min(target, len(pool))]
            criteria = {'event_id': str(event.id)} if event else {'all_subscribers': True}
            campaign = SMSCampaign.objects.create(
                organization=org,
                event=event,
                name=name,
                body=f"{name} - grab tickets before they're gone. Reply STOP to opt out.",
                link_url=link,
                filter_criteria=criteria,
                status=SMSCampaign.Status.SENT,
                scheduled_at=sent_at,
                started_at=sent_at,
                sent_at=sent_at,
                audience_size=len(recips),
                created_by=owner,
            )
            rows = []
            for cust in recips:
                roll = rng.random()
                if roll < 0.88:
                    status = SMSMessageRecipient.Status.DELIVERED
                    delivered_at = sent_at + timedelta(minutes=2)
                elif roll < 0.95:
                    status = SMSMessageRecipient.Status.UNDELIVERED
                    delivered_at = None
                else:
                    status = SMSMessageRecipient.Status.FAILED
                    delivered_at = None
                delivered = status == SMSMessageRecipient.Status.DELIVERED
                clicked = bool(link) and delivered and rng.random() < 0.22
                opted_out = delivered and rng.random() < 0.04
                rows.append(SMSMessageRecipient(
                    campaign=campaign,
                    customer=cust,
                    phone=cust.phone,
                    status=status,
                    sent_at=sent_at,
                    delivered_at=delivered_at,
                    click_count=rng.randint(1, 3) if clicked else 0,
                    first_clicked_at=(sent_at + timedelta(hours=1)) if clicked else None,
                    opted_out_at=(sent_at + timedelta(hours=2)) if opted_out else None,
                ))
            SMSMessageRecipient.objects.bulk_create(rows)
            native_count += 1

        # External SlickText broadcasts (tracked metrics only) on ended events.
        slick_count = 0
        ended = [e for e in events if e.status == EVENT_STATUS_ENDED and e.venue and e.venue.city]
        for i, event in enumerate(ended):
            days_until = (event.start_date - now.date()).days
            send_time = now + timedelta(days=days_until, hours=-3)
            audience = rng.randint(180, 650)
            unique_clicks = max(1, int(audience * rng.uniform(0.06, 0.16)))
            clicks = unique_clicks + rng.randint(0, unique_clicks)
            unsubs = int(audience * rng.uniform(0.005, 0.02))
            orders = max(0, int(unique_clicks * rng.uniform(0.1, 0.3)))
            revenue = _decimal(orders * rng.randint(25, 60))
            click_rate = Decimal(str(round(unique_clicks / audience, 4))) if audience else Decimal("0.0000")
            EventSMSCampaign.objects.create(
                event=event,
                source='slicktext',
                external_id=f"st-seed-{i + 1}",
                name=f"{event.name} - SlickText blast",
                message="Tonight! Doors at 8. Tap for tickets.",
                send_time=send_time,
                audience_size=audience,
                clicks=clicks,
                unique_clicks=unique_clicks,
                click_rate=click_rate,
                unsubscribes=unsubs,
                orders=orders,
                revenue=revenue,
                confirmed_at=now,
                confirmed_by=owner,
                last_synced_at=now,
            )
            slick_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"SMS: {native_count} native campaigns, {slick_count} SlickText broadcasts"
        ))

    def _create_sms_compliance_fixtures(self, org):
        """Named fixtures for manually testing the SMS compliance/UX changes:

        - Suppressed-but-opted-in customers → the red "Opted out (STOP)" badge on
          the customer list + detail (suppression overrides the opt-in flag).
        - A suppressed, opted-OUT customer → selecting them and hitting
          "SMS status → Opt in to SMS" fires the "can only re-subscribe by texting
          START" warning.
        - An international (UK) opted-in customer → dropped from any campaign
          audience by the country gate (SMS_ALLOWED_COUNTRY_PREFIXES), which is what
          prevents Twilio Geo-Permission blocks (Error 21408).

        Recognizable names/emails so they're easy to find via search. Idempotent
        (get_or_create) so re-seeding with --force won't duplicate them.
        """
        specs = [
            # (name, email, phone, opt_in, suppression) where suppression is
            # None | 'global' | 'org'.
            ("Simone Ashford (STOP demo)", "simone.stop@example.test",
             "+12135550101", True, "global"),
            ("Priya Nadar (org STOP demo)", "priya.stop@example.test",
             "+12135550103", True, "org"),
            ("Marcus Reed (re-opt-in demo)", "marcus.stop@example.test",
             "+12135550102", False, "global"),
            ("Liam Fox (UK / geo demo)", "liam.uk@example.test",
             "+447700900123", True, None),
        ]
        for name, email, phone, opt_in, suppression in specs:
            cust, _ = Customer.objects.get_or_create(
                organization=org, email=email,
                defaults={
                    "name": name,
                    "phone": phone,
                    "sms_opt_in": opt_in,
                    "sms_opt_in_date": timezone.now() if opt_in else None,
                },
            )
            if suppression == "global":
                PhoneSuppression.objects.get_or_create(
                    phone=phone, organization=None,
                    defaults={"reason": PhoneSuppression.Reason.TWILIO_STOP},
                )
            elif suppression == "org":
                PhoneSuppression.objects.get_or_create(
                    phone=phone, organization=org,
                    defaults={"reason": PhoneSuppression.Reason.MANUAL},
                )
        self.stdout.write(self.style.SUCCESS(
            f"SMS compliance fixtures: {len(specs)} named customers "
            "(search 'demo' in the customer list)"
        ))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _print_summary(self, org):
        from tickets.models import Event, Customer, TicketOrder, Ticket
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Seed complete."))
        self.stdout.write(f"  Organization: {org.name}")
        self.stdout.write(f"  Events: {Event.objects.filter(organization=org).count()}")
        self.stdout.write(f"  Venues: {Venue.objects.filter(organization=org).count()}")
        self.stdout.write(f"  Customers: {Customer.objects.filter(organization=org).count()}")
        self.stdout.write(f"  Orders: {TicketOrder.objects.filter(event__organization=org).count()}")
        self.stdout.write(f"  Tickets: {Ticket.objects.filter(ticket_order__event__organization=org).count()}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Login:"))
        self.stdout.write(f"  Email:  {OWNER_EMAIL}  /  {OWNER_PASSWORD}")
        self.stdout.write(f"  Phone:  {OWNER_PHONE}")
        self.stdout.write("  (Phone OTP needs E2E_TEST_MODE=True or real Twilio creds; test code is 000000.)")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("SMS compliance test data (search 'demo' in the customer list):"))
        self.stdout.write("  Simone Ashford — opted in but STOP-suppressed → red 'Opted out (STOP)' badge")
        self.stdout.write("  Priya Nadar    — org-level suppression → same badge (per-org opt-out)")
        self.stdout.write("  Marcus Reed    — opted OUT + suppressed → select him, 'SMS status → Opt in to")
        self.stdout.write("                   SMS' should warn 'can only re-subscribe by texting START'")
        self.stdout.write("  Liam Fox (UK)  — +44 number → dropped from any campaign audience by the country gate")
        self.stdout.write("")
        self.stdout.write("Start the dev server: python manage.py runserver")
