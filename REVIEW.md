# Starter review

Line numbers refer to the original 162-line starter. Tests in `tests/test_api.py` use a separate randomly named MongoDB database.

| # | Where (original function / line) | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `health`, 72-74 | Reports readiness without checking MongoDB. | Disconnect MongoDB: still returns 200. | Ping the database; database errors return 503. |
| 2 | Database setup, 22-23 | No indexes for uniqueness or query performance. | Parallel inserts duplicate business keys; explain shows collection scans. | Idempotent named startup indexes, with readiness retry after an outage. |
| 3 | `EmployeeIn`, 53-60 | No code format or name/department length checks. | Code `1`, empty name, oversized department. | Pydantic pattern and length constraints. |
| 4 | `EmployeeIn`, 57-60 | Invalid email, shift times and joining dates accepted. | `bad`, `24:00`, `2026-02-30`, non-padded dates. | Patterns and actual calendar-date validation. |
| 5 | `EmployeeIn`, 58-59 | Equal shift start/end accepted on creation. | Create with 09:30/09:30. | Cross-field validator returns 422. Seeded overnight shifts still follow end <= start. |
| 6 | `PunchInIn`, 65 | Coerces strings/floats; accepts null, seconds and out-of-range timestamps. | Send a string, float or seconds. | Strict bounded integer; omission alone defaults to now. |
| 7 | `PunchInIn`, 66 | Any status accepted. | Punch in with ABSENT or an invented status. | Presence-status enum. |
| 8 | `create_employee`, 79-83 | Check-then-insert race permits duplicates. | Twelve identical creates concurrently. | Unique index and duplicate-key-to-409 handling. |
| 9 | `create_employee`, 82 | Naive local time may be stored as UTC incorrectly. | Run on a non-UTC host. | Aware UTC clock, truncated to seconds. |
| 10 | Responses, 85, 96, 121, 150 | Dates become ISO strings instead of epoch milliseconds. | Inspect creation/punch times and nested history. | Recursive integer-millisecond serialization. |
| 11 | `list_employees`, 93 | Page one skips the first page. | Four employees, page=1, page_size=2. | Offset `(page - 1) * page_size`. |
| 12 | `list_employees`, 94 | Total ignores department filter. | Filter a department with three of four employees. | Count with the same filter. |
| 13 | `list_employees`, 95 | Missing employee-code ordering. | Insert codes out of order, then paginate. | Database sort by code ascending. |
| 14 | Both lists, 89, 130-131 | Invalid pagination accepted. | page=0 or page_size=101. | page >= 1; page_size 1..100. |
| 15 | Filters, 91, 134, 142 | Empty filters treated as absent. | `department=` returns all departments. | Distinguish None from supplied values; status is an enum. |
| 16 | `punch_in`, 101, 113 | Unknown employee dereferenced. | Unknown code causes server error. | Shared employee lookup returns 404. |
| 17 | `punch_in`, 102-103 | Machine-local time/date ignores IST. | Punch near IST midnight on a UTC host. | Explicit UTC conversion and IST calendar. |
| 18 | `punch_in`, 103 | No overnight previous-day assignment. | 01:00 punch for 22:00-06:00 shift. | Use previous day when local time precedes shift end. |
| 19 | `punch_in`, 102 | Milliseconds not truncated. | 09:40:00.999 must become 09:40:00. | Truncate before calculation and storage. |
| 20 | `punch_in`, 104-118 | Concurrent insert race. | Twelve parallel punches for one employee/day. | Unique employee/date index; one 201, others 409. |
| 21 | `compute_late_minutes`, 35 | Shift start uses punch date/time zone. | After-midnight overnight punch needs yesterday's start. | IST boundaries from attendance date. |
| 22 | `compute_late_minutes`, 36-37 | Floors before grace check. | 09:40:01 incorrectly returns zero. | Compare seconds > 600, then floor minutes. |
| 23 | `compute_work_hours`, 41 | Float/banker's rounding differs from half-up. | 126 seconds = 0.035 hours must become 0.04. | Decimal and ROUND_HALF_UP. |
| 24 | `compute_overtime`, 46 | Overnight end stays on same day. | Overnight out at 06:40 next day. | Advance end one calendar day. |
| 25 | `compute_overtime`, 47 | Counts overtime below 30 minutes. | 18:59:59 after an 18:30 end must yield zero. | Floor minutes and enforce >= 30. |
| 26 | `compute_overtime`, 46-47 | Naive/aware subtraction can fail. | Use default PyMongo datetime with aware shift end. | Aware UTC reads and defensive normalization. |
| 27 | `list_attendance`, 127-143 | Invalid dates, status and inverted ranges accepted. | Invalid date/status or from > to. | Shared validation returns 422. |
| 28 | `list_attendance`, 144-147 | Fetches/sorts all matching records in Python. | Small page still loads the full collection. | Indexed database sort/skip/limit and filtered count. |
| 29 | `list_attendance`, 145 | Missing code tie-breaker. | Multiple employees on the same date. | Date descending, code ascending. |
| 30 | Responses, 119, 149 | Exposes database identity as `id`. | Inspect punch-in/list responses. | Strip internal `_id`; no substitute id. |
| 31 | `list_attendance`, 148-150 | Legacy defaults missing. | Read EMP0002 sample without history/half_day. | Normalize public fields without rewriting seed data. |

## Examined and retained

- `load_dotenv()` already preserves real environment variables; `override=False` now makes that explicit.
- Synchronous PyMongo in synchronous FastAPI routes is valid: routes run in FastAPI's worker thread pool.
- The original employee lookup correctly used the business code, not MongoDB identity.
- Inclusive date filters are correct for validated zero-padded YYYY-MM-DD strings.
- Attendance slicing had the correct one-based offset; fetching everything first was the defect.
- Late minutes must start at shift start, not at the end of grace; the original intention was correct.
- Ignoring client-supplied derived fields is required, so Pydantic's default extra-field ignoring remains.
- Initial null work hours, zero overtime, false half-day and empty history were correct. Punch events do not append history.

## Completed contract

Implemented punch-out, corrections, four aggregation-backed analytics endpoints and shared-query explain output. Conditional updates compare the stored snapshot before writing; corrections atomically append history and conflicting requests receive 409. Trend calendars are generated in MongoDB with `$range`, `$dateAdd` and `$unwind`; windows use `$setWindowFields`. Analytics read authoritative stored metrics rather than deriving them from punch timestamps.

Successful responses have explicit models checked against the supplied field lists. The trend pipeline reads department membership once and reuses it across the calendar rather than joining employees for every log. Verification includes 39 functional tests and a separate 100,000-record check with a 92-day trend, passing on MongoDB 6.0.28 and 7 under 1 GiB RAM and 2 CPU limits. Exact tested dependency versions are recorded in `constraints.txt`.
