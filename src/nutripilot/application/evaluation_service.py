"""Stores reproducible evaluation reports for API retrieval."""

from __future__ import annotations

import asyncio

from nutripilot.evaluation.models import EvalCase, EvalReport
from nutripilot.evaluation.runner import EvalRunner


class EvaluationNotFoundError(LookupError):
    pass


class EvaluationService:
    def __init__(self, runner: EvalRunner) -> None:
        self.runner = runner
        self._reports: dict[str, EvalReport] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self, cases: list[EvalCase], *, dataset_version: str) -> EvalReport:
        report = await self.runner.run(cases, dataset_version=dataset_version)
        self._reports[report.run_id] = report
        return report

    def start(self, cases: list[EvalCase], *, dataset_version: str) -> EvalReport:
        queued = EvalReport(
            status="running",
            dataset_version=dataset_version,
            total_cases=len(cases),
            passed_cases=0,
        )
        self._reports[queued.run_id] = queued
        task = asyncio.create_task(
            self._run_background(queued.run_id, cases, dataset_version=dataset_version)
        )
        self._tasks[queued.run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(queued.run_id, None))
        return queued

    async def _run_background(
        self,
        run_id: str,
        cases: list[EvalCase],
        *,
        dataset_version: str,
    ) -> None:
        try:
            completed = await self.runner.run(cases, dataset_version=dataset_version)
            self._reports[run_id] = completed.model_copy(update={"run_id": run_id})
        except Exception:
            current = self._reports[run_id]
            self._reports[run_id] = current.model_copy(
                update={"status": "failed", "metrics": {"execution_error": 1.0}}
            )

    def get(self, run_id: str) -> EvalReport:
        try:
            return self._reports[run_id]
        except KeyError as exc:
            raise EvaluationNotFoundError(run_id) from exc
