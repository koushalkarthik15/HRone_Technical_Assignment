"""Employee Attendance & Analytics API. Run from the kit root with uvicorn app.main:app."""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock
from typing import Annotated, Any, Literal

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
index_lock = Lock()
indexes_ready = False


def ensure_indexes():
    # Repeated startup/readiness calls reuse the same indexes. The lock prevents
    # simultaneous health probes from starting duplicate initialization work.
    global indexes_ready
    with index_lock:
        if indexes_ready:
            return
        db.employees.create_index("emp_code", unique=True, name="employee_code")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
        db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)],
                                        unique=True, name="employee_date")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
        db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)],
                                        name="status_date_employee")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")
        indexes_ready = True


@asynccontextmanager
async def lifespan(app):
    # Keep the readiness route reachable even when MongoDB is temporarily down.
    try:
        ensure_indexes()
    except PyMongoError:
        logging.getLogger(__name__).warning("MongoDB indexes are not ready", exc_info=True)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request: Request, exc: PyMongoError):
    logging.getLogger(__name__).error("MongoDB operation failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def invalid(message: str, field: str = "body"):
    raise HTTPException(422, [{"type": "value_error", "loc": [field], "msg": message}])


def valid_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Expected YYYY-MM")
    date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, AfterValidator(valid_date)]
