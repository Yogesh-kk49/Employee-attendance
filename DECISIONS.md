# Architectural Decisions

### 1. Database Indexes & Initialization
Startup creates required BSON indexes idempotently with bounded retries: unique `emp_code`, `joined_on`, and `(department, joined_on)` on `employees`; unique `(emp_code, date)`, `(date DESC, emp_code ASC)`, and `date ASC` on `attendance_logs`. These eliminate collection scans across analytics queries and listing endpoints.

### 2. Concurrency Control
Race conditions on `POST /attendance/punch-in` are handled without application-level locks: MongoDB's unique `(emp_code, date)` compound index rejects duplicate insertions via `DuplicateKeyError`, returning HTTP 409. For `PATCH /attendance/{emp_code}/{date}`, optimistic locking uses atomic `find_one_and_update` matching pre-read field values (`punch_in`, `punch_out`, `status`) and array length `history.{len}: {"$exists": False}`, returning 409 on interleaved modifications.

### 3. Pure Aggregation Analytics
All analytics operations execute inside MongoDB pipelines. The late leaderboard leverages `$setWindowFields` with `$rank` to calculate competition rankings (1, 2, 2, 4) in-engine. Department summary queries from `employees` with outer-joined correlated `$lookup` on `attendance_logs` to retain zero-log headcount. Department trend generates complete calendars using `$densify` and calculates 7-day moving averages via `$setWindowFields`.

### 4. Scaling to 100× Data
At 100× data volume, the daily correlated headcount `$lookup` in the trend pipeline should be replaced by a pre-aggregated milestone array or time-series collection to avoid repetitive employee scans.
