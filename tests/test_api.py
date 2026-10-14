"""Real MongoDB contract tests. Set TEST_MONGO_URI; only a random test database is used."""
import json
import os
import random
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from bson import json_util
from fastapi.testclient import TestClient

os.environ["MONGO_URI"] = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27028")
run_id = UUID(os.environ["HRONE_TEST_RUN_ID"]).hex if "HRONE_TEST_RUN_ID" in os.environ else uuid4().hex
os.environ["MONGO_DB"] = "hrone_test_" + run_id
from app import main as api  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    api.client.admin.command("ping")
    api.ensure_indexes()
    yield
    assert api.db.name.startswith("hrone_test_")
    api.client.drop_database(api.db.name)


@pytest.fixture(autouse=True)
def clean(database):
    api.db.attendance_logs.delete_many({})
    api.db.employees.delete_many({})


@pytest.fixture
def http():
    return TestClient(api.app)


def ms(local):
    return int(datetime.fromisoformat(local).replace(tzinfo=api.IST).timestamp() * 1000)


def employee(code="EMP0001", department="Engineering", joined="2026-01-01", **kwargs):
    return {"emp_code": code, "name": code, "email": code + "@example.com", "department": department,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined, **kwargs}


def add_employee(**kwargs):
    doc = employee(**kwargs)
    api.db.employees.insert_one({**doc, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    return doc


def punch(http, timestamp="2026-07-06T09:30:00", **kwargs):
    return http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(timestamp), **kwargs})


def log(code, day, status="PRESENT", hours=8.0, late=0, overtime=0, half=False):
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": api.instant(ms(day + "T09:30:00")) if status in api.PRESENCE else None,
            "punch_out": api.instant(ms(day + "T18:30:00")) if hours is not None and status in api.PRESENCE else None,
            "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}


def test_employee_validation_pagination_and_serialization(http):
    for key, value in [("emp_code", "1"), ("name", ""), ("email", "bad"), ("department", ""),
                       ("shift_start", "24:00"), ("shift_end", "09:30"), ("joined_on", "2026-02-30"),
                       ("joined_on", "2026-7-1"), ("joined_on", "0000-01-01")]:
        assert http.post("/employees", json=employee(**{key: value})).status_code == 422
    for n in range(1, 5):
        response = http.post("/employees", json=employee(code=f"EMP{n:04d}", department="A" if n < 4 else "B"))
        assert response.status_code == 201, response.text
        assert type(response.json()["created_at"]) is int
        assert "_id" not in response.json()
    response = http.get("/employees", params={"department": "A", "page_size": 2}).json()
    assert response["total"] == 3
    assert [x["emp_code"] for x in response["items"]] == ["EMP0001", "EMP0002"]
    response = http.get("/employees", params={"department": "A", "page_size": 2, "page": 2}).json()
    assert [x["emp_code"] for x in response["items"]] == ["EMP0003"]
    assert http.get("/employees", params={"department": "a"}).json()["total"] == 0
    for path in ("/employees", "/attendance"):
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
            assert http.get(path, params=params).status_code == 422


@pytest.mark.parametrize("value", [1783312500, "1783312500000", 1783312500000.0, True, None, 4102444800001])
def test_strict_timestamps(http, value):
    add_employee()
    assert punch(http, punched_at=value).status_code == 422


@pytest.mark.parametrize("clock,expected", [("09:40:00", 0), ("09:40:00.999", 0), ("09:40:01", 10),
                                           ("10:15:59", 45), ("09:29:59", 0)])
def test_late_boundaries(http, clock, expected):
    add_employee()
    response = punch(http, "2026-07-06T" + clock)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["late_minutes"] == expected
    assert result["punch_in"] % 1000 == 0
    assert result["date"] == "2026-07-06"
    assert result["history"] == []
    assert "id" not in result and "_id" not in result


@pytest.mark.parametrize("out,hours,overtime,half", [
    ("13:59:41", 4.49, 0, True), ("13:59:42", 4.50, 0, False),
    ("18:59:59", 9.50, 0, False), ("19:00:00", 9.50, 30, False),
    ("19:00:59", 9.52, 30, False), ("09:32:06", 0.04, 0, True)])
def test_work_overtime_half_up(http, out, hours, overtime, half):
    add_employee()
    assert punch(http).status_code == 201
    response = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T" + out)})
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["work_hours"], data["overtime_minutes"], data["half_day"]) == (hours, overtime, half)
    assert data["history"] == []


