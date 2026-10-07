"""Langfuse experiment runner for the Instagram DM support agent.

Syncs the version-controlled corpus (evals/ig_support_agent/cases.jsonl) into a Langfuse
dataset (idempotent, keyed by a stable per-item id), then runs an experiment: for each
case it executes the real pipeline (answer -> classify -> auto-send gate) inside a
Langfuse trace and attaches deterministic boolean scores (grounded, escalation_correct,
tool_correct, no_leak). Subjective answer quality is best added as a Langfuse managed
LLM-as-judge evaluator configured in the UI against this dataset — that way judges evolve
without code changes and apply to every future run.

LIVE, metered OpenAI calls (agent + classifier). Needs LANGFUSE_* + OPENAI_API_KEY.

    python manage.py eval_ig_agent --org familiar-faces
    python manage.py eval_ig_agent --sync-only          # just push the dataset
"""

import concurrent.futures
import hashlib
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.instagram.evaluation import (
    grade_escalation, grade_grounded, grade_tool, judge_private_disclosure,
    load_cases, run_ig_agent,
)

DEFAULT_DATASET = 'ig-support-agent'
DEFAULT_CASES = Path(settings.BASE_DIR) / 'evals' / 'ig_support_agent' / 'cases.jsonl'


def _run_in_thread(fn, *args):
    """Run fn(*args) in a worker thread and block for the result.

    Langfuse runs tasks/evaluators inside its asyncio event loop; Django forbids sync ORM
    from an async context, and this keeps our LLM/ORM work off the loop thread.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


class Command(BaseCommand):
    help = "Run the Instagram support-agent eval as a Langfuse experiment."

    def add_arguments(self, parser):
        import os

        parser.add_argument('--org', default=os.environ.get('IG_EVAL_ORG_SLUG', ''),
                            help='Org slug to answer as (default: IG_EVAL_ORG_SLUG).')
        parser.add_argument('--dataset', default=DEFAULT_DATASET,
                            help=f'Langfuse dataset name (default: {DEFAULT_DATASET}).')
        parser.add_argument('--cases', default=str(DEFAULT_CASES),
                            help='Path to the JSONL corpus.')
        parser.add_argument('--run-name', default='',
                            help='Experiment run name (default: auto).')
        parser.add_argument('--concurrency', type=int, default=4,
                            help='Max concurrent cases (default: 4).')
        parser.add_argument('--sync-only', action='store_true',
                            help='Only upsert the dataset from cases.jsonl; do not run.')

    def handle(self, *args, **opts):
        try:
            from langfuse import Evaluation, Langfuse
        except ImportError:
            raise CommandError("langfuse is not installed. Run `pip install -r requirements.txt`.")

        if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
            raise CommandError("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (see .env.example).")

        org = None
        if not opts['sync_only']:
            slug = opts['org']
            if not slug:
                raise CommandError("Pass --org <slug> or set IG_EVAL_ORG_SLUG.")
            try:
                org = Organization.objects.get(slug=slug)
            except Organization.DoesNotExist:
                raise CommandError(f"No organization with slug '{slug}'.")

        cases = load_cases(opts['cases'])
        if not cases:
            raise CommandError(f"No cases found in {opts['cases']}.")

        langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )

        dataset_name = opts['dataset']
        self._sync_dataset(langfuse, dataset_name, cases)
        self.stdout.write(self.style.SUCCESS(f"Synced {len(cases)} items into dataset '{dataset_name}'."))

        if opts['sync_only']:
            langfuse.flush()
            return

        def _task_body(message):
            # Runs in a worker thread (see task): no event loop here, so Django's
            # synchronous ORM is allowed. Close this thread's DB connection after.
            from django.db import connections
            try:
                return run_ig_agent(org, message)
            finally:
                connections.close_all()

        def task(*, item, **kwargs):
            # Langfuse runs the task inside its asyncio event loop, and Django forbids
            # synchronous ORM calls from an async context (SynchronousOnlyOperation).
            # Offload the ORM-touching pipeline to a worker thread and block for it.
            message = item.input if hasattr(item, 'input') else item['input']
            return _run_in_thread(_task_body, message)

        def ev_grounded(*, output, **kwargs):
            passed, comment = grade_grounded(output)
            return Evaluation(name='grounded', value=passed, data_type='BOOLEAN', comment=comment)

        def ev_escalation(*, output, metadata, **kwargs):
            res = grade_escalation(output, (metadata or {}).get('expected_category'))
            if res is None:
                return None
            passed, comment = res
            return Evaluation(name='escalation_correct', value=passed, data_type='BOOLEAN', comment=comment)

        def ev_tool(*, output, metadata, **kwargs):
            res = grade_tool(output, (metadata or {}).get('expected_tool'))
            if res is None:
                return None
            passed, comment = res
            return Evaluation(name='tool_correct', value=passed, data_type='BOOLEAN', comment=comment)

        # Semantic leak check via an in-code LLM-judge (a substring blocklist false-positives
        # on refusals that echo the term, e.g. "I can't share lifetime value"). Only runs on
        # rows that carry `forbid` (the adversarial cases); those terms become judge hints.
        # TODO(Phase 6): migrate to a Langfuse *managed* LLM-as-judge evaluator in the UI
        # (option B) — non-engineers tune the rubric without a deploy — keeping this in-code
        # judge as the headless/CI path. See the TDD Phase 6 + evals/README.md.
        def ev_no_disclosure(*, output, metadata, **kwargs):
            forbid = (metadata or {}).get('forbid')
            if not forbid:
                return None
            passed, comment = _run_in_thread(
                judge_private_disclosure, output.get('text', ''), forbid,
            )
            return Evaluation(name='no_private_disclosure', value=passed,
                              data_type='BOOLEAN', comment=comment)

        dataset = langfuse.get_dataset(dataset_name)
        result = dataset.run_experiment(
            name=opts['run_name'] or f"ig-support-agent @ {org.slug}",
            description=f"Instagram support agent vs org '{org.slug}'",
            task=task,
            evaluators=[ev_grounded, ev_escalation, ev_tool, ev_no_disclosure],
            max_concurrency=max(1, opts['concurrency']),
        )
        langfuse.flush()
        self._print_summary(result)

    def _sync_dataset(self, langfuse, dataset_name, cases):
        langfuse.create_dataset(
            name=dataset_name,
            description='Instagram DM support agent eval corpus (synced from cases.jsonl).',
        )
        for case in cases:
            message = case['input']
            item_id = hashlib.sha1(message.encode('utf-8')).hexdigest()[:16]
            langfuse.create_dataset_item(
                dataset_name=dataset_name,
                id=item_id,
                input=message,
                expected_output=case.get('expected_answer') or None,
                metadata={
                    'expected_category': case.get('expected_category', ''),
                    'expected_tool': case.get('expected_tool', ''),
                    'forbid': case.get('forbid', []),
                },
            )

    def _print_summary(self, result):
        totals = defaultdict(lambda: [0, 0])  # score name -> [passed, total]
        for item_result in result.item_results:
            for evaluation in item_result.evaluations or []:
                if isinstance(evaluation.value, bool):
                    totals[evaluation.name][0] += int(evaluation.value)
                    totals[evaluation.name][1] += 1

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Scores"))
        for name, (passed, total) in sorted(totals.items()):
            style = self.style.SUCCESS if passed == total else self.style.WARNING
            self.stdout.write(style(f"  {name}: {passed}/{total}"))

        url = getattr(result, 'dataset_run_url', '')
        if url:
            self.stdout.write(f"\nRun: {url}")
