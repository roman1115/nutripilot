"""Command line entry point for reproducible offline evaluation reports."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from nutripilot.application import build_container
from nutripilot.evaluation.models import EvalReport
from nutripilot.evaluation.runner import EvalRunner, load_dataset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the NutriPilot evaluation dataset.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evals/datasets/core.json"),
        help="Path to a versioned JSON evaluation dataset.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evals/reports"),
        help="Directory for JSON and Markdown reports.",
    )
    parser.add_argument(
        "--label",
        default="fake-model-baseline",
        help="Stable report label. The run id is retained inside the report.",
    )
    return parser


async def _evaluate(dataset: Path) -> EvalReport:
    dataset_version, cases = load_dataset(dataset)
    return await EvalRunner(build_container().chat_service).run(
        cases,
        dataset_version=dataset_version,
    )


def _write_report(report: EvalReport, output_dir: Path, label: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{label}.json"
    markdown_path = output_dir / f"{label}.md"
    json_path.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    failed = [result for result in report.results if not result.passed]
    lines = [
        f"# NutriPilot Evaluation: {label}",
        "",
        "> Runtime: deterministic FakeModelGateway; this is an engineering regression baseline, "
        "not a real-model quality claim.",
        "",
        f"- Dataset: `{report.dataset_version}`",
        f"- Run ID: `{report.run_id}`",
        f"- Cases: `{report.passed_cases}/{report.total_cases}` passed",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {value:.4f} |" for name, value in report.metrics.items())
    lines.extend(["", "## Failed Cases", ""])
    if failed:
        lines.extend(f"- `{item.case_id}`: {', '.join(item.failure_reasons)}" for item in failed)
    else:
        lines.append("No failures in this deterministic regression run.")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(_evaluate(args.dataset))
    json_path, markdown_path = _write_report(report, args.output_dir, args.label)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")


if __name__ == "__main__":
    main()
