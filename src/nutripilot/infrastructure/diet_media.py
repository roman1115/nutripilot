"""Bounded Pillow decoding and private atomic file storage; originals are never retained."""

from __future__ import annotations

import hashlib
import heapq
import io
import os
import re
import stat
import warnings
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from nutripilot.domain.diet import DietError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def file_sha256(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


class PrivateFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        # Reject links in every parent, not just the final directory.
        for part in (self.root, *self.root.parents):
            if part.is_symlink():
                raise DietError("UNSAFE_MEDIA_STORAGE", "Private storage cannot use symlinks.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise DietError("UNSAFE_MEDIA_STORAGE", "Private storage is unavailable.")
        descriptor = self._directory()
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)

    def _directory(self) -> int:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                next_descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise DietError("UNSAFE_MEDIA_STORAGE", "Private storage is unavailable.") from exc

    @staticmethod
    def validate_key(key: str) -> None:
        if re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|zip)", key) is None:
            raise DietError("INVALID_MEDIA_KEY", "Invalid private artifact reference.")

    def write(self, content: bytes, extension: str) -> str:
        with self.stage(extension) as (key, output):
            output.write(content)
        return key

    @contextmanager
    def stage(self, extension: str) -> Iterator[tuple[str, BinaryIO]]:
        key = f"{uuid4().hex}.{extension}"
        self.validate_key(key)
        temporary = f"{uuid4().hex}.tmp"
        directory = self._directory()
        try:
            descriptor = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "w+b") as output:
                    yield key, output
                    output.flush()
                    os.fsync(output.fileno())
                os.rename(temporary, key, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)

    def read(self, key: str, *, limit: int = MAX_IMAGE_BYTES) -> bytes:
        with self.open_read(key, limit=limit) as source:
            return source.read()

    def open_read(self, key: str, *, limit: int = MAX_IMAGE_BYTES) -> BinaryIO:
        self.validate_key(key)
        directory = self._directory()
        try:
            descriptor = os.open(key, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            source = os.fdopen(descriptor, "rb")
            try:
                status = os.fstat(source.fileno())
                if not stat.S_ISREG(status.st_mode):
                    raise DietError("INVALID_MEDIA_FILE", "Private artifact is unavailable.")
                if status.st_size > limit:
                    raise DietError("MEDIA_TOO_LARGE", "Private artifact exceeds its size limit.")
                return source
            except BaseException:
                source.close()
                raise
        except (OSError, ValueError) as exc:
            raise DietError("MEDIA_UNAVAILABLE", "Private artifact is unavailable.") from exc
        finally:
            os.close(directory)

    def delete(self, key: str) -> None:
        self.validate_key(key)
        directory = self._directory()
        try:
            with suppress(FileNotFoundError):
                os.unlink(key, dir_fd=directory)
        finally:
            os.close(directory)

    def old_files(self, cutoff: float, after: str = "", limit: int = 100) -> list[str]:
        def eligible(entries: Iterator[os.DirEntry[str]]) -> Iterator[str]:
            for entry in entries:
                if entry.name <= after or not re.fullmatch(
                    r"[a-f0-9]{32}\.(png|jpg|zip|tmp)", entry.name
                ):
                    continue
                with suppress(FileNotFoundError):
                    status = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(status.st_mode) and status.st_mtime <= cutoff:
                        yield entry.name

        directory = self._directory()
        try:
            with os.scandir(directory) as entries:
                return heapq.nsmallest(limit, eligible(entries))
        finally:
            os.close(directory)

    def delete_old(self, key: str, cutoff: float) -> None:
        if re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|zip|tmp)", key) is None:
            raise DietError("INVALID_MEDIA_KEY", "Invalid private artifact reference.")
        directory = self._directory()
        try:
            with suppress(FileNotFoundError):
                status = os.stat(key, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(status.st_mode) and status.st_mtime <= cutoff:
                    os.unlink(key, dir_fd=directory)
                    os.fsync(directory)
        finally:
            os.close(directory)


def sanitize_image(content: bytes, mime: str) -> tuple[bytes, str, int, int, str]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise DietError("MEDIA_TOO_LARGE", "每张图片不能超过10MiB。")
    expected = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(mime)
    if expected is None:
        raise DietError("UNSUPPORTED_MEDIA_TYPE", "仅支持JPEG、PNG、WebP。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                if source.format != expected:
                    raise DietError("INVALID_MEDIA_FORMAT", "图片内容与声明格式不一致。")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise DietError("MEDIA_TOO_MANY_PIXELS", "图片不能超过2000万像素。")
                if getattr(source, "n_frames", 1) != 1 or getattr(source, "is_animated", False):
                    raise DietError("ANIMATED_MEDIA_REJECTED", "不接受动画或多帧图片。")
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                mode = "RGBA" if "A" in oriented.getbands() else "RGB"
                # A new image prevents ancillary metadata from surviving save().
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = io.BytesIO()
                clean.save(output, format="PNG")
                encoded = output.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise DietError("MEDIA_TOO_LARGE", "清洗后的图片超过10MiB，请缩小后重试。")
                return (
                    encoded,
                    "image/png",
                    clean.width,
                    clean.height,
                    hashlib.sha256(content).hexdigest(),
                )
    except DietError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise DietError("INVALID_MEDIA_FORMAT", "图片损坏或无法安全解码。") from exc
