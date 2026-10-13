# HROne Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne Employee Attendance & Analytics API.

The service provides employee profile management, daily attendance punch recording, manual regularization with append-only audit trails, and reporting analytics powered by MongoDB aggregation pipelines.

---

## 1. Prerequisites & Environment

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (Local MongoDB, Docker, or MongoDB Atlas)

### Virtual Environment Setup

```bash
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (CMD):
.venv\Scripts\activate.bat
# Linux / macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

Configure database connection strings via `.env` in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
TEST_MONGO_URI=mongodb://localhost:27017
TEST_MONGO_DB=attendance_test_db
```

*Note: Environment variables in the host environment take precedence over `.env` values.*

---

## 2. Project Structure

```
candidate_project/
├── app/
│   └── main.py              # Application entrypoint, FastAPI routes, models, and pipelines
├── sample_data/
│   ├── employees.json        # Reference employee documents
│   └── attendance_logs.json  # Reference attendance log documents
├── tests/
│   └── test_app.py          # Complete unit and MongoDB integration test suite
├── openapi.yaml              # OpenAPI 3.1 formal contract and business rules (R1-R10)
├── DATA_MODEL.md             # MongoDB data model and document schema contracts
├── PROBLEM_STATEMENT.docx    # HROne assignment specification brief
├── REVIEW.md                 # Starter code defect audit and resolution log
├── DECISIONS.md              # Architectural decisions and design trade-offs (150-200 words)
├── README.md                 # Project documentation and API reference
├── requirements.txt          # Python package dependencies
└── sample_seed.py            # Database seeder for sample_data/
```

---

## 3. Database Indexes

Indexes are created automatically at application startup in `app/main.py` (`_create_indexes`) with up to 3 bounded retry attempts. If index initialization fails, the FastAPI lifespan context manager raises a `RuntimeError` and terminates startup.

### `employees` Collection
1. **`emp_code_unique`**: `{ emp_code: 1 }` (`unique=True`)  
   Enforces employee code uniqueness at the database engine level; prevents race conditions during employee registration.
2. **`joined_on_asc`**: `{ joined_on: 1 }`  
   Supports date filtering for monthly headcount calculations.
3. **`dept_joined_on`**: `{ department: 1, joined_on: 1 }`  
   Compound index serving department-filtered headcount range queries in analytics.

### `attendance_logs` Collection
1. **`emp_date_unique`**: `{ emp_code: 1, date: 1 }` (`unique=True`)  
   Composite natural key index enforcing exactly one record per employee per calendar date; rejects concurrent duplicate punch-ins.
2. **`date_desc_emp_asc`**: `{ date: -1, emp_code: 1 }`  
   Compound index serving cursor sorting for paginated attendance listings (`GET /attendance`).
3. **`date_asc`**: `{ date: 1 }`  
   Supports range-filtered queries in analytics pipelines.

---

## 4. Seeding Sample Data

To populate the database with the initial sample dataset from `sample_data/`:

```bash
python sample_seed.py
```

*Note: This drops and reloads `employees` and `attendance_logs` in the configured `MONGO_DB`.*

---

## 5. Running the Application

Start the Uvicorn ASGI server:

```bash
uvicorn app.main:app --port 8000 --reload
```

- **Interactive API Documentation (Swagger UI)**: `http://localhost:8000/docs`
- **OpenAPI Schema (JSON)**: `http://localhost:8000/openapi.json`
- **Health / Readiness Check**: `http://localhost:8000/health`

---

## 6. Business Rules Reference (R1–R10)

