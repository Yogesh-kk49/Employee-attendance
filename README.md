# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Real environment variables take priority over `.env`. Indexes are created at startup; no manual steps. MongoDB 6.0+ is required (`$densify`, `$setWindowFields`).

## Tests
`pytest tests/test_app.py -v`. Unit tests need no database. Integration tests use a separate database (`TEST_MONGO_URI`, `TEST_MONGO_DB`, default `attendance_test_db`, dropped before and after) and are skipped when MongoDB is unreachable.

## Notes
- `REVIEW.md` lists the starter defects; `DECISIONS.md` explains the design choices.
- Core business rules (R1–R10: IST shift calculations, 10-minute lateness grace, 30-minute overtime threshold, half-up rounding, presence statuses, working days, and headcount) are fully implemented in `app/main.py`.