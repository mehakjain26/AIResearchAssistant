"""Browser tests: the real React build and the real FastAPI app, served together on one local port.

The backend runs in a thread of the test process, so the per-test fakes from ../conftest.py
(moto S3, in-memory DB, fake OpenAI/arXiv/downloads) apply to requests made by the browser.
"""
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from starlette.staticfiles import StaticFiles

from app.main import app

FRONTEND = Path(__file__).resolve().parents[4] / "frontend"


@pytest.fixture(scope="session")
def frontend_dist(tmp_path_factory) -> Path:
    if not shutil.which("npm"):
        pytest.skip("npm is not installed; cannot build the frontend")
    if not (FRONTEND / "node_modules").is_dir():
        subprocess.run(["npm", "ci"], cwd=FRONTEND, check=True, capture_output=True)
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(["npm", "run", "build", "--", "--outDir", str(out), "--emptyOutDir"],
                   cwd=FRONTEND, check=True, capture_output=True)
    return out


def _spa(dist: Path):
    """Routes /api to the backend and everything else to the built frontend (index.html for client routes)."""
    static = StaticFiles(directory=dist, html=True)

    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan" or scope["path"].startswith("/api"):
            return await app(scope, receive, send)
        if not (dist / scope["path"].lstrip("/")).is_file():
            scope = {**scope, "path": "/"}
        return await static(scope, receive, send)

    return asgi


@pytest.fixture(scope="session")
def live_server(frontend_dist):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_spa(frontend_dist), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def base_url(live_server):
    """pytest-playwright resolves relative page.goto() URLs against this."""
    return live_server


@pytest.fixture(autouse=True)
def _timeouts(page):
    page.set_default_timeout(10_000)
    yield