1. **R1 Time Zone & Attendance Date**: Shift times and attendance dates are in Indian Standard Time (IST, `UTC+05:30`). A record's `date` (`YYYY-MM-DD`) is the IST calendar date of its punch-in. For overnight shifts (`shift_end <= shift_start`), punch-ins strictly before `shift_end` IST belong to the previous calendar day. Instants are truncated to whole seconds before calculation and storage.
2. **R2 Lateness**: Grace period is strictly 10 minutes (600 seconds). If arrival is more than 600 seconds after `shift_start`, `late_minutes = floor(elapsed_seconds / 60)`. Otherwise, `0`.
3. **R3 Overtime**: Counted only when minutes between `shift_end` and `punch_out` are `>= 30`. `overtime_minutes = floor(elapsed_seconds / 60)`. Otherwise, `0`.
4. **R4 Work Hours**: `(punch_out - punch_in) in seconds / 3600`, rounded half-up to 2 decimal places. Stored as `null` until punch-out.
5. **R5 Half Day**: `half_day = True` if rounded `work_hours < 4.50`. Counts as `0.5` of a present day.
6. **R6 Presence Statuses**: Accepted statuses are `PRESENT`, `WFH`, `ON_DUTY`, `ABSENT`, and `LEAVE`. `half_day` is a derived boolean field, not an attendance status.
7. **R7 Working Days**: Monday through Friday. Mid-month joiners count working days from their `joined_on` date. Weekend records contribute to overtime and lateness totals, but only weekday records contribute to `present_days`.
8. **R8 Rounding**: Decimal half-up (`ROUND_HALF_UP`). 2 decimal places for counts and hours; 4 decimal places for rates.
9. **R9 Headcount**: Employees with `joined_on <= cutoff_date`, regardless of whether attendance logs exist.
10. **R10 Pagination**: 1-based indexing (`page >= 1`). Default `page_size = 20`, maximum `100`. `total` always reflects the filtered record count.

---

## 7. API Reference & Endpoint Details

### System & Health

#### `GET /health`
- **Description**: Readiness probe verifying MongoDB connectivity and index initialization.
- **Responses**:
  - `200 OK`: `{"status": "ok"}`
  - `503 Service Unavailable`: MongoDB unreachable or indexes not initialized.

---

### Employee Management

#### `POST /employees`
- **Description**: Registers a new employee profile. Enforces unique `emp_code`.
- **Request Body**:
  ```json
  {
    "emp_code": "EMP0007",
    "name": "Kavita Sharma",
    "email": "kavita@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-02-01"
  }
  ```
- **Responses**:
  - `201 Created`:
    ```json
    {
      "emp_code": "EMP0007",
      "name": "Kavita Sharma",
      "email": "kavita@example.com",
      "department": "Engineering",
      "shift_start": "09:30",
      "shift_end": "18:30",
      "joined_on": "2026-02-01",
      "created_at": 1770000000000
    }
    ```
  - `409 Conflict`: `emp_code already exists`
  - `422 Unprocessable Entity`: Validation failure (e.g. invalid date format, identical shift start/end).

#### `GET /employees`
- **Description**: Returns a paginated list of employees sorted by `emp_code` ascending.
- **Query Parameters**:
  - `department` (optional string): Filter by department.
  - `page` (optional integer, default `1`, minimum `1`).
  - `page_size` (optional integer, default `20`, range `1-100`).
- **Responses**:
  - `200 OK`:
    ```json
    {
      "items": [...],
      "total": 6,
      "page": 1,
      "page_size": 20
    }
    ```

---

### Attendance Recording

#### `POST /attendance/punch-in`
- **Description**: Creates a new attendance record for an employee.
- **Request Body**:
  ```json
  {
    "emp_code": "EMP0001",
    "punched_at": 1783310280000,
    "status": "PRESENT"
  }
  ```
  *(If `punched_at` is omitted, the current UTC time is used. Valid statuses: `PRESENT`, `WFH`, `ON_DUTY`.)*
- **Responses**:
  - `201 Created`:
    ```json
    {
      "emp_code": "EMP0001",
      "date": "2026-07-06",
      "status": "PRESENT",
      "punch_in": 1783310280000,
      "punch_out": null,
      "work_hours": null,
      "late_minutes": 0,
      "overtime_minutes": 0,
      "half_day": false,
      "history": []
    }
    ```
  - `404 Not Found`: Employee not found.
  - `409 Conflict`: Attendance record for employee on this date already exists.
  - `422 Unprocessable Entity`: Invalid timestamp or status.

