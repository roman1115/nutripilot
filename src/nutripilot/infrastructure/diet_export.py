"""Bounded ZIP construction spools JSON and CSV to disk rather than retaining a snapshot."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack
from typing import BinaryIO

from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_records import (
    DietLog,
    DietPlan,
    NutritionTarget,
    PersonalFood,
    PlannedMeal,
)

EXPORT_LIMIT = 200 * 1024 * 1024
type ExportResource = DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood


def csv_bytes(rows: list[dict[str, object]], *, header: bool = True) -> bytes:
    output = io.StringIO(newline="")
    if not rows:
        return b""
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    if header:
        writer.writeheader()
    for row in rows:
        values = {}
        for key, value in row.items():
            text = "" if value is None else str(value)
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(
                ("\t", "\r", "\n")
            ):
                text = "'" + text
            values[key] = text
        writer.writerow(values)
    encoded = output.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if header else encoded


def _rows(resource: ExportResource) -> Iterator[tuple[str, dict[str, object]]]:
    if isinstance(resource, DietLog):
        yield (
            "records.csv",
            {
                "id": resource.id,
                "version": resource.version,
                **resource.temporal.model_dump(),
                "timezone": resource.timezone,
                "deleted_at": resource.deleted_at,
            },
        )
        for item in resource.items:
            yield (
                "items.csv",
                {
                    "record_id": resource.id,
                    "item_id": item.id,
                    "name": item.name_snapshot,
                    "quantity": item.quantity.model_dump_json(),
                    "nutrition": item.nutrition_snapshot.model_dump_json(),
                    "note": item.note,
                },
            )
    elif isinstance(resource, DietPlan):
        yield (
            "plans.csv",
            {
                "plan_id": resource.id,
                "title": resource.title,
                "start_date": resource.start_date,
                "version": resource.version,
                "deleted_at": resource.deleted_at,
            },
        )
    elif isinstance(resource, PlannedMeal):
        for item in resource.items:
            yield (
                "planned_items.csv",
                {
                    "plan_id": resource.plan_id,
                    "meal_id": resource.id,
                    "local_date": resource.local_date,
                    "meal_type": resource.meal_type,
                    "item_id": item.id,
                    "name": item.name_snapshot,
                    "quantity": item.quantity.model_dump_json(),
                    "nutrition": item.nutrition_snapshot.model_dump_json(),
                },
            )
    elif isinstance(resource, NutritionTarget):
        yield (
            "targets.csv",
            {
                "id": resource.id,
                "version": resource.version,
                "effective_date": resource.effective_date,
                "goals": resource.goals.model_dump_json(),
                "source": resource.source,
            },
        )
    else:
        yield (
            "personal_foods.csv",
            {
                "id": resource.id,
                "version": resource.version,
                "name": resource.name,
                "brand": resource.brand,
                "source": resource.evidence.model_dump_json(),
                "deleted_at": resource.deleted_at,
            },
        )


class DietArchive:
    def __init__(self, output: BinaryIO, metadata: dict[str, object]) -> None:
        self.output, self.metadata = output, metadata
        self.stack = ExitStack()
        self.files: dict[str, BinaryIO] = {
            name: self.stack.enter_context(
                tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - ExitStack owns lifetime.
            )
            for name in (
                "sources.json",
                "records.csv",
                "items.csv",
                "targets.csv",
                "plans.csv",
                "planned_items.csv",
                "personal_foods.csv",
            )
        }
        self.archive = zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED)
        self.total = 0
        self.record_count = 0
        self.kind: str | None = None
        self.first = True
        self.checksums: dict[str, dict[str, object]] = {}
        try:
            self._emit("sources.json", b"{")
            for key, value in metadata.items():
                self._emit(
                    "sources.json", (json.dumps(key) + ":" + json.dumps(value) + ",").encode()
                )
        except BaseException:
            self.__exit__()
            raise

    def __enter__(self) -> DietArchive:
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.archive.close()
        finally:
            self.stack.close()

    def _count(self, size: int) -> None:
        self.total += size
        if self.total > EXPORT_LIMIT or self.output.tell() > EXPORT_LIMIT:
            raise DietError("EXPORT_TOO_LARGE", "导出超过200MiB，请拆分日期范围或不包含媒体。")

    def _emit(self, name: str, content: bytes) -> None:
        self._count(len(content))
        self.files[name].write(content)

    def batch(self, kind: str, resources: list[ExportResource]) -> None:
        if kind != self.kind:
            if self.kind is not None:
                self._emit("sources.json", b"],")
            self._emit("sources.json", (json.dumps(kind) + ":[").encode())
            self.kind, self.first = kind, True
        for resource in resources:
            if isinstance(resource, DietLog):
                self.record_count += 1
                if self.record_count > 10000:
                    raise DietError("EXPORT_TOO_LARGE", "每次最多10000条记录，请拆分日期范围。")
            self._emit(
                "sources.json", (b"" if self.first else b",") + resource.model_dump_json().encode()
            )
            self.first = False
            for name, row in _rows(resource):
                self._emit(name, csv_bytes([row], header=self.files[name].tell() == 0))

    def media(self, name: str, content: bytes) -> None:
        self._count(len(content))
        self.archive.writestr(name, content)
        self.checksums[name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }

    def finish(self) -> tuple[str, int]:
        self._emit("sources.json", b"]}" if self.kind else b"}")
        for name, source in self.files.items():
            source.seek(0)
            digest, size = hashlib.sha256(), 0
            with self.archive.open(name, "w") as target:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
            self.checksums[name] = {"sha256": digest.hexdigest(), "bytes": size}
        manifest = json.dumps(
            {
                "schema_version": "diet-export-v1",
                **self.metadata,
                "record_count": self.record_count,
                "complete": True,
                "targets_scope": "complete timeline",
                "personal_foods_scope": "all owned foods",
                "files": self.checksums,
            },
            ensure_ascii=False,
        ).encode()
        self._count(len(manifest))
        self.archive.writestr("manifest.json", manifest)
        self.archive.close()
        self._count(0)
        size, digest = self.output.tell(), hashlib.sha256()
        self.output.seek(0)
        while chunk := self.output.read(1024 * 1024):
            digest.update(chunk)
        self.output.seek(0, io.SEEK_END)
        return digest.hexdigest(), size
