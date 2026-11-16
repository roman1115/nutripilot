import io
from pathlib import Path

import pytest
from PIL import Image

from nutripilot.domain.diet import DietError
from nutripilot.infrastructure.diet_media import PrivateFiles, sanitize_image


def image_bytes(format: str = "PNG", **options) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(output, format=format, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    "mime,format", [("image/png", "PNG"), ("image/jpeg", "JPEG"), ("image/webp", "WEBP")]
)
def test_clean_image_is_single_frame_with_no_original_metadata(mime: str, format: str) -> None:
    exif = Image.Exif()
    exif[270] = "private label"
    exif[274] = 6
    clean, output_mime, width, height, digest = sanitize_image(image_bytes(format, exif=exif), mime)
    assert output_mime == "image/png" and len(digest) == 64
    assert (width, height) == (12, 16)
    with Image.open(io.BytesIO(clean)) as image:
        assert not image.getexif() and image.n_frames == 1


@pytest.mark.parametrize(
    "data,mime",
    [(b"broken", "image/png"), (image_bytes(), "image/jpeg"), (b"<svg/>", "image/svg+xml")],
)
def test_corruption_disguise_and_unsupported_type_rejected(data: bytes, mime: str) -> None:
    with pytest.raises(DietError):
        sanitize_image(data, mime)


def test_animated_png_rejected() -> None:
    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        output,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(DietError, match="动画"):
        sanitize_image(output.getvalue(), "image/png")


def test_private_files_reject_symlink_and_traversal(tmp_path: Path) -> None:
    files = PrivateFiles(tmp_path / "private")
    key = files.write(b"fixture", "png")
    assert files.read(key) == b"fixture"
    with pytest.raises(DietError):
        files.read("../outside.png")
    (tmp_path / "private" / ("a" * 32 + ".png")).symlink_to(tmp_path / "outside")
    with pytest.raises(DietError):
        files.read("a" * 32 + ".png")
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "private", target_is_directory=True)
    with pytest.raises(DietError):
        PrivateFiles(linked)
    files.delete(key)
    with pytest.raises(DietError):
        files.read(key)


def test_pixel_and_file_limits_and_existing_directory_permissions(tmp_path: Path) -> None:
    output = io.BytesIO()
    Image.new("1", (5000, 4001)).save(output, format="PNG")
    with pytest.raises(DietError) as exc:
        sanitize_image(output.getvalue(), "image/png")
    assert exc.value.code == "MEDIA_TOO_MANY_PIXELS"
    with pytest.raises(DietError) as exc:
        sanitize_image(b"x" * (10 * 1024 * 1024 + 1), "image/png")
    assert exc.value.code == "MEDIA_TOO_LARGE"
    root = tmp_path / "existing"
    root.mkdir(mode=0o755)
    files = PrivateFiles(root)
    assert root.stat().st_mode & 0o777 == 0o700
    key = files.write(b"private", "png")
    assert (root / key).stat().st_mode & 0o777 == 0o600