#### `POST /attendance/punch-out`
- **Description**: Closes the latest applicable open attendance record at or before `punched_at`. If the latest record is already closed, returns 409 and does not close older open records.
- **Request Body**:
  ```json
  {
    "emp_code": "EMP0001",
    "punched_at": 1783343100000
  }
  ```
- **Responses**:
  - `200 OK`:
    ```json
    {
      "emp_code": "EMP0001",
      "date": "2026-07-06",
      "status": "PRESENT",
      "punch_in": 1783310280000,
      "punch_out": 1783343100000,
      "work_hours": 9.12,
      "late_minutes": 0,
      "overtime_minutes": 0,
      "half_day": false,
      "history": []
    }
    ```
  - `404 Not Found`: Employee or open record not found.
  - `409 Conflict`: Record is already punched out.
  - `422 Unprocessable Entity`: `punched_at` <= `punch_in` or exceeds 24 hours.

#### `GET /attendance`
- **Description**: Returns paginated attendance logs sorted by `date` descending, then `emp_code` ascending.
- **Query Parameters**:
  - `emp_code` (optional string): Filter by employee.
  - `date_from` (optional `YYYY-MM-DD`): Inclusive start date.
  - `date_to` (optional `YYYY-MM-DD`): Inclusive end date.
  - `status` (optional string): `PRESENT`, `WFH`, `ON_DUTY`, `ABSENT`, `LEAVE`.
  - `page` (optional integer, default `1`, minimum `1`).
  - `page_size` (optional integer, default `20`, range `1-100`).
- **Responses**:
  - `200 OK`:
    ```json
    {
      "items": [...],
      "total": 9,
      "page": 1,
      "page_size": 20
    }
    ```
  - `422 Unprocessable Entity`: Invalid date string or `date_from > date_to`.

#### `PATCH /attendance/{emp_code}/{date}`
- **Description**: Regularizes (corrects) an attendance record and appends an audit entry. Uses optimistic concurrency control.
- **Request Body**:
  ```json
  {
    "status": "PRESENT",
    "punch_in": 1783312200000,
    "punch_out": 1783343100000,
    "reason": "Forgot badge at entry gate",
    "regularized_by": "hr.admin"
  }
  ```
- **Responses**:
  - `200 OK`: Record with updated metrics and appended audit history.
  - `404 Not Found`: Employee or record not found.
  - `409 Conflict`: Concurrent modification detected; retry request.
  - `422 Unprocessable Entity`: Request specifies no changes, supplies punches for `ABSENT`/`LEAVE`, or violates date integrity.

---

### Analytics & Reporting

#### `GET /analytics/employees/{emp_code}/monthly`
- **Description**: Monthly attendance breakdown for one employee. Working days are computed from `joined_on` for mid-month joiners.
- **Query Parameters**: `month` (required `YYYY-MM`).
- **Pipeline Architecture**: Grouping, presence day aggregation, late minutes, and overtime sums are computed inside MongoDB; attendance percentage is formatted in Python.
- **Responses**:
  - `200 OK`:
    ```json
    {
      "emp_code": "EMP0001",
      "month": "2026-07",
      "working_days": 23,
      "present_days": 1.0,
      "leave_days": 0,
      "late_count": 0,
      "total_late_minutes": 0,
      "total_overtime_minutes": 0,
      "attendance_pct": 4.35
    }
    ```

#### `GET /analytics/departments/summary`
- **Description**: Monthly attendance summary grouped by department.
- **Query Parameters**:
  - `month` (required `YYYY-MM`).
  - `department` (optional string).
