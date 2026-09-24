"""Run the browser smoke test against a real stack, without an API key.

Steps:
1. Recreate a throwaway database and seed the demo clients (hashing embeddings).
2. Start the API (serving the built frontend) and a worker, both in offline mode
   (LLM_MODE=oracle: the model is replaced by the synthetic dataset's ground truth).
3. Run Playwright (frontend/e2e), then stop everything.

Needs: `docker compose up -d db`, a generated dataset (`accrueboard datagen`; created here if
missing), `pnpm build` in frontend/, and the Playwright browser (`pnpm exec playwright install
chromium`). Usage, from the repository root:

    python scripts/run_e2e.py [--port 8010] [--video]
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
DEFAULT_DB = "postgresql+psycopg://accrue:accrue@localhost:5433/accrueboard_e2e_ui"
RESET = (
    "import os\n"
    "from accrueboard.db.scratch import fresh_database\n"
    "with fresh_database(os.environ['DATABASE_URL']):\n"
    "    pass\n"
)


def resolve(cmd: list[str]) -> list[str]:
    """The full path of the executable (pnpm is a .cmd script on Windows), no shell needed."""
    found = shutil.which(cmd[0])
    if found is None:
        raise FileNotFoundError(f"{cmd[0]} is not on PATH")
    return [found, *cmd[1:]]


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(cmd).splitlines()[0], flush=True)
    subprocess.run(resolve(cmd), cwd=cwd, env=env, check=True)  # noqa: S603 - fixed commands


def start(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    print("+", " ".join(cmd), f"> {log.name}", flush=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(  # noqa: S603 - fixed commands
        resolve(cmd),
        cwd=cwd,
        env=env,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        kill = resolve(["taskkill", "/T", "/F", "/PID", str(process.pid)])
        subprocess.run(kill, capture_output=True, check=False)  # noqa: S603
    else:
        os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=30)


def wait_for(url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - localhost
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"{url} did not come up within {seconds:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--video", action="store_true", help="record a video of the run")
    args = parser.parse_args()

    logs = ROOT / "frontend" / "e2e-results"
    logs.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DATABASE_URL": os.environ.get("E2E_DATABASE_URL", DEFAULT_DB),
        "LLM_MODE": "oracle",
        "EMBEDDER": "hashing",
        "DEMO_MODE": "true",
        "REVIEW_ASSISTANT": "false",
        "FRONTEND_DIST": str(FRONTEND / "dist"),
    }
    run(["uv", "run", "python", "-c", RESET], BACKEND, env)
    run(["uv", "run", "accrueboard", "bootstrap"], BACKEND, env)  # every client, if missing

    uvicorn = ["uv", "run", "uvicorn", "accrueboard.api.app:create_app", "--factory"]
    api = start([*uvicorn, "--port", str(args.port)], BACKEND, env, logs / "api.log")
    worker = start(
        ["uv", "run", "accrueboard", "worker", "--poll", "0.5"], BACKEND, env, logs / "worker.log"
    )
    try:
        base = f"http://localhost:{args.port}"
        wait_for(f"{base}/api/health", 90)
        test_env = {**env, "E2E_BASE_URL": base, **({"E2E_VIDEO": "1"} if args.video else {})}
        result = subprocess.run(  # noqa: S603 - fixed command
            resolve(["pnpm", "exec", "playwright", "test"]),
            cwd=FRONTEND,
            env=test_env,
            check=False,
        )
        return result.returncode
    finally:
        stop(worker)
        stop(api)


if __name__ == "__main__":
    raise SystemExit(main())
