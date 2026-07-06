"""Offline quality eval for AI SMS campaign plans (the quick-select goals).

Runs the REAL strategist (``generate_campaign_plan``) against a fixed corpus of
scenarios — the 10 quick-select goals from the plan form (5 event, 5 segment) —
and grades each generated plan against a rubric of deterministic checks plus an
optional LLM-as-judge pass. Because the strategist runs at ``temperature=0.7`` the
output is non-deterministic, so each scenario is sampled N times and reported as a
pass RATE, not a single verdict.

This is a diagnostic / regression tool, NOT a unit test:
  * It makes LIVE OpenAI calls (costs tokens; metered as AITokenUsage.FEATURE_SMS_PLAN
    exactly like production generation).
  * It does NOT persist SMSCampaignPlan rows — nothing is saved to the plans list.

Deterministic checks (high-confidence FAILs, plus softer WARNs):
  structure   3-5 steps, non-empty strategy summary, valid purposes
  length      each body ideally 1 GSM-7 segment incl. the auto STOP footer
  grounding   no invented links / prices / dates; no self-authored STOP footer
  link        the provided ticket link actually appears in at least one step
  schedule    no send in the past; for events, never at/after doors
  cadence     event offsets descend and fit within the runway (days_until_event)

Usage::

    python manage.py eval_sms_plans --org my-org
    python manage.py eval_sms_plans --org my-org --samples 5 --goals event
    python manage.py eval_sms_plans --org my-org --event <uuid> --judge
    python manage.py eval_sms_plans --org my-org --samples 3 --json /tmp/plans.json
    python manage.py eval_sms_plans --from-json /tmp/plans.json   # re-grade, no LLM cost
"""
import json
import random
import re
import sys
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import Event, Organization
from tickets.sms import sms_segment_info, strip_emoji, with_stop_footer

# The compliance footer as appended by apply_stop_footer, used to measure a body's
# segment count "as written" (before GSM-7 normalization) without re-normalizing it.
_PLAIN_FOOTER = '\n\nReply STOP to opt out'


# The quick-select goals, verbatim from templates/tickets/marketing/sms/plan_form.html.
EVENT_GOALS = [
    'Sell out the remaining tickets',
    'Drive early-bird sales',
    'Create last-minute urgency',
    'Upsell to a premium tier',
    'Bring back past attendees',
]
# Each segment goal is paired with the RFM segment it most naturally targets, so the
# audience the strategist sees matches the objective (values from SMS_SEGMENT_CHOICES).
SEGMENT_GOALS = [
    ('Win back lapsed customers', 'Lapsed'),
    ('Reward and retain VIPs', 'VIP'),
    ('Convert new subscribers to first purchase', 'New'),
    ('Re-engage dormant fans', 'Dormant'),
    ('Reactivate big spenders', 'Big Spender'),
]

# A synthetic tracked link handed to the strategist so the grounding + link-presence
# checks have a known-good URL to compare against. Overridable with --ticket-url.
DEFAULT_TICKET_URL = 'https://cueup.co/t/EVALLINK'

URL_RE = re.compile(r'(https?://[^\s\)\]\}<>"]+|\bwww\.[^\s\)\]\}<>"]+)', re.I)
PRICE_RE = re.compile(r'\$\s?\d[\d,]*(?:\.\d{2})?')
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
DATE_RE = re.compile(
    r'\b(?:' + '|'.join(MONTHS) + r')[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b',
    re.I,
)
STOP_FOOTER_RE = re.compile(r'reply\s+stop|text\s+stop|\bstop\s+to\s+(opt|unsub)', re.I)


def _norm_url(u):
    return u.rstrip('.,!?;:)"\'').lower()