- **Pipeline Architecture**: Pipeline starts from `employees` matching `joined_on <= last_day`, then performs a correlated sub-pipeline `$lookup` to `attendance_logs` with `preserveNullAndEmptyArrays: true`. Employees with zero logs are retained in `headcount`.
- **Responses**:
  - `200 OK`:
    ```json
    {
      "month": "2026-07",
      "items": [
        {
          "department": "Engineering",
          "headcount": 2,
          "present_days": 1.0,
          "avg_work_hours": 9.12,
          "late_count": 0,
          "total_late_minutes": 0,
          "leave_count": 0,
          "on_duty_count": 0
        }
      ]
    }
    ```

#### `GET /analytics/leaderboard/late`
- **Description**: Leaderboard of top late-comers for a given month.
- **Query Parameters**:
  - `month` (required `YYYY-MM`).
  - `limit` (optional integer, default `10`, range `1-50`).
  - `department` (optional string).
- **Pipeline Architecture**: Ranking is computed entirely inside MongoDB via `$setWindowFields` using `$rank` (competition ranking: `1, 2, 2, 4`), matching `rank <= limit`. Ties at the cutoff are included.
- **Responses**:
  - `200 OK`:
    ```json
    {
      "month": "2026-07",
      "items": [
        {
          "rank": 1,
          "emp_code": "EMP0003",
          "name": "Neha Iyer",
          "department": "Sales",
          "total_late_minutes": 35,
          "late_count": 1
        }
      ]
    }
    ```

#### `GET /analytics/departments/{department}/trend`
- **Description**: Daily attendance trend across every calendar day in the range.
- **Query Parameters**:
  - `from` (required `YYYY-MM-DD`).
  - `to` (required `YYYY-MM-DD`).
- **Pipeline Architecture**:
  1. `$match` date range on `attendance_logs`.
  2. Correlated `$lookup` to `employees` filtering `department` early and projecting `_id: 1` to discard non-department records immediately.
  3. `$unionWith` fallback from `employees` to guarantee documents exist even when logs are empty.
  4. `$densify` across all calendar days within bounds.
  5. Correlated sub-pipeline `$lookup` for active headcount on each day (`joined_on <= date`).
  6. `$setWindowFields` calculating 7-day moving averages in MongoDB.
- **Responses**:
  - `200 OK`:
    ```json
    {
      "department": "Engineering",
      "items": [
        {
          "date": "2026-07-06",
          "is_working_day": true,
          "headcount": 1,
          "present_count": 1.0,
          "late_count": 0,
          "attendance_rate": 1.0,
          "moving_avg_7d": 1.0
        }
      ]
    }
    ```
  - `404 Not Found`: Department has no employees.
  - `422 Unprocessable Entity`: `to < from` or date range exceeds 92 days.

---

### Administration & Explain Plans

#### `GET /admin/explain/{endpoint}`
- **Description**: Returns MongoDB execution explain plans with `executionStats` for queries powering operational endpoints.
- **Supported `endpoint` Path Parameters**:
  - `attendance_list`
  - `employee_monthly`
  - `department_summary`
  - `late_leaderboard`
  - `department_trend`
- **Responses**:
  - `200 OK`: Full MongoDB execution plan document verifying index utilization (`IXSCAN`) and absence of unindexed scans.

---

## 8. Test Execution & Verification

Tests are divided into pure unit tests (runnable without database connectivity) and integration tests (executed against a live MongoDB test database).

```bash
# 1. Run complete test suite (unit + MongoDB integration)
pytest tests/test_app.py -v

# 2. Run unit tests only
pytest tests/test_app.py -v -k "not (TestEmployees or TestPunchIn or TestPunchOut or TestListAttendance or TestRegularize or TestEmployeeMonthly or TestDepartmentSummary or TestLateLeaderboard or TestDepartmentTrend or TestHealth or TestExplain)"

# 3. Run query explain-plan tests only
pytest tests/test_app.py -k "TestExplain" -v
```

### Verified Test Results
- **Total Tests**: 108
- **Passing**: 108 (100%)
- **Failing**: 0
- **Skipped**: 0
- **Database Safety**: Integration tests use an isolated database (`attendance_test_db`) which is dropped before and after test execution. Production data is never touched.
