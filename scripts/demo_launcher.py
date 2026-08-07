"""Start the deterministic local API and web demo on Windows."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
CREATE_NO_WINDOW = 0x08000000


def _wait(
    uri: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: int = 45,
) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Process exited before {uri} became ready")
        try:
            with urllib.request.urlopen(uri, timeout=2) as response:
                if response.status < 500:
                    return response.read()
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for {uri}")


def _start(command: list[str], *, stdout_name: str, stderr_name: str) -> subprocess.Popen[bytes]:
    stdout = (RUNTIME_DIR / stdout_name).open("wb")
    stderr = (RUNTIME_DIR / stderr_name).open("wb")
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        stdout=stdout,
        stderr=stderr,
        creationflags=CREATE_NO_WINDOW,
    )


def main() -> None:
    if not PYTHON.exists():
        raise SystemExit(
            "Missing .venv. Run: py -3 -m venv .venv; "
            ".venv\\Scripts\\pip install -e .[dev]"
        )
    if not (PROJECT_ROOT / "web" / "node_modules").exists():
        raise SystemExit("Missing web dependencies. Run: npm --prefix web install")

    RUNTIME_DIR.mkdir(exist_ok=True)
    os.environ.update(
        APP_ENV="demo",
        APP_MODE="demo",
        MODEL_PROVIDER="fake",
        STORAGE_BACKEND="memory",
        MCP_TRANSPORT="inprocess",
        EMBEDDING_PROVIDER="hashing",
        RERANK_PROVIDER="lexical",
    )

    api = _start(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "apps.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        stdout_name="api.out.log",
        stderr_name="api.error.log",
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    web = _start(
        [npm, "--prefix", "web", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"],
        stdout_name="web.out.log",
        stderr_name="web.error.log",
    )
    (RUNTIME_DIR / "api.pid").write_text(str(api.pid), encoding="utf-8")
    (RUNTIME_DIR / "web.pid").write_text(str(web.pid), encoding="utf-8")

    try:
        health_payload = json.loads(
            _wait("http://127.0.0.1:8000/health/ready", api).decode("utf-8")
        )
        if health_payload.get("model_provider") != "fake" or health_payload.get(
            "storage"
        ) != "memory":
            raise RuntimeError("Port 8000 is occupied by a non-demo NutriPilot runtime")
        _wait("http://127.0.0.1:5173", web)
    except Exception:
        api.terminate()
        web.terminate()
        raise

    print("NutriPilot demo is ready:")
    print("  Web:      http://127.0.0.1:5173")
    print("  API docs: http://127.0.0.1:8000/docs")
    print("  Mode:     deterministic FakeModel demo")


if __name__ == "__main__":
    main()
