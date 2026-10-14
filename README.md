# Employee Attendance and Analytics API

An attendance backend for employee registration, punch-in/out, manual corrections, and attendance reports. Built with FastAPI and MongoDB against the supplied v2.0 API contract.

Requires Python 3.11+ and MongoDB 6.0+. All application code is in `app/main.py`. Run the commands below from the directory containing `app/` and `requirements.txt`.

## Run

From this directory, create and activate a virtual environment, then install dependencies:

```text
python -m venv .venv
```

Activate with `.venv\Scripts\Activate.ps1` on PowerShell or `source .venv/bin/activate` on Linux/macOS.

```text
python -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

`requirements.txt` applies `constraints.txt`, which records the exact runtime and test dependency versions used for verification. Keep both files in the repository. The snapshot was resolved on Python 3.12/Windows; platform-specific dependencies absent from that environment may still be resolved separately on other systems.

Set `MONGO_URI` and `MONGO_DB` in the environment, or copy `.env.example` to `.env` and edit it. Real environment variables take precedence. Defaults are `mongodb://localhost:27017` and `attendance_db`. MongoDB must already be running; an optional local instance is:

```text
docker run -d --name hrone-mongo -p 127.0.0.1:27017:27017 mongo:7.0
```

`GET /health` pings MongoDB and returns 503 when unavailable. Indexes are created automatically and idempotently at startup. API documentation is at `/docs`; the supplied `openapi.yaml` remains the detailed business contract.

`python sample_seed.py` is optional and **deletes existing documents in both configured collections** before loading six employees and nine attendance records. Do not run it against data you want to retain. Startup never seeds or clears data.

## Tests

```text
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Set `TEST_MONGO_URI` to your MongoDB server before running tests. The default test port is **27028**, separate from the application's default **27017**. To use the local server shown above, set it to `mongodb://127.0.0.1:27017`:

```powershell
# PowerShell
$env:TEST_MONGO_URI = "mongodb://127.0.0.1:27017"
```

```bash
# Linux/macOS
export TEST_MONGO_URI=mongodb://127.0.0.1:27017
```

Each test run creates a random `hrone_test_...` database and removes only that database afterwards. The account needs permission to create indexes and drop that test database. Existing application databases are not used.

The suite covers validation, timestamp/rounding boundaries, overnight shifts, legacy defaults, corrections, concurrent writes, randomized analytics compared with an independent reference, index plans including lookup collection scans, and a real server startup within 20 seconds. It also checks response fields against the supplied contract, nested history types, the inclusive 92-day trend boundary, and rejection of a 93-day range.

Verified on 2026-10-14 with Python 3.12: **39 functional tests passed on MongoDB 7 and on MongoDB 6.0.28**. The 100,000-record scale test passed separately on both versions. The test client emits a deprecation warning for its HTTP transport; it does not affect these results.

For the optional 100,000-record performance check, run `python tests/run_scale.py`. This runner enforces a hard 120-second process deadline, bounds individual database operations, and cleans up its own database even if the child is stopped. It does not configure Docker limits automatically. Alternatively, set `RUN_SCALE_TEST=1` and run the full suite with `python -m pytest tests -q -s` (without the runner's process deadline).

Each scale check ran with MongoDB limited to **1 GiB RAM and 2 CPU cores**, without additional swap allowance. The trend query covers the full **92-day range**. All five explain checks contained `IXSCAN` and no `COLLSCAN`.

| Query | MongoDB 7 | MongoDB 6.0.28 |
|---|---:|---:|
| Attendance list | 0.075 s | 0.069 s |
| Employee monthly summary | 0.015 s | 0.012 s |
| Department summary | 0.783 s | 0.789 s |
| Late leaderboard | 0.307 s | 0.181 s |
| Department trend (92 days) | 0.470 s | 0.434 s |

The bounded runs, including data creation and cleanup, took 12.50 seconds on MongoDB 7 and 12.45 seconds on MongoDB 6. Timings depend on hardware and database state.

## Implementation notes

- Instants use aware UTC BSON dates and integer epoch milliseconds at the API boundary. Shifts and attendance dates use IST.
- Unique indexes resolve duplicate creates/punch-ins; snapshot-conditional writes prevent stale punch-outs and lost correction history.
- Analytics use stored derived metrics and MongoDB pipelines. Trend gap filling generates a bounded calendar in the database, including completely empty periods. Ranking and moving averages use window stages.
- The trend pipeline reads department membership once, reuses it for daily attendance filtering, and calculates headcount from each employee's joining date. This avoids an employee lookup for every attendance record.
- `GET /admin/explain/{endpoint}` shares the same query builders and hints as the corresponding public endpoint; it returns actual execution statistics.
- Response models validate successful responses and describe their fields in `/docs`, including nullable values and nested correction history.
- Unknown resources return 404, conflicts 409, invalid input 422. MongoDB failures return 503 without exposing connection details.

`REVIEW.md` documents the starter defects and fixes. `DECISIONS.md` explains the index, concurrency, ranking, and headcount choices. For submission, place this directory's contents at the repository root, including `constraints.txt`, and exclude `.env`, virtual environments, data dumps, and local tooling directories.
