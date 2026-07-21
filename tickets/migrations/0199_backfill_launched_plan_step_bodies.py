# Backfill each launched AI-plan step's body from its campaign's final sent text.
#
# Historically a step launched via the full composer (or the inline confirm) kept the
# pre-send body it was created with, while the actual campaign carried the organizer's
# composer edits + the per-campaign tracking link minted at send time. The step therefore
# displayed text that didn't match what was sent. The code fix syncs on launch going
# forward; this repairs steps launched before that fix by copying the campaign's body onto
# the step and recomputing its segment/encoding meter (same as _apply_step_body).
from django.db import migrations

from tickets.sms import sms_segment_info, with_stop_footer


def backfill_bodies(apps, schema_editor):
    SMSCampaignPlan = apps.get_model('tickets', 'SMSCampaignPlan')
    SMSCampaign = apps.get_model('tickets', 'SMSCampaign')

    for plan in SMSCampaignPlan.objects.all().iterator():
        steps = plan.steps or []
        ids = [s['launched_campaign_id'] for s in steps if s.get('launched_campaign_id')]
        if not ids:
            continue
        body_by_id = {
            str(cid): body for cid, body in
            SMSCampaign.objects.filter(
                organization_id=plan.organization_id, id__in=ids,
            ).values_list('id', 'body')
        }
        changed = False
        for step in steps:
            cid = step.get('launched_campaign_id')
            sent_body = body_by_id.get(str(cid)) if cid else None
            if sent_body is None or step.get('body') == sent_body:
                continue
            sent_body = sent_body[:1600]
            encoding, segments = sms_segment_info(with_stop_footer(sent_body))
            step['body'] = sent_body
            step['segments'] = segments
            step['encoding'] = encoding
            changed = True
        if changed:
            plan.steps = steps
            plan.save(update_fields=['steps'])


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0198_recompute_smscampaignplan_status'),
    ]

    operations = [
        migrations.RunPython(backfill_bodies, migrations.RunPython.noop),
    ]