MonthString = Annotated[str, AfterValidator(valid_month)]
ShiftString = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftString = "09:30"
    shift_end: ShiftString = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    # A missing value means now; explicit JSON null is not a valid instant.
    punched_at: EpochMillis = Field(default_factory=lambda: epoch_ms(now()))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_null(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null when supplied")
        return self


# Response models describe the existing contract and catch serialization mistakes.
# Keep output fields separate from creation validators: seeded employees need not
# satisfy creation-only rules such as unequal shift times.
class ResponseModel(BaseModel):
    model_config = ConfigDict(strict=True)


class Health(ResponseModel):
    status: Literal["ok"]


class Employee(ResponseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: DateString
    created_at: EpochMillis


class PageResponse(ResponseModel):
    total: int
    page: int
    page_size: int


class EmployeePage(PageResponse):
    items: list[Employee]


class HistoryChange(ResponseModel):
    before: str | int | float | bool | None = Field(alias="from")
    after: str | int | float | bool | None = Field(alias="to")


class HistoryEntry(ResponseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, HistoryChange]


class AttendanceRecord(ResponseModel):
    emp_code: str
    date: DateString
    status: Status
    punch_in: EpochMillis | None
    punch_out: EpochMillis | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(PageResponse):
    items: list[AttendanceRecord]


class EmployeeMonthly(ResponseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentItem(ResponseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(ResponseModel):
    month: str
    items: list[DepartmentItem]


class LeaderboardItem(ResponseModel):
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(ResponseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(ResponseModel):
    date: DateString
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class Trend(ResponseModel):
    department: str
    items: list[TrendItem]


class ExplainResult(ResponseModel):
    endpoint: str
    collection: str
    # Explain output varies with MongoDB version and must remain unmodified.
    explain: dict[str, Any]


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def instant(value: int) -> datetime:
    # R1 discards milliseconds before storing a punch or applying any thresholds.
    return EPOCH + timedelta(seconds=value // 1000)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch_ms(value: datetime) -> int:
    delta = aware(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def json_value(value: Any) -> Any:
    """Also converts timestamps nested in history and BSON values in explain output."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


RECORD_DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
MUTABLE = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def public(doc: dict, attendance: bool = False) -> dict:
    result = {**RECORD_DEFAULTS, **doc} if attendance else dict(doc)
    result.pop("_id", None)
    return json_value(result)


def employee(emp_code: str) -> dict:
    result = db.employees.find_one({"emp_code": emp_code})
    if result is None:
        raise HTTPException(404, "Unknown employee")
    return result


def attendance_day(punch_in: datetime, emp: dict) -> str:
    # An early-morning punch can belong to yesterday's overnight shift.
    # At exactly shift_end it belongs to the current day, hence the strict <.
    local = aware(punch_in).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(emp: dict, day: str) -> tuple[datetime, datetime]:
    start = datetime.combine(date.fromisoformat(day), time.fromisoformat(emp["shift_start"]), IST)
    end = datetime.combine(date.fromisoformat(day), time.fromisoformat(emp["shift_end"]), IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    # Compare seconds first: 10 minutes and 1 second is late by 10 whole minutes.
    seconds = int((aware(punch_in) - shift_start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    minutes = int((aware(punch_out) - shift_end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derive(doc: dict, emp: dict) -> dict:
    if doc["status"] not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    if pin is None:
        invalid("Presence requires punch_in")
    if attendance_day(pin, emp) != doc["date"]:
        invalid("punch_in must remain on the attendance date")
    start, end = shift_bounds(emp, doc["date"])
    result = {"late_minutes": compute_late_minutes(pin, start), "work_hours": None,
              "overtime_minutes": 0, "half_day": False}
    if pout is not None:
        duration = (aware(pout) - aware(pin)).total_seconds()
        if not 0 < duration <= 86400:
            invalid("punch_out must be after punch_in and within 24 hours")
        hours = compute_work_hours(pin, pout)
        # Half-day status uses the rounded hours, including the 4.495 -> 4.50 edge.
        result.update(work_hours=hours, overtime_minutes=compute_overtime(pout, end), half_day=hours < 4.5)
    return result


def snapshot_filter(doc: dict) -> dict:
    # Match exactly what was read, including missing fields in legacy records.
    # This prevents stale calculations and lost audit entries without transactions.
    return {"_id": doc["_id"], "$and": [
        {field: {"$eq": doc[field], "$exists": True}} if field in doc
        else {field: {"$exists": False}} for field in (*MUTABLE, "history")
    ]}


@app.get("/health", response_model=Health)
def health():
    client.admin.command("ping")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return public(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    items = db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public(doc) for doc in items], "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = {**RECORD_DEFAULTS, "emp_code": body.emp_code, "date": attendance_day(ts, emp),
           "status": body.status, "punch_in": ts, "history": []}
    doc.update(derive(doc, emp))
    # Let the unique index choose the winner of concurrent requests.
    # A preliminary existence check would leave a race before the insert.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already recorded for this attendance date") from None
    return public(doc, True)


@app.post("/attendance/punch-out", response_model=AttendanceRecord)
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    # Find the latest qualifying punch, even if it is already closed. Filtering
    # only open records could incorrectly close an older attendance record.
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": ts}},
        sort=[("punch_in", -1)])
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    updated = {**doc, "punch_out": ts}
    updates = {"punch_out": ts, **derive(updated, emp)}
    result = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": updates},
                                                  return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Attendance changed concurrently; retry")
    return public(result, True)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = employee(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "No attendance for this date")
    supplied = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    if supplied.get("status") in ("ABSENT", "LEAVE") and {"punch_in", "punch_out"} & supplied.keys():
        invalid("Do not supply punch times with ABSENT or LEAVE")
    for field in ("punch_in", "punch_out"):
        if field in supplied:
            supplied[field] = instant(supplied[field])
    old = {**RECORD_DEFAULTS, **doc}
    updated = {**old, **supplied}
    # Derive from the final state so a changed punch also updates hours and flags.
    updated.update(derive(updated, emp))
    changes = {field: {"from": old.get(field), "to": updated.get(field)}
               for field in MUTABLE if old.get(field) != updated.get(field)}
    if not changes:
        invalid("Correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Apply the correction and append its audit entry in one atomic document write.
    result = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {
        "$set": {field: updated.get(field) for field in MUTABLE}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Attendance changed concurrently; retry")
    return public(result, True)


# Query plans are shared verbatim by the public endpoints and /admin/explain.
@dataclass
class Plan:
    collection: str
    hint: str
    pipeline: list | None = None
    query: dict | None = None
    sort: dict | None = None
    skip: int = 0
    limit: int = 0

    def command(self) -> dict:
        if self.pipeline is not None:
            return {"aggregate": self.collection, "pipeline": self.pipeline,
                    "cursor": {}, "hint": self.hint, "allowDiskUse": True}
        return {"find": self.collection, "filter": self.query, "sort": self.sort,
                "skip": self.skip, "limit": self.limit, "hint": self.hint}

    def run(self):
        if self.pipeline is not None:
            return list(db[self.collection].aggregate(self.pipeline, hint=self.hint, allowDiskUse=True))
        return list(db[self.collection].find(self.query).sort(list(self.sort.items()))
                    .skip(self.skip).limit(self.limit).hint(self.hint))


def attendance_plan(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not exceed date_to", "query")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {**({"$gte": date_from} if date_from else {}),
                         **({"$lte": date_to} if date_to else {})}
    hint = "employee_date" if emp_code is not None else (
        "status_date_employee" if status is not None else "date_employee")
    return Plan("attendance_logs", hint, query=query, sort={"date": -1, "emp_code": 1},
                skip=(page - 1) * page_size, limit=page_size)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    plan = attendance_plan(emp_code, date_from, date_to, status, page, page_size)
    return {"items": [public(doc, True) for doc in plan.run()],
            "total": db.attendance_logs.count_documents(plan.query, hint=plan.hint),
            "page": page, "page_size": page_size}


def month_bounds(month: str) -> tuple[str, str]:
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def mongo_date(expr):
    return {"$dateFromString": {"dateString": expr, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def weekday(expr):
    return {"$and": [{"$gte": [{"$isoDayOfWeek": {"date": expr, "timezone": "+05:30"}}, 1]},
                     {"$lte": [{"$isoDayOfWeek": {"date": expr, "timezone": "+05:30"}}, 5]}]}


def rounded(expr, places=2):
    """Half-up for nonnegative metrics; MongoDB's $round uses half-even."""
    factor = 10 ** places
    return {"$cond": [{"$eq": [expr, None]}, None, {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, Decimal128("0.5")]}}, factor]}}]}


def present_weight():
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def stats_group():
    # Monthly presence excludes weekends; late/overtime totals include them.
    # Keep hours as a sum and count so department averages weight records equally.
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [weekday(mongo_date("$date")), present_weight(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                                           {"$toDecimal": {"$ifNull": ["$work_hours", 0]}}, 0]}},
        "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                                          {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, 1, 0]}}
    }}