def test_overnight_and_utc_boundary(http):
    add_employee(shift_start="22:00", shift_end="06:00")
    result = punch(http, "2026-07-07T01:00:00").json()
    assert result["date"] == "2026-07-06" and result["late_minutes"] == 180
    result = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T06:40:00")})
    assert result.status_code == 200, result.text
    assert result.json()["overtime_minutes"] == 40
    assert punch(http, "2026-07-08T06:00:00").json()["date"] == "2026-07-08"


def test_missing_conflicts_and_validation(http):
    assert punch(http).status_code == 404
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    add_employee()
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    assert punch(http, status="ABSENT").status_code == 422
    assert punch(http).status_code == 201
    assert punch(http).status_code == 409
    for stamp, expected in [("2026-07-06T09:29:59", 404), ("2026-07-06T09:30:00", 422),
                            ("2026-07-07T09:30:01", 422), ("2026-07-07T09:30:00", 200)]:
        assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(stamp)}).status_code == expected
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T09:30:00")}).status_code == 409


def test_corrections_and_nested_dates(http):
    add_employee()
    punch(http, "2026-07-06T10:05:00")
    path = "/attendance/EMP0001/2026-07-06"
    base = {"reason": "Device correction", "regularized_by": "hr.admin"}
    assert http.patch(path, json=base).status_code == 422
    assert http.patch(path, json={**base, "punch_in": None}).status_code == 422
    assert http.patch(path, json={**base, "status": "LEAVE", "punch_out": ms("2026-07-06T18:30:00")}).status_code == 422
    assert http.patch(path, json={**base, "punch_in": ms("2026-07-07T09:30:00")}).status_code == 422
    result = http.patch(path, json={**base, "punch_in": ms("2026-07-06T09:30:00"), "work_hours": 999}).json()
    assert result["work_hours"] is None
    assert set(result["history"][0]["changes"]) == {"punch_in", "late_minutes"}
    assert isinstance(result["history"][0]["changes"]["punch_in"]["from"], int)
    result = http.patch(path, json={**base, "status": "LEAVE"}).json()
    assert result["punch_in"] is None and result["punch_out"] is None
    assert len(result["history"]) == 2
    assert http.patch(path, json={**base, "status": "WFH"}).status_code == 422
    result = http.patch(path, json={**base, "status": "WFH", "punch_in": ms("2026-07-06T09:30:00"),
                                   "punch_out": ms("2026-07-06T13:00:00")}).json()
    assert result["half_day"] is True and result["work_hours"] == 3.5 and len(result["history"]) == 3
    stored = api.db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)


def test_concurrent_writes(http):
    def parallel(fn):
        with ThreadPoolExecutor(max_workers=12) as pool:
            return list(pool.map(fn, range(12)))
    codes = parallel(lambda _: http.post("/employees", json=employee()).status_code)
    assert codes.count(201) == 1 and codes.count(409) == 11
    codes = parallel(lambda _: punch(http).status_code)
    assert codes.count(201) == 1 and codes.count(409) == 11
    codes = parallel(lambda _: http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T18:30:00")}).status_code)
    assert codes.count(200) == 1 and codes.count(409) == 11
    results = parallel(lambda n: http.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": f"Correction {n}", "regularized_by": f"hr{n}", "punch_out": ms("2026-07-06T19:00:00") + n * 60000}))
    assert all(r.status_code in (200, 409) for r in results)
    history = api.db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]
    assert len(history) == sum(r.status_code == 200 for r in results)
    previous = api.instant(ms("2026-07-06T18:30:00"))
    for entry in history:
        assert entry["changes"]["punch_out"]["from"] == previous
        previous = entry["changes"]["punch_out"]["to"]


def test_sample_legacy_and_filters(http):
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((Path(__file__).parents[1] / "sample_data" / (name + ".json")).read_text())
        api.db[name].insert_many(docs)
    result = http.get("/attendance", params={"emp_code": "EMP0002"}).json()
    assert result["total"] == 1
    assert result["items"][0]["history"] == [] and result["items"][0]["half_day"] is False
    result = http.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 2}).json()
    assert result["total"] == 6
    assert [(d["date"], d["emp_code"]) for d in result["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0004")]
    for params in ({"date_from": "2026-07-08", "date_to": "2026-07-06"}, {"date_from": "2026-02-30"}, {"status": "WRONG"}):
        assert http.get("/attendance", params=params).status_code == 422
    result = http.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert result["working_days"] == 15 and result["present_days"] == 1 and result["attendance_pct"] == 6.67


