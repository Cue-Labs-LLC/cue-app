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
