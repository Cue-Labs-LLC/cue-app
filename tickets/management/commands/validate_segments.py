"""Evaluate the accuracy/precision of RFM segment assignments.

Read-only diagnostics: does NOT change any stored scores or segments. Reports
internal correctness (score histograms, raw-value spread, 125-cell cube coverage,
segment size distribution) and predictive validity (an as-of-T backtest that
measures realized future behavior per segment with a Spearman separation score).

Usage::

    python manage.py validate_segments
    python manage.py validate_segments --org my-org
    python manage.py validate_segments --org my-org --holdout-days 180 --compare-canonical
    python manage.py validate_segments --org my-org --cutoff 2025-12-31
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.segmentation.validation import SegmentDiagnostics


class Command(BaseCommand):
    help = "Evaluate accuracy of RFM segment assignments (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--org", dest="org", help="Organization slug (default: all)")
        parser.add_argument("--holdout-days", dest="holdout_days", type=int, default=180)
        parser.add_argument("--cutoff", dest="cutoff", help="Backtest cutoff date YYYY-MM-DD")
        parser.add_argument(
            "--compare-canonical", dest="compare_canonical", action="store_true",
            help="Also score the candidate canonical 11-segment grid",
        )
        parser.add_argument(
            "--compare-absolute", dest="compare_absolute", action="store_true",
            help="Also score the candidate absolute / behavior-anchored bands",
        )

    def handle(self, *args, **options):
        orgs = Organization.objects.all()
        if options.get("org"):
            orgs = orgs.filter(slug=options["org"])
        orgs = list(orgs)
        if not orgs:
            raise CommandError("No matching organizations found.")

        cutoff = None
        if options.get("cutoff"):
            try:
                cutoff = datetime.strptime(options["cutoff"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--cutoff must be YYYY-MM-DD")

        for org in orgs:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {org.name} ({org.slug}) ==="))
            diag = SegmentDiagnostics(org).calculate(
                holdout_days=options["holdout_days"],
                cutoff=cutoff,
                compare_canonical=options["compare_canonical"],
                compare_absolute=options["compare_absolute"],
            )
            self._print_internal(diag["internal"])
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("Backtest — current rules"))
            self._print_backtest(diag["backtest"])
            if options["compare_canonical"]:
                self.stdout.write("")
                self.stdout.write(self.style.HTTP_INFO("Canonical 11-segment grid"))
                cov = diag["internal_canonical_coverage"]
                self.stdout.write(f"  cube coverage: {cov['coverage_pct']}%")
                self.stdout.write(self.style.HTTP_INFO("Backtest — canonical rules"))
                self._print_backtest(diag["backtest_canonical"])
            if options["compare_absolute"]:
                self.stdout.write("")
                self.stdout.write(self.style.HTTP_INFO("Backtest — absolute bands"))
                self._print_backtest(diag["backtest_absolute"])

    def _print_internal(self, internal):
        self.stdout.write(self.style.HTTP_INFO("Score histograms (1-5)"))
        for dim, counts in internal["score_histograms"].items():
            row = "  ".join(f"{score}:{counts[score]}" for score in range(1, 6))
            self.stdout.write(f"  {dim:9s} {row}")

        self.stdout.write(self.style.HTTP_INFO("Raw value spread"))
        for dim, s in internal["raw_value_stats"].items():
            if s is None:
                self.stdout.write(f"  {dim:9s} (no data)")
                continue
            # Degenerate bucketing: >=60% share one value, so >=3 score buckets collapse.
            flag = " <-- collapsed" if s["p20"] == s["p60"] else ""
            self.stdout.write(
                f"  {dim:9s} n={s['n']} distinct={s['n_distinct']} "
                f"min={s['min']:.0f} p20={s['p20']:.0f} p40={s['p40']:.0f} "
                f"p60={s['p60']:.0f} p80={s['p80']:.0f} max={s['max']:.0f}{flag}"
            )

        cov = internal["cube_coverage"]
        self.stdout.write(self.style.HTTP_INFO("Cube coverage (125 cells)"))
        self.stdout.write(
            f"  coverage: {cov['coverage_pct']}% "
            f"({cov['covered_cells']}/{cov['total_cells']}), "
            f"{len(cov['uncovered_cells'])} cells fall through to catch-all"
        )
        cell_counts = sorted(cov["segment_cell_counts"].items(), key=lambda kv: -kv[1])
        self.stdout.write("  cells per segment: " + ", ".join(f"{k}={v}" for k, v in cell_counts))

        dist = internal["segment_size_distribution"]
        self.stdout.write(self.style.HTTP_INFO("Segment sizes"))
        for name, info in dist["by_segment"].items():
            self.stdout.write(f"  {name:16s} {info['count']:6d}  {info['pct']}%")
        if dist["over_concentrated"]:
            self.stdout.write(
                self.style.WARNING(
                    "  over-concentrated (>40%): " + ", ".join(dist["over_concentrated"])
                )
            )

    def _print_backtest(self, bt):
        if bt.get("status") != "ok":
            self.stdout.write(self.style.WARNING(f"  {bt.get('status')}: {bt}"))
            return
        self.stdout.write(
            f"  cutoff={bt['cutoff']} segmented={bt['n_segmented_customers']} "
            f"holdout_orders={bt['holdout_orders']}"
        )
        self.stdout.write(f"  {'segment':16s} {'n':>6s} {'repeat':>8s} {'avg_future_rev':>15s}")
        for name, s in sorted(
            bt["per_segment"].items(), key=lambda kv: -kv[1]["avg_future_revenue"]
        ):
            self.stdout.write(
                f"  {name:16s} {s['n']:6d} {s['repeat_rate']*100:7.1f}% "
                f"{s['avg_future_revenue']:15.2f}"
            )
        sep = bt["separation"]
        self.stdout.write(
            f"  separation: spearman(revenue)={sep['spearman_future_revenue']} "
            f"spearman(repeat)={sep['spearman_repeat_rate']} "
            f"violations={sep['monotonic_violations']}"
        )
