# Recompute every SMSCampaignPlan.status under the new four-state scheme
# (Draft / In progress / Scheduled / Sent). The label is derived from the live
# campaign status of each step, so existing plans saved under the old two-state
# scheme (Draft until every step launched, then In progress) get corrected here.
from django.db import migrations


def recompute_status(apps, schema_editor):
    SMSCampaignPlan = apps.get_model('tickets', 'SMSCampaignPlan')
    SMSCampaign = apps.get_model('tickets', 'SMSCampaign')

    for plan in SMSCampaignPlan.objects.all().iterator():
        steps = plan.steps or []
        total = len(steps)
        ids = [s['launched_campaign_id'] for s in steps if s.get('launched_campaign_id')]
        status_by_id = {}
        if ids:
            status_by_id = {
                str(cid): status for cid, status in
                SMSCampaign.objects.filter(
                    organization_id=plan.organization_id, id__in=ids,
                ).values_list('id', 'status')
            }

        draft = scheduled = sent = 0
        for step in steps:
            cid = step.get('launched_campaign_id')
            campaign_status = status_by_id.get(str(cid)) if cid else None
            if campaign_status in ('sending', 'sent'):
                sent += 1
            elif campaign_status == 'scheduled':
                scheduled += 1
            else:
                # Unlaunched, deleted, or canceled/failed → needs action.
                draft += 1

        if total and sent == total:
            new_status = 'sent'
        elif draft == total:
            new_status = 'draft'
        elif draft == 0:
            new_status = 'scheduled'
        else:
            new_status = 'in_progress'

        if plan.status != new_status:
            plan.status = new_status
            plan.save(update_fields=['status'])


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0197_alter_smscampaignplan_status'),
    ]

    operations = [
        migrations.RunPython(recompute_status, migrations.RunPython.noop),
    ]
