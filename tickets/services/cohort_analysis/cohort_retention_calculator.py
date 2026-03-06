"""
Monthly cohort retention analysis.
Groups customers by the month of their first event, then tracks what
percentage return in subsequent months.
"""
import pandas as pd
from tickets.models import TicketOrder


class CohortRetentionCalculator:
    def __init__(self, organization):
        self.organization = organization

    def calculate(self, max_periods=12):
        orders_qs = TicketOrder.objects.filter(
            customer__organization=self.organization,
            is_in_person=False,
        ).values('customer_id', 'event__start_date')

        rows = list(orders_qs)
        if not rows:
            return {'cohorts': [], 'summary': {'total_cohorts': 0, 'avg_m1_retention': 0, 'avg_m3_retention': 0}}

        df = pd.DataFrame(rows)
        df.rename(columns={'event__start_date': 'event_date'}, inplace=True)
        df['event_date'] = pd.to_datetime(df['event_date'])

        # Assign monthly period to each event
        df['event_month'] = df['event_date'].dt.to_period('M')

        # Each customer's cohort = month of their earliest event
        cohort_map = df.groupby('customer_id')['event_month'].min().reset_index()
        cohort_map.columns = ['customer_id', 'cohort_month']

        df = df.merge(cohort_map, on='customer_id')

        # Deduplicate: one entry per customer per event_month
        activity = df.drop_duplicates(subset=['customer_id', 'event_month'])

        # Compute period offset from cohort month (int64: period[M] cannot convert to int32)
        activity['period_offset'] = (
            activity['event_month'].astype('int64') - activity['cohort_month'].astype('int64')
        )

        # Group by cohort_month and period_offset to get unique customer counts
        cohort_sizes = cohort_map.groupby('cohort_month')['customer_id'].nunique()
        retention = activity.groupby(['cohort_month', 'period_offset'])['customer_id'].nunique().reset_index()
        retention.columns = ['cohort_month', 'period_offset', 'active_count']

        # Build cohort list
        cohorts_list = []
        all_m1 = []
        all_m3 = []

        for cohort_month in sorted(cohort_sizes.index):
            size = cohort_sizes[cohort_month]
            cohort_data = retention[retention['cohort_month'] == cohort_month].set_index('period_offset')

            periods = []
            for p in range(min(max_periods + 1, int(cohort_data.index.max()) + 1) if len(cohort_data) else 0):
                active = int(cohort_data.loc[p, 'active_count']) if p in cohort_data.index else 0
                pct = round(active / size * 100, 1) if size else 0
                periods.append({
                    'period': p,
                    'active': active,
                    'retention_pct': pct,
                    'bg_opacity': round(pct / 100, 2),
                })

            cohort_label = str(cohort_month)
            cohorts_list.append({
                'cohort': cohort_label,
                'size': int(size),
                'periods': periods,
            })

            # Collect M1/M3 retention for summary
            if len(periods) > 1:
                all_m1.append(periods[1]['retention_pct'])
            if len(periods) > 3:
                all_m3.append(periods[3]['retention_pct'])

        summary = {
            'total_cohorts': len(cohorts_list),
            'avg_m1_retention': round(sum(all_m1) / len(all_m1), 1) if all_m1 else 0,
            'avg_m3_retention': round(sum(all_m3) / len(all_m3), 1) if all_m3 else 0,
        }

        return {'cohorts': cohorts_list, 'summary': summary}
