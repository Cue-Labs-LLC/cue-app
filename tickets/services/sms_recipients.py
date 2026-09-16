"""Resolve a pasted list of raw phone numbers into an SMS audience.

Organizers can paste phone numbers straight into the composer instead of picking a
filter/tag audience. Every pasted number is classified against the org's contacts
so we only send to *subscribed* contacts and can show the organizer, up front, how
many are unsubscribed / unreachable / not in their contacts.

The buckets mirror how ``SMSCampaign.materialize()`` silently drops numbers, but
here we count them per-reason instead of dropping silently:

- matched       — an opted-in org contact, not suppressed, valid & in-country. Sends.
- unsubscribed  — a STOP/opt-out (``PhoneSuppression`` STOP/MANUAL) or a contact
                  with ``sms_opt_in=False``.
- unreachable   — malformed number, out-of-country (geo-blocked), or a hard bounce
                  (``PhoneSuppression`` BOUNCE).
- not_contact   — a valid number that matches no contact in the org.

Only ``matched_customer_ids`` become recipients; the caller feeds them through the
normal ``manual_include_ids`` path so dedupe/suppression/cap/charging are unchanged.
"""
import re

from django.conf import settings
from django.db.models import Q

# Split a pasted blob on any run of whitespace, commas, or semicolons.
_SPLIT_RE = re.compile(r'[\s,;]+')
# How many example numbers to surface per non-matched bucket in the UI.
_SAMPLE_LIMIT = 5


def _paste_cap():
    return getattr(settings, 'SMS_PASTE_MAX_RECIPIENTS', 10000)


# Per-number deliverability outcomes. These mirror, in order, the reasons
# ``SMSCampaign.materialize()`` drops a candidate from an audience — so a subscriber
# labeled anything other than DELIVERY_DELIVERABLE genuinely won't receive a send.
DELIVERY_DELIVERABLE = 'deliverable'
DELIVERY_SUPPRESSED = 'suppressed'   # replied STOP / manual opt-out / hard bounce
DELIVERY_INVALID = 'invalid'         # malformed E.164 (e.g. doubled country code)
DELIVERY_COUNTRY = 'country'         # outside SMS_ALLOWED_COUNTRY_PREFIXES
DELIVERY_NO_PHONE = 'no_phone'       # opted in but no phone on file

# Human labels for the CSV export column (see customer_export_csv).
DELIVERABILITY_LABELS = {
    DELIVERY_DELIVERABLE: 'Deliverable',
    DELIVERY_SUPPRESSED: 'Opted out',
    DELIVERY_INVALID: 'Invalid number',
    DELIVERY_COUNTRY: 'Intl (unsupported)',
    DELIVERY_NO_PHONE: 'No phone',
}


def deliverability_status(raw_phone, suppressed_phones):
    """Classify one contact's phone into a DELIVERY_* outcome.

    ``suppressed_phones`` is a set of normalized E.164 numbers the caller fetched once
    (e.g. via ``PhoneSuppression.suppressed_phones(org)``) so this stays a pure, DB-free
    check usable per-row. Order matches ``SMSCampaign.materialize()``:
    suppressed → invalid format → non-allowed country. Dedupe is aggregate-only and is
    handled by ``subscriber_reachability`` (a single number can't be "the duplicate").
    """
    from tickets.sms import normalize_phone, is_plausible_e164, sms_country_allowed

    raw = (raw_phone or '').strip()
    if not raw:
        return DELIVERY_NO_PHONE
    phone = normalize_phone(raw)
    if not phone:
        return DELIVERY_NO_PHONE
    if phone in suppressed_phones:
        return DELIVERY_SUPPRESSED
    if not is_plausible_e164(phone):
        return DELIVERY_INVALID
    if not sms_country_allowed(phone):
        return DELIVERY_COUNTRY
    return DELIVERY_DELIVERABLE


def subscriber_reachability(org, subscriber_qs):
    """Aggregate how many subscribers in ``subscriber_qs`` a send would actually reach.

    Iterates the queryset's (id, phone) pairs once against a single
    ``PhoneSuppression.suppressed_phones(org)`` set, attributing each non-deliverable
    subscriber to its reason and deduping deliverable numbers by normalized phone so
    ``contactable`` matches a real campaign's materialized audience. Returns:

        {total, contactable, unreachable, suppressed, invalid, country, no_phone, duplicate}

    where total == contactable + unreachable and
    unreachable == duplicate + suppressed + invalid + country + no_phone.
    Callers scope ``subscriber_qs`` to opted-in customers (the "subscribers" universe).
    """
    from tickets.models import PhoneSuppression
    from tickets.sms import normalize_phone

    suppressed = PhoneSuppression.suppressed_phones(org)
    result = {
        'total': 0, 'contactable': 0, 'suppressed': 0,
        'invalid': 0, 'country': 0, 'no_phone': 0, 'duplicate': 0,
    }
    seen = set()
    for _cid, phone in subscriber_qs.values_list('id', 'phone').iterator():
        result['total'] += 1
        status = deliverability_status(phone, suppressed)
        if status != DELIVERY_DELIVERABLE:
            result[status] += 1
            continue
        norm = normalize_phone((phone or '').strip())
        if norm in seen:
            result['duplicate'] += 1
            continue
        seen.add(norm)
        result['contactable'] += 1
    result['unreachable'] = result['total'] - result['contactable']
    return result


