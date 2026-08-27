from __future__ import annotations

import json
from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.reports.quality import aggregate_range, default_day


class Command(BaseCommand):
    help = "Build privacy-safe daily correction, disagreement, and duplicate trends."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--date", type=date.fromisoformat, help="One day to rebuild.")
        parser.add_argument("--from", dest="start", type=date.fromisoformat)
        parser.add_argument("--to", dest="end", type=date.fromisoformat)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["date"] and (options["start"] or options["end"]):
            raise CommandError("Use --date or --from/--to, not both.")
        if options["date"]:
            start = end = options["date"]
        else:
            start = options["start"] or options["end"] or default_day()
            end = options["end"] or start
        aggregate_range(start, end)

        from apps.reports.models import QualityMetricDaily

        rows = list(
            QualityMetricDaily.objects.filter(day__gte=start, day__lte=end).order_by(
                "day", "institution", "source_type", "engine"
            )
        )
        payload = [
            {
                "day": row.day.isoformat(),
                "institution": row.institution,
                "source_type": row.source_type,
                "engine": row.engine,
                "observations": row.observations_count,
                "corrected": row.corrected_count,
                "disagreements": row.disagreement_count,
                "duplicate_candidates": row.duplicate_candidates_count,
                "duplicate_confirmed": row.duplicate_confirmed_count,
                "ocr_issues": row.ocr_issue_count,
                "parser_issues": row.parser_issue_count,
                "correction_rate": str(row.correction_rate),
                "disagreement_rate": str(row.disagreement_rate),
                "duplicate_rate": str(row.duplicate_rate),
                "ocr_issue_rate": str(row.ocr_issue_rate),
                "parser_issue_rate": str(row.parser_issue_rate),
                "mean_confidence": str(row.mean_confidence),
            }
            for row in rows
        ]
        if options["as_json"]:
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return
        self.stdout.write(f"Built {len(payload)} quality dimension(s) for {start} through {end}.")
        for row in payload:
            self.stdout.write(
                f"{row['day']} {row['institution']}/{row['source_type']}/{row['engine']}: "
                f"{row['observations']} observation(s), "
                f"correction rate {row['correction_rate']}, "
                f"disagreement rate {row['disagreement_rate']}, "
                f"duplicate rate {row['duplicate_rate']}"
            )
