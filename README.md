# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne engineering assignment.

---

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (local, Docker, or Atlas free tier M0)

---

## Setup

```bash
# 1. Clone / unzip the project
cd candidate_project

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Environment variables

Create a `.env` file in the project root (never commit it):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Real environment variables always win over `.env`. For Atlas:

```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true
MONGO_DB=attendance_db
```

> MongoDB indexes are created **automatically at server startup** with a bounded retry mechanism and logging:
> - `employees`: `emp_code` (unique), `joined_on`, `(department, joined_on)` compound
> - `attendance_logs`: `(emp_code, date)` (unique), `(date DESC, emp_code ASC)` compound, `date ASC`

---

## Seed sample data (optional)

```bash
python sample_seed.py
```

This loads the 9 sample documents from `sample_data/` into the configured database.  
The grader uses a much larger hidden dataset; the sample data shows document shapes only.

---

## Start the server

```bash
uvicorn app.main:app --port 8000 --reload
```

Swagger UI: http://localhost:8000/docs  
OpenAPI JSON: http://localhost:8000/openapi.json  
Readiness probe: http://localhost:8000/health

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe (pings MongoDB and verifies index initialization) |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees (paginated, sorted by `emp_code`) |
| `POST` | `/attendance/punch-in` | Record punch-in (creates attendance record) |
| `POST` | `/attendance/punch-out` | Record punch-out (closes open record) |
| `GET` | `/attendance` | List attendance records (paginated, DB-side sort) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize (manually correct) a record |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET` | `/analytics/departments/summary` | Department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard ($setWindowFields rank) |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend ($densify gap-filling) |
| `GET` | `/admin/explain/{endpoint}` | MongoDB explain for an endpoint's query |

Business rules R1–R10 and schema contracts are specified in `openapi.yaml` and `DATA_MODEL.md`.

---

## Running tests

```bash
# All tests (unit tests run; integration tests skip if MongoDB is unavailable)
pytest tests/test_app.py -v

# Unit tests only (no MongoDB required)
pytest tests/test_app.py -v -k "TestHalfUp or TestEpoch or TestAttendance or TestLate or TestWork or TestOvertime or TestRecompute or TestPydantic or TestStrictDateValidation"

# Integration tests only (require a running MongoDB)
TEST_MONGO_URI=mongodb://localhost:27017 TEST_MONGO_DB=attendance_test_db \
    pytest tests/test_app.py -v -k "not (TestHalfUp or TestEpoch or TestAttendanceDate or TestLateMinutes or TestWorkHours or TestOvertimeMinutes or TestRecompute or TestPydantic or TestStrictDateValidation)"
```

**Integration tests use a separate database** (`attendance_test_db` by default, configurable via `TEST_MONGO_DB`). The database is dropped before and after the test run — production data is never touched.

If MongoDB is unavailable, integration tests are automatically skipped and reported as `SKIPPED`, not as passing.

---

## Project structure

```
candidate_project/
├── app/
│   └── main.py          # All application code (single file, as required)
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── tests/
│   └── test_app.py      # Unit + integration tests
├── openapi.yaml          # Formal API specification & business rules R1-R10
├── DATA_MODEL.md         # Data model specification & MongoDB schema contracts
├── REVIEW.md             # Defects found in the starter code
├── DECISIONS.md          # Architectural decisions & design rationale
├── README.md             # Setup and operational documentation
├── requirements.txt
└── sample_seed.py        # Loads sample_data/ into MongoDB
```

---

## Key implementation notes

- **All timestamps** in requests and responses are **epoch milliseconds** (integer). Values that look like seconds (< 100,000,000,000) are rejected with 422.
- **Rounding** uses half-up (not Python's default banker's rounding) via `Decimal.quantize(ROUND_HALF_UP)`.
- **Concurrency** for punch-in and employee creation is handled by MongoDB unique indexes (`DuplicateKeyError` → 409), avoiding application-level locks.
- **Regularization** uses optimistic concurrency (matching pre-read state and history length) to detect concurrent updates and return 409 Conflict.
- **Analytics pushdown**: Leaderboard competition ranking uses `$setWindowFields` with `$rank`, department summary starts from `employees` with correlated `$lookup` (preserving zero-log headcount), and department trend uses `$densify` with 7-day moving average windows in MongoDB.
- **Explain endpoint** uses `db.command("explain", ..., verbosity="executionStats")` and shares the exact pipelines used by the operational endpoints.
- **No `.env` file or secrets** are committed to the repository.