def log_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                         "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, stats_group()],
                         "as": "stats"}}


def workday_count(first, last):
    # Start at the later of joining day and month start. A future join date
    # produces an empty range; the final calendar day is included with +1.
    return {"$let": {"vars": {"start": mongo_date({"$max": ["$joined_on", first]}),
                               "end": mongo_date(last)}, "in": {"$size": {"$filter": {
        "input": {"$range": [0, {"$max": [0, {"$add": [1, {"$dateDiff": {
            "startDate": "$$start", "endDate": "$$end", "unit": "day", "timezone": "+05:30"}}]}]}]},
        "as": "offset", "cond": weekday({"$dateAdd": {"startDate": "$$start", "unit": "day", "amount": "$$offset"}})
    }}}}}


def monthly_plan(emp_code, month):
    first, last = month_bounds(month)
    fields = ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    pipeline = [{"$match": {"emp_code": emp_code}}, log_lookup(first, last),
                {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
                {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month},
                               "working_days": workday_count(first, last),
                               **{field: {"$ifNull": ["$stats." + field, 0]} for field in fields}}},
                {"$set": {"attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                    rounded({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]}), None]}}}]
    return Plan("employees", "employee_code", pipeline=pipeline)


def summary_plan(month, department=None):
    # Start from employees, not logs, so people with no attendance still count.
    first, last = month_bounds(month)
    query = {"joined_on": {"$lte": last}}
    if department is not None:
        query["department"] = department
    fields = ("present_days", "late_count", "total_late_minutes", "on_duty_count", "hours_sum", "hours_count")
    pipeline = [{"$match": query}, log_lookup(first, last),
                {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
                {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                             "leave_count": {"$sum": {"$ifNull": ["$stats.leave_days", 0]}},
                             **{field: {"$sum": {"$ifNull": ["$stats." + field, 0]}} for field in fields}}},
                {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "leave_count": 1,
                               **{field: 1 for field in fields if not field.startswith("hours_")},
                               "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                                    rounded({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
                {"$sort": {"department": 1}}]
    return Plan("employees", "department_joined" if department is not None else "joined_department", pipeline=pipeline)


def leaderboard_plan(month, department=None, limit=10):
    # Rank by late minutes alone, then apply the rank cutoff. Sorting by employee
    # code inside the window would break ties that must share a rank.
    first, last = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                             "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                             "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                       "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}])
    return Plan("attendance_logs", "date_employee", pipeline=pipeline)


def trend_plan(department, first, last):
    count = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    if not 1 <= count <= 92:
        invalid("Trend range must contain 1 to 92 days", "query")
    # Generate the bounded calendar inside MongoDB. An existing department anchors
    # the series even when it has no logs or nobody has joined by the requested day.
    pipeline = [
        {"$match": {"department": department}},
        # Read department membership once, rather than joining employees again
        # for every log. Join dates still determine each day's eligible headcount.
        {"$group": {"_id": None, "people": {"$push": {"code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "people": 1, "days": {"$range": [0, count]}}}, {"$unwind": "$days"},
        {"$set": {"day": {"$dateAdd": {"startDate": mongo_date(first), "unit": "day", "amount": "$days"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "+05:30"}},
                  "is_working_day": weekday("$day")}},
        {"$lookup": {"from": "attendance_logs", "localField": "date", "foreignField": "date",
                     "let": {"codes": "$people.code"}, "pipeline": [
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None, "present_count": {"$sum": present_weight()},
                         "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$project": {"date": 1, "is_working_day": 1,
                       "headcount": {"$size": {"$filter": {"input": "$people", "as": "person",
                                      "cond": {"$lte": ["$$person.joined_on", "$date"]}}}},
                       "present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]},
                       "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            rounded({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$set": {"_rate": {"$toDecimal": "$attendance_rate"}}},
        # Gap filling makes seven rows equal seven calendar days. $avg ignores
        # null weekend rates, but those rows still occupy positions in the window.
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": "$_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": rounded("$moving_avg_7d", 4)}},
        {"$unset": "_rate"}, {"$sort": {"date": 1}}
    ]
    return Plan("employees", "department_code", pipeline=pipeline)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(emp_code: str, month: MonthString):
    rows = monthly_plan(emp_code, month).run()
    if not rows:
        raise HTTPException(404, "Unknown employee")
    return json_value(rows[0])


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": json_value(summary_plan(month, department).run())}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(month: MonthString, department: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=50)] = 10):
    return {"month": month, "items": json_value(leaderboard_plan(month, department, limit).run())}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(department: str, first: Annotated[DateString, Query(alias="from")],
                     last: Annotated[DateString, Query(alias="to")]):
    rows = trend_plan(department, first, last).run()
    if not rows:
        raise HTTPException(404, "Unknown department")
    return {"department": department, "items": json_value(rows)}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}", response_model=ExplainResult)
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: MonthString | None = None,
                     department: str | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     date_from: DateString | None = None, date_to: DateString | None = None,
                     status: Status | None = None, first: Annotated[DateString | None, Query(alias="from")] = None,
                     last: Annotated[DateString | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    required = {"attendance_list": {}, "employee_monthly": {"emp_code": emp_code, "month": month},
                "department_summary": {"month": month}, "late_leaderboard": {"month": month},
                "department_trend": {"department": department, "from": first, "to": last}}[endpoint]
    for name, value in required.items():
        if value is None:
            invalid(f"Missing required parameter: {name}", "query")
    if endpoint == "attendance_list":
        plan = attendance_plan(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        plan = monthly_plan(emp_code, month)
    elif endpoint == "department_summary":
        plan = summary_plan(month, department)
    elif endpoint == "late_leaderboard":
        plan = leaderboard_plan(month, department, limit)
    else:
        plan = trend_plan(department, first, last)
    result = db.command("explain", plan.command(), verbosity="executionStats")
    return {"endpoint": endpoint, "collection": plan.collection, "explain": json_value(result)}
