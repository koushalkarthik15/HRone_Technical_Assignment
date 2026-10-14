"""Run only the scale test with a hard two-minute deadline and isolated cleanup."""
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from pymongo import MongoClient


def main():
    run_id = uuid4().hex
    uri = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27028")
    # Driver timeouts bound individual database operations as well as the parent
    # process deadline. The test container's CPU/RAM limits are configured outside.
    uri += ("&" if "?" in uri else "?") + "socketTimeoutMS=12000&timeoutMS=15000"
    env = {**os.environ, "RUN_SCALE_TEST": "1", "HRONE_TEST_RUN_ID": run_id,
           "TEST_MONGO_URI": uri, "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests/test_api.py", "-q", "-s", "-k", "100k", "--tb=short"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        try:
            output, _ = process.communicate(timeout=120)
            print(output, end="", flush=True)
            result = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=10)
            print(output, end="", flush=True)
            print("Scale test stopped at the 120-second safety deadline.", flush=True)
            result = 124
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # The run ID is generated here, so cleanup can only target our own DB,
        # including when the child was stopped before pytest fixture teardown.
        with MongoClient(uri, serverSelectionTimeoutMS=3000) as client:
            client.drop_database("hrone_test_" + run_id)
            print("Isolated test database removed.", flush=True)
        print(f"Total elapsed: {time.monotonic() - started:.2f}s", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
