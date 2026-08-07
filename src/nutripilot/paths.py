from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """Locate runtime assets in both source checkouts and installed Docker wheels."""

    module_path = Path(__file__).resolve()
    for candidate in (Path.cwd(), *module_path.parents):
        if (candidate / "data" / "meals.json").is_file():
            return candidate
    raise RuntimeError("Could not locate the NutriPilot project data directory")
