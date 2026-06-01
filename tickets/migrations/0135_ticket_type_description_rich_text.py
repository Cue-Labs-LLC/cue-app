"""Backfill plain-text ticket-type descriptions to HTML for rich-text rendering.

Wraps existing newline-separated descriptions in <p>/<br> so they render
correctly after the buy-page template switches from |linebreaksbr to |safe.
Reverse migration is a no-op; HTML in the column is still readable text,
just slightly noisier if the new template were rolled back.
"""
import html
import re

from django.db import migrations


_HAS_HTML_RE = re.compile(r"<[a-zA-Z/]")


def _to_html(text):
    paragraphs = re.split(r"\n{2,}", text.strip())
    out = []
    for p in paragraphs:
        escaped = html.escape(p)
        out.append("<p>" + escaped.replace("\n", "<br>") + "</p>")
    return "".join(out)


def backfill(apps, schema_editor):
    SaleableTicketType = apps.get_model("tickets", "SaleableTicketType")
    qs = SaleableTicketType.objects.exclude(description="").exclude(description__isnull=True)

    to_update = []
    for tt in qs.iterator():
        desc = tt.description or ""
        if _HAS_HTML_RE.search(desc):
            continue
        tt.description = _to_html(desc)
        to_update.append(tt)
        if len(to_update) >= 500:
            SaleableTicketType.objects.bulk_update(to_update, ["description"], batch_size=500)
            to_update = []
    if to_update:
        SaleableTicketType.objects.bulk_update(to_update, ["description"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0134_organization_mailchimp_campaign_title_hints"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
