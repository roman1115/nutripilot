"""Start the local demo or real Qwen/BGE stack on Windows, macOS, and Linux."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
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
    with (
        (RUNTIME_DIR / stdout_name).open("wb") as stdout,
        (RUNTIME_DIR / stderr_name).open("wb") as stderr,
    ):
        return subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            stdout=stdout,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return number


def _check_port(port: int) -> None:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SystemExit(
                f"Port {port} is unavailable. Choose --api-port and --web-port explicitly."
            ) from exc


def _configure_real(args: argparse.Namespace) -> None:
    from nutripilot.config import Settings

    settings = Settings(model_provider="openai")
    if importlib.util.find_spec("sentence_transformers") is None:
        raise SystemExit("Missing ML dependencies. Run: uv sync --frozen --extra dev --extra ml")
    embedding = Path(args.embedding_model).resolve()
    reranker = Path(args.rerank_model).resolve()
    for model in (embedding, reranker):
        weights_present = any(
            (model / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")
        )
        if not (model / "config.json").is_file() or not weights_present:
            raise SystemExit(f"Missing local BGE model files in {model}")
    _check_port(args.mcp_port)
    if args.mcp_port in {args.api_port, args.web_port}:
        raise SystemExit("API, web and MCP ports must be different")
    os.environ.update(
        APP_MODE="docker",
        MODEL_PROVIDER="openai",
        MODEL_API_KEY=settings.model_api_key or "",
        MODEL_NAME=settings.model_name,
        MODEL_BASE_URL=settings.model_base_url,
        MODEL_FALLBACK_NAME=settings.model_fallback_name or "",
        EMBEDDING_PROVIDER="bge",
        EMBEDDING_MODEL=str(embedding),
        EMBEDDING_DEVICE=settings.embedding_device or "cpu",
        RERANK_PROVIDER="bge",
        RERANK_MODEL=str(reranker),
        RERANK_DEVICE=settings.rerank_device or "cpu",
        DATABASE_URL=os.getenv("DATABASE_URL", settings.database_url),
        REDIS_URL=os.getenv("REDIS_URL", settings.redis_url),
        QDRANT_URL=os.getenv("QDRANT_URL", settings.qdrant_url),
        QDRANT_COLLECTION="meal_search_bge_m3",
        MCP_ENABLED="true",
        MCP_SERVER_URL=f"http://127.0.0.1:{args.mcp_port}/mcp",
        MCP_INTERNAL_TOKEN=secrets.token_urlsafe(32),
        MCP_TIMEOUT_SECONDS="120",
        NUTRIPILOT_MCP_HOST="127.0.0.1",
        NUTRIPILOT_MCP_PORT=str(args.mcp_port),
        HF_HUB_OFFLINE="1",
        OMP_NUM_THREADS=os.getenv("OMP_NUM_THREADS", "4"),
        TOKENIZERS_PARALLELISM="false",
    )
    # The compose service names only resolve inside Docker; local Python uses loopback.
    for variable, service in (
        ("DATABASE_URL", "postgres"),
        ("REDIS_URL", "redis"),
        ("QDRANT_URL", "qdrant"),
    ):
        os.environ[variable] = (
            os.environ[variable]
            .replace(f"@{service}:", "@127.0.0.1:")
            .replace(f"://{service}:", "://127.0.0.1:")
        )
    if not args.no_infrastructure:
        subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", "postgres", "redis", "qdrant"],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            check=True,
            timeout=120,
        )
    subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        check=True,
        timeout=60,
    )


async def _warmup_mcp(process: subprocess.Popen[bytes], port: int) -> None:
    from nutripilot.tools.mcp.transport import StreamableHTTPMCPTransport

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("MCP exited during startup; see real-mcp.error.log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            await asyncio.sleep(0.5)
    transport = StreamableHTTPMCPTransport(
        os.environ["MCP_SERVER_URL"],
        token=os.environ["MCP_INTERNAL_TOKEN"],
        timeout_seconds=120,
    )
    result = await transport.call_tool("search_meals", {"query": "午餐", "limit": 1})
    if not isinstance(result, dict) or not result.get("hits"):
        raise RuntimeError("BGE warmup did not return meal candidates")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=_port, default=8000)
    parser.add_argument("--web-port", type=_port, default=5173)
    parser.add_argument("--foreground", action="store_true", default=os.name != "nt")
    parser.add_argument("--background", action="store_false", dest="foreground")
    parser.add_argument("--mode", choices=["demo", "real"], default="demo")
    parser.add_argument("--mcp-port", type=_port, default=8010)
    parser.add_argument(
        "--embedding-model",
        default=str(RUNTIME_DIR / "models" / "bge-m3"),
    )
    parser.add_argument(
        "--rerank-model",
        default=str(RUNTIME_DIR / "models-parallel" / "bge-reranker-v2-m3"),
    )
    parser.add_argument("--no-infrastructure", action="store_true")
    args = parser.parse_args()
    if args.api_port == args.web_port:
        parser.error("API and web ports must be different")
    if not PYTHON.exists():
        raise SystemExit(
            "Missing .venv. Install Python 3.12 dependencies first: "
            "uv sync --python 3.12 --extra dev --frozen"
        )
    if not (PROJECT_ROOT / "web" / "node_modules").exists():
        raise SystemExit("Missing web dependencies. Run: npm --prefix web install")

    _check_port(args.api_port)
    _check_port(args.web_port)
    api_url = f"http://127.0.0.1:{args.api_port}"
    web_url = f"http://127.0.0.1:{args.web_port}"
    RUNTIME_DIR.mkdir(exist_ok=True)
    os.environ.update(
        APP_ENV="demo",
        APP_MODE="demo",
        MODEL_PROVIDER="fake",
        EMBEDDING_PROVIDER="hashing",
        RERANK_PROVIDER="lexical",
        MCP_ENABLED="true",
        WEB_ORIGIN=f"{web_url},http://localhost:{args.web_port}",
        VITE_API_BASE_URL=api_url,
    )
    from nutripilot.config import Settings

    configured_run_path = Settings().run_store_path
    os.environ.setdefault(
        "RUN_STORE_PATH",
        configured_run_path or str(RUNTIME_DIR / f"{args.mode}-observability.sqlite3"),
    )
    if args.mode == "real":
        # Remove demo overrides before reading the user's real model configuration.
        for key in ("EMBEDDING_PROVIDER", "RERANK_PROVIDER"):
            os.environ.pop(key, None)
        _configure_real(args)

    processes: dict[str, subprocess.Popen[bytes]] = {}
    leave_running = False
    prefix = f"{args.mode}-{args.api_port}-" if args.mode == "real" or args.api_port != 8000 else ""
    try:
        if args.mode == "real":
            processes["mcp"] = _start(
                [str(PYTHON), "-m", "apps.nutrition_mcp"],
                stdout_name=f"{prefix}mcp.out.log",
                stderr_name=f"{prefix}mcp.error.log",
            )
            print("Warming up BGE embedding and reranker...", flush=True)
            asyncio.run(_warmup_mcp(processes["mcp"], args.mcp_port))
        processes["api"] = _start(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
            ],
            stdout_name=f"{prefix}api.out.log",
            stderr_name=f"{prefix}api.error.log",
        )
        npm = "npm.cmd" if os.name == "nt" else "npm"
        processes["web"] = _start(
            [
                npm,
                "--prefix",
                "web",
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.web_port),
                "--strictPort",
            ],
            stdout_name=f"{prefix}web.out.log",
            stderr_name=f"{prefix}web.error.log",
        )
        for name, process in processes.items():
            (RUNTIME_DIR / f"{prefix}{name}.pid").write_text(str(process.pid), encoding="utf-8")
        health_payload = json.loads(
            _wait(f"{api_url}/health/ready", processes["api"]).decode("utf-8")
        )
        expected_model = "openai" if args.mode == "real" else "fake"
        expected_storage = "postgresql" if args.mode == "real" else "memory"
        if health_payload.get("model_provider") != expected_model or (
            health_payload.get("storage") != expected_storage
        ):
            raise RuntimeError("The API did not start in the expected runtime mode")
        _wait(web_url, processes["web"])

        print("NutriPilot is ready:", flush=True)
        print(f"  Web:      {web_url}", flush=True)
        print(f"  API docs: {api_url}/docs", flush=True)
        print(f"  Mode:     {args.mode}", flush=True)
        print(f"  Logs:     {RUNTIME_DIR}", flush=True)
        if args.foreground:
            print("Press Ctrl+C to stop both services.", flush=True)
            while True:
                for name, process in processes.items():
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited; see logs in {RUNTIME_DIR}")
                time.sleep(0.5)
        else:
            leave_running = True
    except KeyboardInterrupt:
        print("\nStopping NutriPilot services.", flush=True)
    finally:
        if not leave_running:
            for name, process in reversed(list(processes.items())):
                _stop(process)
                (RUNTIME_DIR / f"{prefix}{name}.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