def _rank(xs):
    """Fractional ranks (ties share the average rank), for Spearman."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(a, b):
    """Spearman rank correlation (Pearson on ranks); 0.0 when either side is constant."""
    if len(a) < 2:
        return 0.0
    ra, rb = _rank(a), _rank(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((x - mb) ** 2 for x in rb) ** 0.5
    return num / (da * db) if da and db else 0.0


def _allowed_date_variants(event):
    """Human/machine date strings that are legitimately groundable to the event date."""
    if event is None:
        return set()
    d = event.start_date
    variants = {d.isoformat()}
    for fmt in ('%B %-d', '%b %-d', '%B %d', '%b %d', '%A', '%-m/%-d', '%m/%d'):
        try:
            variants.add(d.strftime(fmt).lower())
        except ValueError:
            # Windows / non-glibc: %-d unsupported. Fall back to manual strip.
            variants.add(d.strftime(fmt.replace('%-d', '%d').replace('%-m', '%m')).lower())
    # Also allow "July 6th" style ordinals.
    variants.add(f"{d.strftime('%b').lower()} {d.day}")
    variants.add(f"{d.strftime('%B').lower()} {d.day}")
    return variants


class PlanGrade:
    """Accumulates FAIL/WARN findings for one generated plan."""

    def __init__(self):
        self.fails = []
        self.warns = []
        self.examples = []  # (severity, note, message) for human eyeballing

    def fail(self, note, message=None):
        self.fails.append(note)
        if message is not None:
            self.examples.append(('FAIL', note, message))

    def warn(self, note, message=None):
        self.warns.append(note)
        if message is not None:
            self.examples.append(('WARN', note, message))

    @property
    def passed(self):
        return not self.fails


def grade_plan(plan, *, event, ticket_url, tz, now):
    """Run the deterministic rubric over one plan dict. Returns a PlanGrade."""
    g = PlanGrade()
    steps = plan.get('steps') or []

    # ── structure ────────────────────────────────────────────────────────────
    if not (plan.get('strategy_summary') or '').strip():
        g.fail('empty strategy_summary')
    if not 3 <= len(steps) <= 5:
        g.fail(f'{len(steps)} steps (expected 3-5)')

    allowed_dates = _allowed_date_variants(event)
    allowed_url = _norm_url(ticket_url) if ticket_url else None
    days_until = None
    if event is not None:
        days_until = (event.start_date - now.date()).days
    event_start = None
    if event is not None and event.start_time:
        event_start = datetime.combine(event.start_date, event.start_time, tzinfo=tz)

    link_seen = False
    prev_offset = None
    for i, step in enumerate(steps):
        body = step.get('body') or ''
        label = f'step {i + 1} ({step.get("purpose")})'

        # ── length / segments ────────────────────────────────────────────────
        # Attribute a high segment count to the RIGHT cause, since each has a different
        # fix. Three measurements:
        #   raw  = as written
        #   norm = after GSM-7 punctuation normalization (keeps emoji)
        #   lean = norm + emoji removed = the true length of the actual words
        # If `lean` is still multi-segment the copy is genuinely too long (edit it). If
        # only `norm` is, an emoji is forcing UCS-2 (drop the emoji). If only `raw` is,
        # smart punctuation inflated it and send-time normalization already fixes it.
        # `raw` must measure the body AS WRITTEN — appended with a plain footer, NOT via
        # with_stop_footer (which normalizes internally), or raw would equal norm and the
        # punctuation-inflation branch below could never fire.
        raw_enc, raw_seg = sms_segment_info(body + _PLAIN_FOOTER)
        norm_enc, norm_seg = sms_segment_info(with_stop_footer(body))
        lean_enc, lean_seg = sms_segment_info(with_stop_footer(strip_emoji(body)))
        if lean_seg > 2:
            g.fail(f'{label}: {lean_seg} segments even as plain GSM-7 (genuinely long)', body)
        elif lean_seg == 2:
            g.warn(f'{label}: 2 segments as plain GSM-7 (long)', body)
        elif norm_enc == 'UCS-2':
            # The words fit in one GSM-7 segment; emoji (or other non-GSM-7 content) is
            # what forces UCS-2 and the extra cost — a brand-voice + cost miss.
            g.warn(f'{label}: emoji forces UCS-2 ({norm_seg} segs; 1 as plain GSM-7)', body)
        elif raw_seg > 1:
            # Inflated as written but normalization collapses it back to one segment —
            # informational, not a defect the copywriting needs to fix.
            g.warn(f'{label}: {raw_seg} segs as written, 1 after GSM-7 normalization ({raw_enc}->GSM-7)')
        if len(body) > 320:
            g.fail(f'{label}: body {len(body)} chars (>320)', body)

        # ── grounding: self-authored STOP footer (must not) ──────────────────
        if STOP_FOOTER_RE.search(body):
            g.fail(f'{label}: wrote its own STOP footer', body)

        # ── grounding: links ─────────────────────────────────────────────────
        for u in URL_RE.findall(body):
            nu = _norm_url(u)
            if allowed_url and (nu == allowed_url or allowed_url in nu or nu in allowed_url):
                link_seen = True
            else:
                g.fail(f'{label}: invented link {u!r}', body)

        # ── grounding: prices (context carries no ticket price) ──────────────
        for p in PRICE_RE.findall(body):
            g.warn(f'{label}: mentions price {p!r} (verify vs. real pricing)', body)

        # ── grounding: calendar dates must match the event date ──────────────
        if event is not None:
            for dt in DATE_RE.findall(body):
                if dt.strip().lower() not in ' '.join(allowed_dates):
                    if not any(dt.strip().lower() in v or v in dt.strip().lower()
                               for v in allowed_dates):
                        g.warn(f'{label}: date {dt!r} not in event data', body)

        # ── schedule ─────────────────────────────────────────────────────────
        send_at = step.get('send_at')
        dt = None
        if send_at:
            try:
                dt = datetime.fromisoformat(send_at)
            except ValueError:
                g.fail(f'{label}: unparseable send_at {send_at!r}')
        if dt is not None:
            # A dump hand-edited to a naive timestamp would otherwise raise TypeError when
            # compared to the tz-aware `now`; assume the report's timezone for such values.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)
            if dt < now:
                g.fail(f'{label}: scheduled in the past ({step.get("timing_label")})')
            if event_start and dt >= event_start:
                g.fail(f'{label}: scheduled at/after doors ({step.get("timing_label")})')

        # ── cadence (event plans) ────────────────────────────────────────────
        offset = step.get('offset_days')
        if event is not None and isinstance(offset, int):
            if prev_offset is not None and offset > prev_offset:
                g.warn(f'{label}: offset {offset} rises after {prev_offset} (should descend)')
            if days_until is not None and days_until >= 0 and offset > days_until:
                g.warn(f'{label}: offset {offset}d exceeds {days_until}d runway')
            prev_offset = offset

    if allowed_url and not link_seen and steps:
        g.warn('ticket link never appears in any step')

    return g


class Command(BaseCommand):
    help = "Offline quality eval for AI SMS campaign plans (live LLM; read-only, no plans saved)."

    def add_arguments(self, parser):
        parser.add_argument('--org', help='Organization slug (default: first strategist-enabled org)')
        parser.add_argument('--event', help='Event UUID for the event scenarios (default: nearest upcoming)')
        parser.add_argument('--samples', type=int, default=2, help='Generations per scenario (default 2)')
        parser.add_argument('--goals', choices=['event', 'segment', 'all'], default='all')
        parser.add_argument('--ticket-url', default=DEFAULT_TICKET_URL, help='Synthetic ticket link for event scenarios')
        parser.add_argument('--judge', action='store_true', help='Add an LLM-as-judge pass (brand voice + persuasion)')
        parser.add_argument('--json', dest='json_out', help='Dump raw generated plans to this path')
        parser.add_argument('--from-json', dest='json_in', help='Re-grade plans from a prior --json dump (no LLM calls)')
        parser.add_argument('--rate', action='store_true',
                            help='Blind human-rating mode to calibrate the judge (needs --from-json and a TTY)')
        parser.add_argument('--limit', type=int, help='In --rate mode, rate at most N randomly-sampled plans')

    def handle(self, *args, **opts):
        if opts.get('rate'):
            if not opts.get('json_in'):
                raise CommandError('--rate requires --from-json <dump> — a saved run to rate.')
            self._rate_and_calibrate(opts['json_in'], opts)
            return
        if opts['json_in']:
            self._regrade_from_json(opts['json_in'], opts)
            return

        org = self._resolve_org(opts.get('org'))
        tz = org.get_timezone()
        now = timezone.now().astimezone(tz)
        event = self._resolve_event(org, opts.get('event')) if opts['goals'] in ('event', 'all') else None

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== SMS plan eval · {org.name} ({org.slug}) ==="))
        if opts['goals'] in ('event', 'all'):
            if event is None:
                self.stdout.write(self.style.WARNING("No usable event found — skipping event scenarios."))
            else:
                self.stdout.write(f"Event: {event.name} · {event.start_date} · runway "
                                  f"{(event.start_date - now.date()).days}d")
        self.stdout.write(f"Samples per goal: {opts['samples']}   Judge: {'on' if opts['judge'] else 'off'}\n")

        scenarios = []
        if opts['goals'] in ('event', 'all') and event is not None:
            for goal in EVENT_GOALS:
                scenarios.append(('event', goal, event, None))
        if opts['goals'] in ('segment', 'all'):
            for goal, seg in SEGMENT_GOALS:
                scenarios.append(('segment', f'{goal}', None, {'rfm_segment': [seg]}))

        from tickets.services.sms_strategist import generate_campaign_plan, SMSStrategistError

        dump = []
        worst = []  # (goal, note, message) for the worst-offenders section
        for kind, goal, ev, criteria in scenarios:
            grades = []
            raw_plans = []
            errors = 0
            for _ in range(opts['samples']):
                try:
                    plan = generate_campaign_plan(
                        org, event=ev, criteria=criteria, objective=goal,
                        ticket_url=opts['ticket_url'] if ev is not None else '',
                    )
                except SMSStrategistError as exc:
                    errors += 1
                    self.stderr.write(self.style.ERROR(f"  [{goal}] generation failed: {exc}"))
                    continue
                raw_plans.append(plan)
                grades.append(grade_plan(
                    plan, event=ev, ticket_url=opts['ticket_url'] if ev is not None else '',
                    tz=tz, now=now,
                ))
            dump.append({'kind': kind, 'goal': goal, 'org_slug': org.slug,
                         'event_id': str(ev.id) if ev else None,
                         'ticket_url': opts['ticket_url'] if ev is not None else '',
                         'criteria': criteria, 'plans': raw_plans})
            self._report_scenario(kind, goal, grades, errors)
            # Reuse the grades just computed rather than re-grading in _print_worst.
            for g in grades:
                worst.extend((goal, note, msg) for sev, note, msg in g.examples if sev == 'FAIL')

        if opts['judge']:
            self._run_judge(org, dump)

        if opts['json_out']:
            with open(opts['json_out'], 'w') as fh:
                json.dump(dump, fh, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(f"\nRaw plans written to {opts['json_out']}"))

        self._print_worst(worst)

    # ── scenario reporting ────────────────────────────────────────────────────
    def _report_scenario(self, kind, goal, grades, errors):
        n = len(grades)
        if not n:
            self.stdout.write(f"  {self.style.ERROR('✗')} [{kind}] {goal[:40]:40s} — all {errors} samples errored")
            return
        passed = sum(1 for g in grades if g.passed)
        total_fails = sum(len(g.fails) for g in grades)
        total_warns = sum(len(g.warns) for g in grades)
        rate = passed / n
        mark = self.style.SUCCESS('✓') if rate == 1 else (
            self.style.WARNING('~') if rate >= 0.5 else self.style.ERROR('✗'))
        line = (f"  {mark} [{kind}] {goal[:40]:40s} "
                f"clean {passed}/{n}   fails={total_fails} warns={total_warns}")
        if errors:
            line += f"   errors={errors}"
        self.stdout.write(line)
        # Surface the distinct failure notes so a bad pattern is visible at a glance.
        seen = []
        for g in grades:
            for f in g.fails:
                key = re.sub(r'step \d+ \(\w+\)', 'step', f)
                if key not in seen:
                    seen.append(key)
                    self.stdout.write(f"        {self.style.ERROR('FAIL')} {f}")

    # ── worst offenders ───────────────────────────────────────────────────────
    def _print_worst(self, worst):
        """Print the FAIL example messages collected during grading (no re-grading)."""
        if not worst:
            return
        self.stdout.write(self.style.HTTP_INFO("\n── Sample failing messages (eyeball these) ──"))
        for goal, note, msg in worst[:12]:
            self.stdout.write(f"  {self.style.ERROR(note)}  [{goal[:28]}]")
            self.stdout.write(f"     {msg!r}")

    # ── LLM judge ─────────────────────────────────────────────────────────────
    def _judge(self, org, plan):
        try:
            from langchain_openai import ChatOpenAI
            from pydantic import BaseModel, Field
            from django.conf import settings
            from tickets.services.sms_strategist import _recent_campaign_bodies

            class Verdict(BaseModel):
                brand_voice: int = Field(ge=1, le=5, description="Match to the org's own voice samples.")
                persuasion: int = Field(ge=1, le=5, description="Likelihood to drive the objective.")
                coherence: int = Field(ge=1, le=5, description="Sequence reads as distinct, escalating touches.")
                notes: str = Field(description="One sentence: the biggest weakness.")

            voice = _recent_campaign_bodies(org) or ['(no prior messages)']
            bodies = [s.get('body') for s in (plan.get('steps') or [])]
            prompt = (
                "You are a strict SMS marketing reviewer. Score this generated campaign "
                "sequence 1-5 on three axes. Be harsh; 5 is exceptional.\n\n"
                f"THE ORG'S OWN PAST MESSAGES (voice to match):\n{json.dumps(voice, indent=2)}\n\n"
                f"STRATEGY: {plan.get('strategy_summary')}\n"
                f"GENERATED MESSAGES:\n{json.dumps(bodies, indent=2)}"
            )
            llm = ChatOpenAI(model=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
                             api_key=getattr(settings, 'OPENAI_API_KEY', ''), temperature=0)
            v = llm.with_structured_output(Verdict).invoke(prompt)
            return {'brand_voice': v.brand_voice, 'persuasion': v.persuasion,
                    'coherence': v.coherence, 'notes': v.notes}
        except Exception as exc:  # judge is best-effort — never crash the eval
            self.stderr.write(self.style.WARNING(f"  judge unavailable: {exc}"))
            return None

    AXES = ('brand_voice', 'persuasion', 'coherence')

    def _run_judge(self, org, dump):
        """Judge every plan, printing per-goal averages + the 'weakness' notes, then an
        overall rollup with the weakest goals called out. Used by both a live run and the
        --from-json re-judge path (so the judge can be re-run without regenerating plans)."""
        self.stdout.write(self.style.HTTP_INFO(
            "\n── LLM-as-judge (brand voice / persuasion / coherence, 1-5) ──"))
        per_goal = []          # [(goal, [verdict, ...]), ...]
        all_verdicts = []
        for row in dump:
            # Keep verdicts aligned 1:1 with plans (None on judge failure) and cache them
            # onto the row so a later --json dump / --rate calibration can reuse them.
            aligned = row.get('judge') or [self._judge(org, p) for p in row.get('plans', [])]
            row['judge'] = aligned
            verdicts = [v for v in aligned if v]
            if not verdicts:
                continue
            per_goal.append((row['goal'], verdicts))
            all_verdicts.extend(verdicts)
            self._print_goal_judge(row['goal'], verdicts)
        self._judge_rollup(all_verdicts, per_goal)

    @staticmethod
    def _avg(verdicts, axis):
        return sum(v[axis] for v in verdicts) / len(verdicts)

    def _print_goal_judge(self, goal, verdicts):
        avgs = '  '.join(f'{axis.split("_")[0]} {self._avg(verdicts, axis):.1f}' for axis in self.AXES)
        self.stdout.write(f"  {goal[:34]:34s} {avgs}")
        # The judge's one-line weakness per sample — the 'why' behind the scores.
        for v in verdicts:
            note = (v.get('notes') or '').strip()
            if note:
                self.stdout.write(f"        - {note}")

    def _judge_rollup(self, all_verdicts, per_goal):
        if not all_verdicts:
            return
        self.stdout.write(self.style.HTTP_INFO("\n  Overall"))
        for axis in self.AXES:
            vals = [v[axis] for v in all_verdicts]
            avg = sum(vals) / len(vals)
            self.stdout.write(f"    avg {axis:11s} {avg:.2f}  (min {min(vals)}, n={len(vals)})")
        # Call out the goals with the lowest brand-voice match — the usual priority axis.
        if len(per_goal) > 1:
            ranked = sorted(per_goal, key=lambda gv: self._avg(gv[1], 'brand_voice'))
            self.stdout.write("  Weakest brand voice:")
            for goal, verdicts in ranked[:3]:
                self.stdout.write(f"    {self._avg(verdicts, 'brand_voice'):.1f}  {goal}")

    # ── resolvers ─────────────────────────────────────────────────────────────
    def _resolve_org(self, slug):
        qs = Organization.objects.all()
        if slug:
            org = qs.filter(slug=slug).first()
            if not org:
                raise CommandError(f"No organization with slug {slug!r}")
            return org
        org = qs.filter(ai_sms_strategist_enabled=True).first() or qs.first()
        if not org:
            raise CommandError("No organizations exist.")
        return org

    def _resolve_event(self, org, event_id):
        base = Event.objects.filter(organization=org, deleted_at__isnull=True)
        if event_id:
            ev = base.filter(id=event_id).first()
            if not ev:
                raise CommandError(f"No event {event_id!r} in {org.slug}")
            return ev
        today = timezone.localdate()
        return (base.filter(start_date__gte=today).order_by('start_date').first()
                or base.order_by('-start_date').first())

    # ── re-grade / re-judge from a dump ───────────────────────────────────────
    def _regrade_from_json(self, path, opts):
        """Re-run the deterministic grader on saved plans (free), and — with --judge —
        re-score them with the LLM judge. Decouples grading/judging from generation, so
        you can iterate on the grader or judge against a fixed baseline, or judge a dump
        captured earlier without --judge, without paying to regenerate plans."""
        with open(path) as fh:
            dump = json.load(fh)
        mode = "re-grade + re-judge" if opts.get('judge') else "re-grade (no LLM calls)"
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {mode}: {path} ==="))
        worst = []
        for row in dump:
            ev = Event.objects.filter(id=row['event_id']).first() if row.get('event_id') else None
            tz = ev.organization.get_timezone() if ev else timezone.get_current_timezone()
            now = timezone.now().astimezone(tz)
            grades = [grade_plan(p, event=ev, ticket_url=row.get('ticket_url') or '', tz=tz, now=now)
                      for p in row.get('plans', [])]
            self._report_scenario(row['kind'], row['goal'], grades, 0)
            for g in grades:
                worst.extend((row['goal'], note, msg) for sev, note, msg in g.examples if sev == 'FAIL')
        self._print_worst(worst)
        if opts.get('judge'):
            # The judge needs the org for its brand-voice reference; prefer --org, else the
            # slug saved in the dump, else the default strategist-enabled org.
            slug = opts.get('org') or (dump[0].get('org_slug') if dump else None)
            self._run_judge(self._resolve_org(slug), dump)

    # ── judge calibration: blind human rating vs the judge ────────────────────
    def _rate_and_calibrate(self, path, opts):
        """Walk the rater through scoring plans blind (judge scores hidden), then report
        how well the judge agrees with them. This is Phase 0: decide whether the judge is
        trustworthy enough to optimize against before spending on prompt experiments."""
        if not sys.stdin.isatty():
            raise CommandError('--rate is interactive; run it in a terminal.')
        with open(path) as fh:
            dump = json.load(fh)
        slug = opts.get('org') or (dump[0].get('org_slug') if dump else None)
        org = self._resolve_org(slug)

        # Ensure every plan has a judge verdict (reuse cached ones from the dump; only call
        # the LLM for any that are missing).
        missing = sum(1 for r in dump if not r.get('judge'))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"{missing} scenario(s) have no cached judge scores — scoring them now (LLM cost)…"))
        for row in dump:
            if not row.get('judge'):
                row['judge'] = [self._judge(org, p) for p in row.get('plans', [])]

        # Flatten to (goal, plan, verdict), keep only plans the judge actually scored.
        items = []
        for row in dump:
            verdicts = row.get('judge') or []
            for i, plan in enumerate(row.get('plans', [])):
                v = verdicts[i] if i < len(verdicts) else None
                if v:
                    items.append((row['goal'], plan, v))
        if not items:
            raise CommandError('No judged plans to rate.')
        # Sample (fixed seed = reproducible selection) and shuffle so rating order doesn't
        # track goal order (reduces anchoring).
        rng = random.Random(0)
        rng.shuffle(items)
        if opts.get('limit'):
            items = items[:opts['limit']]

        voice = None
        try:
            from tickets.services.sms_strategist import _recent_campaign_bodies
            voice = _recent_campaign_bodies(org)
        except Exception:
            pass

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Judge calibration · {org.name} · {len(items)} plans to rate ==="))
        self.stdout.write(
            "Score each plan 1-5 on three axes (judge scores are HIDDEN until the end).\n"
            "  brand_voice = sounds like THIS org   persuasion = would drive the goal   "
            "coherence = distinct, escalating touches\n"
            "Enter = skip a plan · q = stop and show results.\n")
        if voice:
            self.stdout.write(self.style.HTTP_INFO("This org's real messages (the voice to match):"))
            for b in voice[:6]:
                self.stdout.write(f"    {b!r}")
            self.stdout.write("")

        rated = []  # (verdict, {axis: human_score})
        for n, (goal, plan, verdict) in enumerate(items, 1):
            self.stdout.write(self.style.HTTP_INFO(f"\n[{n}/{len(items)}] Goal: {goal}"))
            self.stdout.write(f"  strategy: {plan.get('strategy_summary')}")
            for j, step in enumerate(plan.get('steps') or [], 1):
                self.stdout.write(f"    {j}. ({step.get('purpose')}) {step.get('body')}")
            human = self._prompt_scores()
            if human == 'quit':
                break
            if human:
                rated.append((verdict, human))

        self._print_calibration(rated)
        out = path.rsplit('.', 1)[0] + '.ratings.json'
        with open(out, 'w') as fh:
            json.dump([{'human': h, 'judge': {a: v[a] for a in self.AXES}} for v, h in rated],
                      fh, indent=2)
        self.stdout.write(self.style.SUCCESS(f"\nRatings saved to {out}"))

    def _prompt_scores(self):
        """Prompt for the three axis scores. Returns a dict, {} to skip, or 'quit'."""
        scores = {}
        for axis in self.AXES:
            while True:
                raw = input(f"    {axis} (1-5, Enter=skip plan, q=quit): ").strip().lower()
                if raw == 'q':
                    return 'quit'
                if raw == '':
                    return {}  # skip this whole plan
                if raw in ('1', '2', '3', '4', '5'):
                    scores[axis] = int(raw)
                    break
                self.stdout.write("      please enter 1-5, Enter, or q")
        return scores

    def _print_calibration(self, rated):
        if not rated:
            self.stdout.write(self.style.WARNING("\nNothing rated — no calibration to report."))
            return
        self.stdout.write(self.style.HTTP_INFO(f"\n── Judge vs you (n={len(rated)}) ──"))
        self.stdout.write(f"  {'axis':12s} {'you':>5s} {'judge':>6s} {'MAE':>5s} {'±1':>5s} {'spearman':>9s}")
        verdict_lines = []
        for axis in self.AXES:
            h = [r[1][axis] for r in rated]
            j = [r[0][axis] for r in rated]
            mae = sum(abs(a - b) for a, b in zip(h, j)) / len(h)
            within1 = sum(1 for a, b in zip(h, j) if abs(a - b) <= 1) / len(h)
            rho = _spearman(h, j)
            self.stdout.write(f"  {axis:12s} {sum(h)/len(h):5.2f} {sum(j)/len(j):6.2f} "
                              f"{mae:5.2f} {within1*100:4.0f}% {rho:9.2f}")
            verdict_lines.append((axis, rho, mae))
        # Plain-language read on whether the judge is trustworthy as a relative signal.
        worst_rho = min(r for _, r, _ in verdict_lines)
        if worst_rho >= 0.5:
            msg, style = ("Judge tracks your ranking — trust it as a RELATIVE signal for experiments.",
                          self.style.SUCCESS)
        elif worst_rho >= 0.3:
            msg, style = ("Judge partly tracks you — usable but noisy; prefer larger sample sizes.",
                          self.style.WARNING)
        else:
            msg, style = ("Judge does NOT track you — fix the judge (try pairwise scoring) before "
                          "optimizing against it.", self.style.ERROR)
        self.stdout.write("\n  " + style(msg))
        self.stdout.write("  (Spearman = rank agreement; MAE = avg point gap; ±1 = share within one point.)")