def classify_pasted_phones(org, raw_text, cap=None):
    """Classify a pasted blob of phone numbers against ``org``'s contacts.

    Returns a dict:
        {
          'total_pasted': int,     # non-empty tokens the organizer pasted
          'unique': int,           # distinct valid-format numbers
          'duplicates': int,       # pasted entries dropped as repeats
          'over_cap': bool,        # total_pasted > cap → caller blocks the send
          'cap': int,
          'matched_customer_ids': [str, ...],   # subscribed contacts → recipients
          'counts': {'matched','unsubscribed','unreachable','not_contact'},
          'samples': {'unsubscribed':[...], 'unreachable':[...], 'not_contact':[...]},
        }
    """
    from tickets.models import Customer, PhoneSuppression
    from tickets.sms import normalize_phone, is_plausible_e164, sms_country_allowed

    cap = cap or _paste_cap()
    tokens = [t for t in _SPLIT_RE.split((raw_text or '').strip()) if t]
    total_pasted = len(tokens)

    counts = {'matched': 0, 'unsubscribed': 0, 'unreachable': 0, 'not_contact': 0}
    samples = {'unsubscribed': [], 'unreachable': [], 'not_contact': []}
    matched_customer_ids = []

    def _sample(bucket, value):
        if len(samples[bucket]) < _SAMPLE_LIMIT:
            samples[bucket].append(value)

    # Normalize + dedupe. Malformed numbers are unreachable and never reach a lookup.
    seen = set()
    uniques = []          # (normalized_phone, original_token)
    duplicates = 0
    for tok in tokens:
        p = normalize_phone(tok)
        if not is_plausible_e164(p):
            counts['unreachable'] += 1
            _sample('unreachable', tok)
            continue
        if p in seen:
            duplicates += 1
            continue
        seen.add(p)
        uniques.append((p, tok))

    if not uniques:
        return {
            'total_pasted': total_pasted, 'unique': 0, 'duplicates': duplicates,
            'over_cap': total_pasted > cap, 'cap': cap,
            'matched_customer_ids': [], 'counts': counts, 'samples': samples,
        }

    # Build normalized-phone → contact map for the org (prefer an opted-in contact
    # when two stored numbers normalize to the same E.164). Python match mirrors
    # materialize()/set_sms_opt_in so behavior is identical on SQLite and Postgres.
    phone_to_customer = {}
    for c in (Customer.objects.filter(organization=org)
              .exclude(phone='').only('id', 'phone', 'sms_opt_in')):
        norm = normalize_phone(c.phone)
        if not norm:
            continue
        existing = phone_to_customer.get(norm)
        if existing is None or (c.sms_opt_in and not existing.sms_opt_in):
            phone_to_customer[norm] = c

    # Suppression reasons for just the pasted numbers (org-specific OR global).
    unique_phones = [p for p, _ in uniques]
    supp_reasons = {}
    for phone, reason in (PhoneSuppression.objects
                          .filter(Q(organization=org) | Q(organization__isnull=True),
                                  phone__in=unique_phones)
                          .values_list('phone', 'reason')):
        supp_reasons.setdefault(phone, set()).add(reason)

    stop_reasons = {PhoneSuppression.Reason.TWILIO_STOP, PhoneSuppression.Reason.MANUAL}

    for p, tok in uniques:
        if not sms_country_allowed(p):
            counts['unreachable'] += 1
            _sample('unreachable', tok)
            continue
        reasons = supp_reasons.get(p)
        if reasons:
            if reasons & stop_reasons:
                counts['unsubscribed'] += 1
                _sample('unsubscribed', tok)
                continue
            if PhoneSuppression.Reason.BOUNCE in reasons:
                counts['unreachable'] += 1
                _sample('unreachable', tok)
                continue
        customer = phone_to_customer.get(p)
        if customer is None:
            counts['not_contact'] += 1
            _sample('not_contact', tok)
        elif customer.sms_opt_in:
            counts['matched'] += 1
            matched_customer_ids.append(str(customer.id))
        else:
            counts['unsubscribed'] += 1
            _sample('unsubscribed', tok)

    return {
        'total_pasted': total_pasted,
        'unique': len(uniques),
        'duplicates': duplicates,
        'over_cap': total_pasted > cap,
        'cap': cap,
        'matched_customer_ids': matched_customer_ids,
        'counts': counts,
        'samples': samples,
    }
