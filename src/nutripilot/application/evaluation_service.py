"""Stores reproducible evaluation reports for API retrieval."""

from __future__ import annotations

import asyncio

from nutripilot.evaluation.models import EvalCase, EvalReport
from nutripilot.evaluation.runner import EvalRunner
from nutripilot.infrastructure.run_store import SQLiteRunStore


class EvaluationNotFoundError(LookupError):
    pass


class EvaluationBusyError(RuntimeError):
    pass


class EvaluationService:
    def __init__(self, runner: EvalRunner, store: SQLiteRunStore | None = None) -> None:
        self.runner = runner
        self.store = store
        self._reports: dict[str, EvalReport] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self, cases: list[EvalCase], *, dataset_version: str) -> EvalReport:
        report = await self.runner.run(cases, dataset_version=dataset_version)
        self._save(report)
        return report

    def start(self, cases: list[EvalCase], *, dataset_version: str, user_id: int = 1) -> EvalReport:
        if self._tasks:
            raise EvaluationBusyError("An evaluation is already running.")
        queued = EvalReport(
            status="running",
            dataset_version=dataset_version,
            total_cases=len(cases),
            passed_cases=0,
            owner_user_id=user_id,
        )
        self._save(queued)
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
            self._save(
                completed.model_copy(
                    update={
                        "run_id": run_id,
                        "owner_user_id": self._reports[run_id].owner_user_id,
                    }
                )
            )
        except asyncio.CancelledError:
            current = self._reports[run_id]
            self._save(current.model_copy(update={"status": "interrupted"}))
            raise
        except Exception:
            current = self._reports[run_id]
            self._save(
                current.model_copy(update={"status": "failed", "metrics": {"execution_error": 1.0}})
            )

    def _save(self, report: EvalReport) -> None:
        if self.store:
            self.store.save_report(report)
            if report.status != "running":
                self._reports.pop(report.run_id, None)
                return
        self._reports[report.run_id] = report

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def get(self, run_id: str, *, user_id: int | None = None) -> EvalReport:
        try:
            report = self._reports.get(run_id)
            if report is None and self.store:
                report = self.store.get_report(run_id)
            if report is None or (user_id is not None and report.owner_user_id != user_id):
                raise KeyError(run_id)
            return report
        except KeyError as exc:
            raise EvaluationNotFoundError(run_id) from exc
