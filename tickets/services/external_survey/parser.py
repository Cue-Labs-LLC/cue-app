"""
Parse a Typeform-style CSV survey export into ExternalSurveyResponse rows.
"""
import csv
import json
import io
import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)

# Map column header prefixes (lowercased) to model field names.
# The first matching prefix wins.
_COLUMN_MAP = [
    ('start date',        'responded_at'),
    ('date',              'responded_at'),
    ('question 10',       'found_out_how'),
    ('question 11',       'text_feedback'),
    ('question 12',       'raffle_email'),
    ('question 1',        'overall_rating'),
    ('question 2',        'nps_score'),
    ('question 3',        'city'),
    ('question 4',        'enjoyed'),
    ('question 5',        'genres'),
    ('question 6',        'improvements'),
    ('question 7',        'crowd_vibe'),
    ('question 8',        'venue_feel'),
    ('question 9',        'pre_event_info'),
    ('email',             'email'),
]

_MULTI_SELECT_FIELDS = {'enjoyed', 'genres', 'improvements'}


def _resolve_headers(raw_headers):
    """Return a list of (col_index, field_name) tuples for recognised headers."""
    mapping = []
    for idx, header in enumerate(raw_headers):
        lower = header.strip().lower()
        for prefix, field in _COLUMN_MAP:
            if lower.startswith(prefix):
                mapping.append((idx, field))
                break
    return mapping


def _parse_multiselect(value):
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def _parse_nps(value):
    stripped = value.strip()
    if stripped.isdigit():
        score = int(stripped)
        if 0 <= score <= 10:
            return score
    return None


def _parse_datetime(value):
    stripped = value.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(stripped, fmt)
            return timezone.make_aware(dt)
        except ValueError:
            continue
    return None


class ExternalSurveyParser:
    def __init__(self, organization, upload):
        self.organization = organization
        self.upload = upload

    def parse(self, file_obj):
        from tickets.models import ExternalSurveyResponse

        if hasattr(file_obj, 'read'):
            raw = file_obj.read()
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8-sig')
            reader = csv.reader(io.StringIO(raw))
        else:
            reader = csv.reader(file_obj)

        rows = list(reader)
        if not rows:
            return {'rows_inserted': 0, 'rows_skipped': 0, 'errors': []}

        raw_headers = rows[0]
        col_mapping = _resolve_headers(raw_headers)

        errors = []
        batch = []
        rows_skipped = 0

        for row_num, row in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in row):
                rows_skipped += 1
                continue

            kwargs = {
                'organization': self.organization,
                'upload': self.upload,
                'enjoyed': [],
                'genres': [],
                'improvements': [],
            }

            responded_at = None

            for col_idx, field in col_mapping:
                if col_idx >= len(row):
                    continue
                value = row[col_idx]

                if field == 'responded_at':
                    responded_at = _parse_datetime(value)
                elif field == 'nps_score':
                    kwargs['nps_score'] = _parse_nps(value)
                elif field in _MULTI_SELECT_FIELDS:
                    kwargs[field] = _parse_multiselect(value)
                else:
                    kwargs[field] = value.strip()

            if responded_at is None:
                errors.append(f"Row {row_num}: could not parse responded_at")
                rows_skipped += 1
                continue

            kwargs['responded_at'] = responded_at
            batch.append(ExternalSurveyResponse(**kwargs))

        if batch:
            ExternalSurveyResponse.objects.bulk_create(batch, batch_size=500)

        self.upload.error_log = json.dumps(errors)
        return {
            'rows_inserted': len(batch),
            'rows_skipped': rows_skipped,
            'errors': errors,
        }
