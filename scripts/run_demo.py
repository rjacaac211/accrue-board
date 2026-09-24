"""Start a fresh demo stack with real models, optionally pre-processed and screen-captured.

Steps:
1. Recreate the demo database and seed the demo clients (history plus local embeddings).
2. Start the API (serving the built frontend, demo controls on) and a worker that calls the
   configured models (LLM_MODE from .env, recorded under data/recordings).
3. Optionally feed the first documents and wait until the worker and the review assistant are
   done with them, so the demo starts from a board with history on it.
4. Either keep running until Ctrl+C (open the printed URL), or record the scripted walkthrough
   (frontend/e2e/demo.capture.ts) as a silent video under data/demo/.

Needs `docker compose up -d db`, ANTHROPIC_API_KEY in .env (or recordings), `pnpm build` in
frontend/, and for --capture the Playwright browser. From the repository root:

    python scripts/run_demo.py [--prefeed 10] [--capture]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_e2e import BACKEND, FRONTEND, RESET, ROOT, resolve, run, start, stop, wait_for

DEMO_DB = "postgresql+psycopg://accrue:accrue@localhost:5433/accrueboard_demo"
CLIENT = "fernhill"


def api(base: str, path: str, body: dict[str, object] | None = None) -> object:
    request = urllib.request.Request(  # noqa: S310 - localhost
        f"{base}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read())


def settled(base: str) -> bool:
    """No document waiting for the worker, and every held one already investigated."""
    cards = api(base, f"/api/clients/{CLIENT}/board")
    if not isinstance(cards, list):
        raise TypeError("unexpected board response")
    if any(c["state"] in ("queued", "processing") for c in cards):
        return False
    for card in cards:
        if card["state"] == "needs_review":
            detail = api(base, f"/api/tasks/{card['task_id']}")
            if not isinstance(detail, dict) or detail["assistant"] is None:
                return False
    return True


def prefeed(base: str, count: int) -> None:
    fed = 0
    while fed < count:
        batch = api(base, f"/api/demo/clients/{CLIENT}/feed", {"count": min(5, count - fed)})
        if not isinstance(batch, dict) or not batch["task_ids"]:
            raise RuntimeError("nothing left to feed")
        fed += len(batch["task_ids"])
        print(f"fed {fed}/{count}", flush=True)
    deadline = time.monotonic() + 20 * 60
    while not settled(base):
        if time.monotonic() > deadline:
            raise TimeoutError("the worker did not finish the pre-fed documents in 20 minutes")
        time.sleep(3)
    print("pre-fed documents processed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--prefeed", type=int, default=10, help="documents to process first")
    parser.add_argument("--capture", action="store_true", help="record the walkthrough video")
    args = parser.parse_args()

    logs = ROOT / "data" / "demo"
    logs.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DATABASE_URL": DEMO_DB,
        "DEMO_MODE": "true",
        "REVIEW_ASSISTANT": "true",
        "FRONTEND_DIST": str(FRONTEND / "dist"),
    }
    run(["uv", "run", "python", "-c", RESET], BACKEND, env)
    run(["uv", "run", "accrueboard", "bootstrap"], BACKEND, env)  # every client, if missing

    uvicorn = ["uv", "run", "uvicorn", "accrueboard.api.app:create_app", "--factory"]
    server = start([*uvicorn, "--port", str(args.port)], BACKEND, env, logs / "api.log")
    worker = start(
        ["uv", "run", "accrueboard", "worker", "--poll", "0.5"], BACKEND, env, logs / "worker.log"
    )
    base = f"http://localhost:{args.port}"
    try:
        wait_for(f"{base}/api/health", 120)
        if args.prefeed:
            prefeed(base, args.prefeed)
        if not args.capture:
            print(f"demo running at {base} (Ctrl+C to stop)", flush=True)
            while True:
                time.sleep(3600)
        result = subprocess.run(  # noqa: S603 - fixed command
            resolve(
                ["pnpm", "exec", "playwright", "test", "--config", "playwright.capture.config.ts"]
            ),
            cwd=FRONTEND,
            env={**env, "E2E_BASE_URL": base},
            check=False,
        )
        videos = sorted((FRONTEND / "e2e-results").rglob("*.webm"))
        if videos:
            target = logs / "walkthrough.webm"
            shutil.copyfile(videos[-1], target)
            print(f"video: {target}", flush=True)
        return result.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        stop(worker)
        stop(server)


if __name__ == "__main__":
    raise SystemExit(main())