def test_ties_headcount_weighted_average_and_empty_trends(http):
    for n in range(1, 5):
        add_employee(code=f"EMP{n:04d}")
    add_employee(code="EMP0005", department="Future", joined="2026-08-01")
    add_employee(code="EMP0006", department="Empty")
    docs = [log("EMP0001", "2026-07-06", hours=10, late=50),
            log("EMP0001", "2026-07-07", hours=10),
            log("EMP0002", "2026-07-06", hours=4, late=30, half=True),
            log("EMP0003", "2026-07-06", hours=None, late=30),
            log("EMP0003", "2026-07-11", hours=8, overtime=40),
            log("EMP9999", "2026-07-06", late=900)]
    api.db.attendance_logs.insert_many(docs)
    result = http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [x["rank"] for x in result] == [1, 2, 2]
    assert [x["emp_code"] for x in result] == ["EMP0001", "EMP0002", "EMP0003"]
    result = http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [x["department"] for x in result] == ["Empty", "Engineering"]
    assert result[0]["headcount"] == 1 and result[0]["avg_work_hours"] is None
    assert result[1]["headcount"] == 4 and result[1]["avg_work_hours"] == 8.0
    assert result[1]["present_days"] == 3.5
    for dep in ("Empty", "Future"):
        response = http.get(f"/analytics/departments/{dep}/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert len(rows) == 7
        assert [r["attendance_rate"] for r in rows] == ([0.0] * 5 + [None] * 2 if dep == "Empty" else [None] * 7)
    rows = http.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-13"}).json()["items"]
    assert rows[0]["present_count"] == 2.5 and rows[0]["attendance_rate"] == 0.625
    assert rows[1]["moving_avg_7d"] == 0.4375
    assert rows[5]["present_count"] == 1 and rows[5]["attendance_rate"] is None
    assert rows[7]["moving_avg_7d"] == 0.05  # July 6 is outside this window.
    result = http.get("/analytics/employees/EMP0005/monthly", params={"month": "2026-07"}).json()
    assert result["working_days"] == 0 and result["attendance_pct"] is None


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}), ("attendance_list", {"status": "PRESENT", "page": 2}),
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"})])
def test_explain(http, endpoint, params):
    add_employee()
    api.db.attendance_logs.insert_one(log("EMP0001", "2026-07-06", late=30))
    response = http.get("/admin/explain/" + endpoint, params=params)
    assert response.status_code == 200, response.text
    text = json.dumps(response.json()["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text
    def check_scans(value):
        if isinstance(value, dict):
            if "collectionScans" in value:
                assert value["collectionScans"] == 0
            for child in value.values():
                check_scans(child)
        elif isinstance(value, list):
            for child in value:
                check_scans(child)
    check_scans(response.json())


def test_analytics_errors(http):
    assert http.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).status_code == 404
    assert http.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    for start, end in [("2026-07-02", "2026-07-01"), ("2026-01-01", "2026-04-04")]:
        assert http.get("/analytics/departments/Unknown/trend", params={"from": start, "to": end}).status_code == 422
    for month in ("2026-13", "2026-1", "0000-01", "2026-01-01"):
        assert http.get("/analytics/departments/summary", params={"month": month}).status_code == 422
    for name in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend", "bogus"):
        assert http.get("/admin/explain/" + name).status_code == 422


def test_full_92_day_trend_and_range_boundary(http):
    add_employee(joined="2026-07-13")
    api.db.attendance_logs.insert_many([
        log("EMP0001", "2026-07-13", half=True, hours=3.5),
        log("EMP0001", "2026-09-30", hours=8),
        log("EMP0001", "2026-10-01", hours=8),  # Outside the requested period.
    ])
    path = "/analytics/departments/Engineering/trend"
    params = {"from": "2026-07-01", "to": "2026-09-30"}
    response = http.get(path, params=params)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 92
    assert [r["date"] for r in rows] == [
        (date(2026, 7, 1) + timedelta(days=n)).isoformat() for n in range(92)]
    assert all(r["headcount"] == 0 and r["attendance_rate"] is None for r in rows[:12])
    assert rows[12]["attendance_rate"] == 0.5
    assert rows[12]["moving_avg_7d"] == 0.5  # Earlier null days do not dilute the mean.
    assert rows[-1]["attendance_rate"] == 1.0
    assert rows[-1]["moving_avg_7d"] == 0.2
    assert http.get(path, params={**params, "to": "2026-10-01"}).status_code == 422
    explain = http.get("/admin/explain/department_trend", params={**params, "department": "Engineering"})
    assert explain.status_code == 200, explain.text
    assert "COLLSCAN" not in json.dumps(explain.json())
    # An empty period must still include both endpoints of the range.
    empty = http.get(path, params={"from": "2025-10-01", "to": "2025-12-31"}).json()["items"]
    assert len(empty) == 92
    assert all(row["moving_avg_7d"] is None and row["present_count"] == 0 for row in empty)


def test_response_models_preserve_contract(http):
    from fastapi.exceptions import ResponseValidationError
    from pydantic import ValidationError

    # Generated documentation must describe every successful response.
    schema = http.get("/openapi.json").json()
    import yaml
    contract = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text())
    for name in ("Employee", "EmployeePage", "HistoryEntry", "AttendanceRecord", "AttendancePage",
                 "EmployeeMonthly", "DepartmentSummary", "Leaderboard", "Trend"):
        expected = contract["components"]["schemas"][name]
        actual = schema["components"]["schemas"][name]
        assert set(actual["properties"]) == set(expected["properties"])
        assert set(actual["required"]) == set(expected["required"])
    routes = [(route.path, route) for route in api.app.routes if getattr(route, "response_model", None)]
    assert len(routes) == 12
    for path, route in routes:
        method = next(iter(route.methods)).lower()
        code = str(route.status_code or 200)
        assert "$ref" in schema["paths"][path][method]["responses"][code]["content"]["application/json"]["schema"]
    assert "from" in schema["components"]["schemas"]["HistoryChange"]["properties"]
    assert "before" not in schema["components"]["schemas"]["HistoryChange"]["properties"]

    add_employee()
    # Missing nullable fields must fail validation rather than silently disappear.
    with pytest.raises(ValidationError):
        api.AttendanceRecord.model_validate({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"})
    # A wrong stored metric is detected on the actual HTTP response path.
    api.db.attendance_logs.insert_one({**log("EMP0001", "2026-07-06"), "late_minutes": "30"})
    with pytest.raises(ResponseValidationError):
        http.get("/attendance")


def test_response_history_types_and_seeded_equal_shift(http):
    # The input-only unequal-shift validator must not reject a valid seeded record.
    add_employee(shift_start="09:30", shift_end="09:30")
    assert http.get("/employees").status_code == 200
    assert punch(http).status_code == 201
    response = http.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_out": ms("2026-07-06T13:00:00"), "reason": "Missing device punch", "regularized_by": "hr"})
    assert response.status_code == 200, response.text
    changes = response.json()["history"][0]["changes"]
    assert type(changes["half_day"]["from"]) is bool
    assert type(changes["half_day"]["to"]) is bool
    assert type(changes["punch_out"]["to"]) is int
    assert changes["punch_out"]["from"] is None
    assert type(changes["work_hours"]["to"]) is float


def test_health_database_failure(http, monkeypatch):
    from pymongo.errors import ConnectionFailure
    def fail(*args, **kwargs):
        raise ConnectionFailure("test outage")
    monkeypatch.setattr(api.client.admin.__class__, "command", fail)
    assert http.get("/health").status_code == 503


def test_real_server_startup(tmp_path):
    """Exercise the required launch command and startup index creation over HTTP."""
    import httpx
    add_employee()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "server.log").open("w+") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=Path(__file__).parents[1], stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 19
            with httpx.Client(trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/health")
                        if response.status_code == 200:
                            assert client.get(f"http://127.0.0.1:{port}/employees").json()["total"] == 1
                            return
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None:
                        break
                    time.sleep(.1)
            output.seek(0)
            pytest.fail("Server did not become ready within 20 seconds: " + output.read())
        finally:
            process.terminate()
            process.wait(timeout=10)


def half_up(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def test_randomized_analytics_against_independent_reference(http):
    rng = random.Random(420)
    employees, records = [], []
    for n in range(1, 31):
        emp = add_employee(code=f"EMP{n:04d}", department="ABC"[n % 3], joined=rng.choice(["2026-01-01", "2026-07-13", "2026-08-01"]))
        employees.append(emp)
        if n % 7 == 0:
            continue
        for day in range(1, 32):
            stamp = f"2026-07-{day:02d}"
            if stamp < emp["joined_on"] or rng.random() < .35:
                continue
            status = rng.choice(list(api.PRESENCE) + ["ABSENT", "LEAVE"])
            hours = rng.choice([None, 3.5, 8.12, 8.13, 9.0]) if status in api.PRESENCE else None
            records.append(log(emp["emp_code"], stamp, status, hours, rng.choice([0, 10, 35]) if status in api.PRESENCE else 0,
                               rng.choice([0, 30, 60]) if hours else 0, hours is not None and hours < 4.5))
    api.db.attendance_logs.insert_many(records)
    for emp in employees:
        own = [r for r in records if r["emp_code"] == emp["emp_code"]]
        days = sum(date(2026, 7, d).weekday() < 5 and f"2026-07-{d:02d}" >= emp["joined_on"] for d in range(1, 32))
        present = sum((.5 if r["half_day"] else 1) for r in own if r["status"] in api.PRESENCE and date.fromisoformat(r["date"]).weekday() < 5)
        expected = {"emp_code": emp["emp_code"], "month": "2026-07", "working_days": days, "present_days": present,
                    "leave_days": sum(r["status"] == "LEAVE" for r in own), "late_count": sum(r["late_minutes"] > 0 for r in own),
                    "total_late_minutes": sum(r["late_minutes"] for r in own), "total_overtime_minutes": sum(r["overtime_minutes"] for r in own),
                    "attendance_pct": half_up(Decimal(str(present)) / days * 100) if days else None}
        assert http.get(f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": "2026-07"}).json() == expected
    for dep in "ABC":
        people = [e for e in employees if e["department"] == dep]
        eligible = [e for e in people if e["joined_on"] <= "2026-07-31"]
        own = [r for r in records if r["emp_code"] in {e["emp_code"] for e in eligible}]
        hours = [Decimal(str(r["work_hours"])) for r in own if r["status"] in api.PRESENCE and r["work_hours"] is not None]
        summary = http.get("/analytics/departments/summary", params={"month": "2026-07", "department": dep}).json()["items"][0]
        assert summary["headcount"] == len(eligible)
        assert summary["avg_work_hours"] == (half_up(sum(hours) / len(hours)) if hours else None)
        rows = http.get(f"/analytics/departments/{dep}/trend", params={"from": "2026-07-01", "to": "2026-07-31"}).json()["items"]
        rates = []
        for row in rows:
            day = row["date"]
            headcount = sum(e["joined_on"] <= day for e in people)
            daily = [r for r in own if r["date"] == day]
            present = sum(.5 if r["half_day"] else 1 for r in daily if r["status"] in api.PRESENCE)
            rate = half_up(Decimal(str(present)) / headcount, 4) if headcount and date.fromisoformat(day).weekday() < 5 else None
            rates.append(rate)
            window = [Decimal(str(x)) for x in rates[-7:] if x is not None]
            assert row["headcount"] == headcount and row["present_count"] == present
            assert row["attendance_rate"] == rate
            assert row["moving_avg_7d"] == (half_up(sum(window) / len(window), 4) if window else None)


@pytest.mark.skipif(os.getenv("RUN_SCALE_TEST") != "1", reason="Set RUN_SCALE_TEST=1 for 100k-record verification")
def test_100k_indexed_queries(http):
    employees = [{**employee(code=f"EMP{n:04d}", department=f"D{n % 10}"), "created_at": api.now()} for n in range(1000)]
    api.db.employees.insert_many(employees)
    start = date(2026, 5, 1)
    for offset in range(100):
        day = (start + timedelta(days=offset)).isoformat()
        api.db.attendance_logs.insert_many([log(e["emp_code"], day, late=30 if i % 3 == 0 else 0) for i, e in enumerate(employees)])
    assert api.db.attendance_logs.count_documents({}) == 100000
    cases = [("attendance_list", {}, "/attendance"),
             ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "/analytics/employees/EMP0001/monthly"),
             ("department_summary", {"month": "2026-07"}, "/analytics/departments/summary"),
             ("late_leaderboard", {"month": "2026-07"}, "/analytics/leaderboard/late"),
             ("department_trend", {"department": "D1", "from": "2026-05-01", "to": "2026-07-31"}, "/analytics/departments/D1/trend")]
    for endpoint, params, url in cases:
        begin = time.perf_counter()
        response = http.get(url, params=params)
        elapsed = time.perf_counter() - begin
        assert response.status_code == 200, response.text
        if endpoint == "department_trend":
            assert len(response.json()["items"]) == 92
        explain = http.get("/admin/explain/" + endpoint, params=params)
        assert explain.status_code == 200, explain.text
        raw = json.dumps(explain.json())
        assert "IXSCAN" in raw and "COLLSCAN" not in raw
        print(f"{endpoint}: {elapsed:.3f}s on 100000 records")
        assert elapsed < 10
