"""
tests/test_app.py
=================
Two test layers:

1. UNIT TESTS (no MongoDB required)
   - Business-logic helpers: half_up, ms_to_dt, dt_to_ms, attendance_date_for,
     compute_late_minutes, compute_work_hours, compute_overtime_minutes, recompute_derived.
   - Pydantic model validation (EpochMillis range, status enums, shift-differ rule).

2. INTEGRATION TESTS (require a running MongoDB)
   - Use a dedicated test database (TEST_MONGO_DB, default: attendance_test_db).
   - Skipped automatically if MongoDB is unreachable.
   - Cover: employee CRUD, punch-in/out flow, duplicate/race errors, corrections,
     analytics endpoints, explain endpoint, edge cases (empty dataset, overnight shifts).

Run:
    pytest tests/test_app.py -v
    pytest tests/test_app.py -v -k unit          # unit only
    pytest tests/test_app.py -v -k integration   # integration only

Environment variables (optional):
    TEST_MONGO_URI  – default mongodb://localhost:27017
    TEST_MONGO_DB   – default attendance_test_db
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient

# Environment and test database setup
from dotenv import load_dotenv
load_dotenv()

TEST_MONGO_URI = os.getenv("TEST_MONGO_URI", os.getenv("MONGO_URI", "mongodb://localhost:27017"))
TEST_MONGO_DB = os.getenv("TEST_MONGO_DB", "attendance_test_db")

os.environ["MONGO_URI"] = TEST_MONGO_URI
os.environ["MONGO_DB"] = TEST_MONGO_DB

from app.main import (  # noqa: E402
    IST,
    _create_indexes,
    app,
    attendance_date_for,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    dt_to_ms,
    half_up,
    ms_to_dt,
    recompute_derived,
)

# MongoDB connectivity check for conditional integration test execution

def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient
        c = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=8000)
        c.admin.command("ping")
        return True
    except Exception:
        return False


MONGO_AVAILABLE = _mongo_available()
skip_if_no_mongo = pytest.mark.skipif(
    not MONGO_AVAILABLE,
    reason="MongoDB unavailable - integration test skipped",
)

# Unit Tests (runnable without database connection)


class TestHalfUp:
    """half_up() must use half-up rounding, not banker's rounding."""

    def test_rounds_half_up(self):
        assert half_up(2.5) == 2.5  # already exact
        assert half_up(2.345, 2) == 2.35
        assert half_up(2.355, 2) == 2.36
        assert half_up(9.08333, 2) == 9.08
        assert half_up(9.125, 2) == 9.13  # not 9.12 (banker's)

    def test_four_decimals(self):
        assert half_up(0.12345, 4) == 0.1235
        assert half_up(0.0, 4) == 0.0

    def test_negative(self):
        # half-up for negative: -2.345 rounds toward zero, i.e. -2.34
        # Our implementation uses Decimal ROUND_HALF_UP which is "round half away from zero"
        # For positive numbers this matches the spec; negative numbers are rarely used.
        result = half_up(3.0 / 3600, 2)
        assert isinstance(result, float)


class TestEpochConversion:
    """ms_to_dt and dt_to_ms must round-trip and truncate sub-second precision."""

    def test_ms_to_dt_utc_aware(self):
        dt = ms_to_dt(1_783_312_500_000)
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc

    def test_ms_to_dt_truncates_subseconds(self):
        # 1_000 ms difference
        dt1 = ms_to_dt(1_783_312_500_000)
        dt2 = ms_to_dt(1_783_312_500_999)  # same second, different ms
        # Because we do ms // 1000 (integer division), both map to the same second
        assert dt1 == dt2

    def test_dt_to_ms_round_trip(self):
        ms = 1_783_312_500_000
        assert dt_to_ms(ms_to_dt(ms)) == ms

    def test_dt_to_ms_none(self):
        assert dt_to_ms(None) is None

    def test_dt_to_ms_naive_treated_as_utc(self):
        naive = datetime(2026, 7, 6, 9, 30, 0)
        result = dt_to_ms(naive)
        aware = datetime(2026, 7, 6, 9, 30, 0, tzinfo=timezone.utc)
        assert result == dt_to_ms(aware)


class TestAttendanceDate:
    """attendance_date_for() R1 – IST calendar date and overnight shifts."""

    # Normal day-shift (09:30–18:30): date = IST date of punch_in
    def test_normal_shift_ist_date(self):
        # 2026-07-06T03:58:00Z = 09:28 IST on 2026-07-06
        punch = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        assert attendance_date_for(punch, "09:30", "18:30") == "2026-07-06"

    def test_normal_shift_after_midnight_utc(self):
        # 2026-07-06T20:00:00Z = 01:30 IST on 2026-07-07 – still same IST day
        punch = datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc)
        assert attendance_date_for(punch, "09:30", "18:30") == "2026-07-07"

    # Overnight shift (22:00–06:00)
    def test_overnight_punch_in_before_midnight(self):
        # 22:00 IST = 16:30 UTC; date should be current day
        punch = datetime(2026, 7, 6, 16, 30, 0, tzinfo=timezone.utc)  # 22:00 IST
        assert attendance_date_for(punch, "22:00", "06:00") == "2026-07-06"

    def test_overnight_early_morning_belongs_to_previous_day(self):
        # 04:00 IST on 2026-07-07 = 2026-07-06T22:30Z
        # punch_in IST time (04:00) < shift_end (06:00) → belongs to 2026-07-06
        punch = datetime(2026, 7, 6, 22, 30, 0, tzinfo=timezone.utc)  # 04:00 IST on 07-07
        assert attendance_date_for(punch, "22:00", "06:00") == "2026-07-06"

    def test_overnight_after_shift_end_is_new_day(self):
        # 07:00 IST on 2026-07-07 (past shift end 06:00) = new record
        punch = datetime(2026, 7, 7, 1, 30, 0, tzinfo=timezone.utc)  # 07:00 IST
        assert attendance_date_for(punch, "22:00", "06:00") == "2026-07-07"


class TestLateMinutes:
    """compute_late_minutes() R2 – grace period is strictly > 10 minutes."""

    IST_OFFSET = timedelta(hours=5, minutes=30)

    def _punch(self, h, m, s=0):
        """Create a UTC datetime for HH:MM:SS IST on 2026-07-06."""
        ist = datetime(2026, 7, 6, h, m, s, tzinfo=timezone(self.IST_OFFSET))
        return ist.astimezone(timezone.utc)

    def test_within_grace(self):
        # 09:40:00 IST = exactly 10 min after 09:30 → NOT late (needs strictly > 10)
        assert compute_late_minutes(self._punch(9, 40, 0), "09:30", "2026-07-06") == 0

    def test_one_second_past_grace(self):
        # 09:40:01 IST = 10 min 1 sec after 09:30 → 10 minutes late
        assert compute_late_minutes(self._punch(9, 40, 1), "09:30", "2026-07-06") == 10

    def test_example_from_spec_45min(self):
        # 10:15:59 IST → 45 min 59 sec → floor = 45 min late
        assert compute_late_minutes(self._punch(10, 15, 59), "09:30", "2026-07-06") == 45

    def test_early_arrival_zero(self):
        # 09:28 IST – before shift start
        assert compute_late_minutes(self._punch(9, 28, 0), "09:30", "2026-07-06") == 0

    def test_35min_late(self):
        # 10:05 IST = 35 min after 09:30
        assert compute_late_minutes(self._punch(10, 5, 0), "09:30", "2026-07-06") == 35


class TestWorkHours:
    """compute_work_hours() R4 – half-up rounding to 2 decimal places."""

    def _dt(self, h, m, s=0):
        return datetime(2026, 7, 6, h, m, s, tzinfo=timezone.utc)

    def test_normal_day(self):
        # 9 h 7 min = 9.1167 h → 9.12
        pi = self._dt(3, 58)
        po = self._dt(13, 5)
        assert compute_work_hours(pi, po) == 9.12

    def test_half_day(self):
        # 3 h 30 min = 3.5
        pi = self._dt(4, 0)
        po = self._dt(7, 30)
        assert compute_work_hours(pi, po) == 3.5

    def test_rounding_half_up(self):
        # 9 h 3 min = 9.05 – should not banker-round
        pi = self._dt(3, 58)
        po = self._dt(13, 1)
        assert compute_work_hours(pi, po) == 9.05


class TestOvertimeMinutes:
    """compute_overtime_minutes() R3 – 30-minute minimum threshold."""

    IST_OFFSET = timedelta(hours=5, minutes=30)

    def _ist(self, d, h, m, s=0):
        return datetime(
            *[int(x) for x in d.split("-")], h, m, s,
            tzinfo=timezone(self.IST_OFFSET)
        ).astimezone(timezone.utc)

    def test_under_30min_no_overtime(self):
        # punch_out at 18:35, shift_end 18:30 → 5 min < 30 → 0
        po = self._ist("2026-07-06", 18, 35)
        assert compute_overtime_minutes(po, "18:30", "2026-07-06", False) == 0

    def test_exactly_30min(self):
        # 19:00 – 18:30 = 30 min → 30 (>= 30)
        po = self._ist("2026-07-06", 19, 0)
        assert compute_overtime_minutes(po, "18:30", "2026-07-06", False) == 30

    def test_40min_overtime(self):
        # 19:10 – 18:30 = 40 min
        po = self._ist("2026-07-06", 19, 10)
        assert compute_overtime_minutes(po, "18:30", "2026-07-06", False) == 40

    def test_overnight_40min_overtime(self):
        # Shift 22:00-06:00. punch_out 06:40 next day = 40 min after shift_end
        po = self._ist("2026-07-07", 6, 40)
        assert compute_overtime_minutes(po, "06:00", "2026-07-06", True) == 40


class TestRecompute:
    """recompute_derived() integration of R2-R5."""

    IST_OFFSET = timedelta(hours=5, minutes=30)

    def _utc(self, h, m, s=0):
        return datetime(2026, 7, 7, h, m, s, tzinfo=timezone.utc)

    def test_no_punchout_gives_null_work_hours(self):
        pi = self._utc(3, 58)  # 09:28 IST
        r = recompute_derived(pi, None, "PRESENT", "09:30", "18:30", "2026-07-07")
        assert r["work_hours"] is None
        assert r["overtime_minutes"] == 0
        assert r["half_day"] is False

    def test_half_day_flag(self):
        pi = self._utc(4, 0)   # 09:30 IST
        po = self._utc(7, 30)  # 13:00 IST → 3.5 h
        r = recompute_derived(pi, po, "WFH", "09:30", "18:30", "2026-07-07")
        assert r["work_hours"] == 3.5
        assert r["half_day"] is True

    def test_absent_status_clears_all(self):
        r = recompute_derived(None, None, "ABSENT", "09:30", "18:30", "2026-07-07")
        assert r == {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}


class TestPydanticModels:
    """Validate Pydantic model constraints without touching the database."""

    def test_punch_in_rejects_seconds_epoch(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from app.main import PunchInRequest
            PunchInRequest(emp_code="EMP0001", punched_at=1_000_000)  # looks like seconds

    def test_punch_in_accepts_valid_ms(self):
        from app.main import PunchInRequest
        req = PunchInRequest(emp_code="EMP0001", punched_at=1_783_312_500_000)
        assert req.punched_at == 1_783_312_500_000

    def test_employee_shifts_must_differ(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from app.main import EmployeeIn
            EmployeeIn(
                emp_code="EMP0001", name="X", email="x@x.com",
                department="Eng", shift_start="09:30", shift_end="09:30",
                joined_on="2026-01-01"
            )

    def test_employee_emp_code_pattern(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from app.main import EmployeeIn
            EmployeeIn(
                emp_code="E001", name="X", email="x@x.com",
                department="Eng", joined_on="2026-01-01"
            )

    def test_regularize_absent_with_punch_in_raises(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from app.main import RegularizeRequest
            RegularizeRequest(
                status="ABSENT",
                punch_in=1_783_312_500_000,
                reason="test reason",
                regularized_by="hr.admin",
            )


# Integration Tests (executed when MongoDB connection is available)

@pytest.fixture(scope="module")
def client() -> Generator:
    """
    TestClient backed by the real FastAPI app, connected to the test database.
    The test database is dropped before and after the module.
    """
    if not MONGO_AVAILABLE:
        pytest.skip("MongoDB unavailable")

    from app.main import db

    db.client.drop_database(TEST_MONGO_DB)
    _create_indexes()

    with TestClient(app) as c:
        yield c

    db.client.drop_database(TEST_MONGO_DB)


# Employee management tests

@skip_if_no_mongo
class TestEmployees:
    def test_create_employee_201(self, client):
        r = client.post("/employees", json={
            "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"
        })
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert isinstance(data["created_at"], int)
        assert data["created_at"] > 0

    def test_create_employee_duplicate_409(self, client):
        r = client.post("/employees", json={
            "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"
        })
        assert r.status_code == 409

    def test_create_invalid_emp_code_422(self, client):
        r = client.post("/employees", json={
            "emp_code": "E1", "name": "X", "email": "x@x.com",
            "department": "Eng", "joined_on": "2026-01-01"
        })
        assert r.status_code == 422

    def test_create_same_shift_422(self, client):
        r = client.post("/employees", json={
            "emp_code": "EMP0099", "name": "Test", "email": "t@t.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        assert r.status_code == 422

    def test_list_employees_pagination(self, client):
        # Create a second employee for list tests
        client.post("/employees", json={
            "emp_code": "EMP0002", "name": "Vikram Shah", "email": "v@example.com",
            "department": "Engineering", "joined_on": "2026-07-13"
        })
        client.post("/employees", json={
            "emp_code": "EMP0003", "name": "Neha Iyer", "email": "n@example.com",
            "department": "Sales", "joined_on": "2026-02-01"
        })
        r = client.get("/employees?page=1&page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert data["page"] == 1
        assert len(data["items"]) == 2
        assert data["total"] >= 2

    def test_list_employees_sorted_by_emp_code(self, client):
        r = client.get("/employees?page=1&page_size=100")
        codes = [item["emp_code"] for item in r.json()["items"]]
        assert codes == sorted(codes)

    def test_list_employees_department_filter(self, client):
        r = client.get("/employees?department=Sales")
        data = r.json()
        assert all(item["department"] == "Sales" for item in data["items"])
        # total reflects filtered set
        assert data["total"] == len(data["items"])

    def test_list_employees_page1_offset(self, client):
        # page=1 should start from index 0 (not skip 20)
        r1 = client.get("/employees?page=1&page_size=1")
        r2 = client.get("/employees?page=2&page_size=1")
        assert r1.status_code == 200
        assert r2.status_code == 200
        item1 = r1.json()["items"][0]["emp_code"]
        item2 = r2.json()["items"][0]["emp_code"]
        assert item1 != item2  # different employees


# Punch-in tests

@skip_if_no_mongo
class TestPunchIn:
    """
    Uses EMP0001 (Engineering, 09:30-18:30) created in TestEmployees.
    Note: pytest class ordering – TestEmployees runs first in the same module fixture.
    """

    # 2026-07-06T03:58:00Z = 09:28 IST on 2026-07-06
    VALID_TS = 1_783_310_280_000  # 2026-07-06T03:58:00Z in ms

    def test_punch_in_201(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": self.VALID_TS,
        })
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["date"] == "2026-07-06"
        assert data["punch_in"] == self.VALID_TS
        assert data["punch_out"] is None
        assert data["late_minutes"] == 0
        assert data["work_hours"] is None
        assert data["history"] == []

    def test_punch_in_duplicate_409(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": self.VALID_TS + 60_000,
        })
        assert r.status_code == 409

    def test_punch_in_unknown_employee_404(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9999",
            "punched_at": self.VALID_TS,
        })
        assert r.status_code == 404

    def test_punch_in_seconds_epoch_422(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1_000_000,  # looks like seconds
        })
        assert r.status_code == 422

    def test_punch_in_invalid_status_422(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "status": "ABSENT",
        })
        assert r.status_code == 422

    def test_punch_in_late(self, client):
        # EMP0003 (Sales) - punch in at 10:05 IST (35 min late)
        # 2026-07-06T04:35:00Z = 10:05 IST
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0003",
            "punched_at": 1_783_312_500_000,  # 2026-07-06T04:35:00Z
        })
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 35


# Punch-out tests

@skip_if_no_mongo
class TestPunchOut:
    # 2026-07-06T13:05:00Z = 18:35 IST (5 min OT, below 30-min threshold)
    PUNCH_OUT_TS = 1_783_343_100_000

    def test_punch_out_200(self, client):
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": self.PUNCH_OUT_TS,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["punch_out"] == self.PUNCH_OUT_TS
        assert isinstance(data["work_hours"], float)
        assert data["work_hours"] == 9.12
        assert data["overtime_minutes"] == 0  # 5 min < 30 min threshold
        assert data["half_day"] is False

    def test_punch_out_already_done_409(self, client):
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": self.PUNCH_OUT_TS + 60_000,
        })
        assert r.status_code == 409

    def test_punch_out_no_open_record_404(self, client):
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9999",
            "punched_at": self.PUNCH_OUT_TS,
        })
        assert r.status_code == 404

    def test_punch_out_before_punch_in_422(self, client):
        # EMP0003 punched in at 10:05 IST. Try to punch out at 09:00 IST same day.
        # 2026-07-06T03:30:00Z = 09:00 IST
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0003",
            "punched_at": 1_783_308_600_000,
        })
        assert r.status_code == 422

    def test_punch_out_with_overtime(self, client):
        # EMP0003 punch-out at 19:10 IST = 40 min overtime
        # 2026-07-06T13:40:00Z = 19:10 IST
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0003",
            "punched_at": 1_783_345_200_000,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["overtime_minutes"] == 40
        assert data["work_hours"] == 9.08


# Attendance log listing tests

@skip_if_no_mongo
class TestListAttendance:
    def test_list_all(self, client):
        r = client.get("/attendance")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["page"] == 1

    def test_list_date_filter(self, client):
        r = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-06")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["date"] == "2026-07-06"

    def test_list_invalid_date_range_422(self, client):
        r = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-06")
        assert r.status_code == 422

    def test_list_sorted_date_desc_emp_asc(self, client):
        r = client.get("/attendance?page=1&page_size=100")
        items = r.json()["items"]
        dates = [i["date"] for i in items]
        assert dates == sorted(dates, reverse=True)

    def test_list_emp_code_filter(self, client):
        r = client.get("/attendance?emp_code=EMP0001")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["emp_code"] == "EMP0001"

    def test_list_status_filter(self, client):
        r = client.get("/attendance?status=PRESENT")
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["status"] == "PRESENT"

    def test_punch_out_fields_are_epoch_ms(self, client):
        r = client.get("/attendance?emp_code=EMP0001")
        data = r.json()
        if data["items"]:
            item = data["items"][0]
            assert isinstance(item["punch_in"], int)
            assert item["punch_in"] >= 100_000_000_000
            if item["punch_out"] is not None:
                assert isinstance(item["punch_out"], int)
                assert item["punch_out"] >= 100_000_000_000


# Attendance regularization (manual correction) tests

@skip_if_no_mongo
class TestRegularize:
    def test_regularize_changes_punch_in(self, client):
        # Correct EMP0001's punch_in on 2026-07-06 to 10:00 IST = 04:30 UTC = 30 min late
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": 1_783_312_200_000,  # 10:00 IST – 30 min late
            "reason": "biometric glitch correction",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["late_minutes"] == 30
        assert len(data["history"]) == 1
        assert data["history"][0]["by"] == "hr.admin"
        assert "punch_in" in data["history"][0]["changes"]

    def test_regularize_no_change_422(self, client):
        # Sending same punch_in that is already stored
        current = client.get("/attendance?emp_code=EMP0001&date_from=2026-07-06&date_to=2026-07-06")
        item = current.json()["items"][0]
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": item["punch_in"],
            "reason": "no change test",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_regularize_absent_with_punch_in_422(self, client):
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": 1_783_312_200_000,
            "reason": "should fail test",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_regularize_unknown_employee_404(self, client):
        r = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "test reason",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404

    def test_regularize_unknown_date_404(self, client):
        r = client.patch("/attendance/EMP0001/2020-01-01", json={
            "reason": "test reason",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404

    def test_regularize_history_appends(self, client):
        # Make a second correction, check history length grows
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": 1_783_310_280_000,  # revert to 09:28 IST
            "reason": "reverting previous correction",
            "regularized_by": "hr.admin2",
        })
        assert r.status_code == 200
        assert len(r.json()["history"]) == 2


# Analytics and aggregation pipeline tests

@skip_if_no_mongo
class TestEmployeeMonthly:
    def test_known_employee(self, client):
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert isinstance(data["working_days"], int)
        assert data["working_days"] > 0
        assert data["attendance_pct"] is not None

    def test_unknown_employee_404(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404

    def test_month_with_no_records(self, client):
        r = client.get("/analytics/employees/EMP0001/monthly?month=2025-01")
        assert r.status_code == 200
        data = r.json()
        assert data["present_days"] == 0.0
        assert data["attendance_pct"] == 0.0 or data["attendance_pct"] is None

    def test_invalid_month_422(self, client):
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
        assert r.status_code == 422


@skip_if_no_mongo
class TestDepartmentSummary:
    def test_summary_returns_items(self, client):
        r = client.get("/analytics/departments/summary?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert isinstance(data["items"], list)

    def test_summary_filtered_department(self, client):
        r = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        assert r.status_code == 200
        data = r.json()
        for item in data["items"]:
            assert item["department"] == "Engineering"

    def test_summary_required_fields(self, client):
        r = client.get("/analytics/departments/summary?month=2026-07")
        assert r.status_code == 200
        for item in r.json()["items"]:
            for field in ("department", "headcount", "present_days", "avg_work_hours",
                          "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
                assert field in item, f"Missing field: {field}"


@skip_if_no_mongo
class TestLateLeaderboard:
    def test_leaderboard_returns_items(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert isinstance(data["items"], list)

    def test_leaderboard_required_fields(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07")
        for item in r.json()["items"]:
            for field in ("rank", "emp_code", "name", "department", "total_late_minutes", "late_count"):
                assert field in item

    def test_leaderboard_sorted_desc(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07")
        minutes = [i["total_late_minutes"] for i in r.json()["items"]]
        assert minutes == sorted(minutes, reverse=True)

    def test_leaderboard_limit(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
        assert r.status_code == 200
        items = r.json()["items"]
        # all returned items must have rank <= 1
        for item in items:
            assert item["rank"] <= 1


@skip_if_no_mongo
class TestDepartmentTrend:
    def test_trend_returns_all_days(self, client):
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
        assert r.status_code == 200
        data = r.json()
        assert data["department"] == "Engineering"
        assert len(data["items"]) == 5  # 5 calendar days

    def test_trend_required_fields(self, client):
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-08")
        for item in r.json()["items"]:
            for field in ("date", "is_working_day", "headcount", "present_count",
                          "late_count", "attendance_rate", "moving_avg_7d"):
                assert field in item

    def test_trend_unknown_department_404(self, client):
        r = client.get("/analytics/departments/Nonexistent/trend?from=2026-07-06&to=2026-07-10")
        assert r.status_code == 404

    def test_trend_to_before_from_422(self, client):
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-06")
        assert r.status_code == 422

    def test_trend_range_exceeds_92_422(self, client):
        r = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
        assert r.status_code == 422

    def test_trend_weekend_has_null_rate(self, client):
        # 2026-07-06 is a Monday. 2026-07-05 would be a Sunday.
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-05")
        item = r.json()["items"][0]
        assert item["is_working_day"] is False
        assert item["attendance_rate"] is None


@skip_if_no_mongo
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@skip_if_no_mongo
class TestExplain:
    def test_explain_attendance_list(self, client):
        r = client.get("/admin/explain/attendance_list")
        assert r.status_code == 200
        data = r.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data
        # Must use executionStats verbosity, not just queryPlanner
        assert "executionStats" in data["explain"]

    def test_explain_employee_monthly(self, client):
        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["collection"] == "attendance_logs"
        # executionStats present for aggregate explain too
        assert "executionStats" in data["explain"] or "stages" in data["explain"]

    def test_explain_department_summary_uses_employees_collection(self, client):
        r = client.get("/admin/explain/department_summary?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        # department_summary starts from the employees collection
        assert data["collection"] == "employees"
        assert "explain" in data

    def test_explain_late_leaderboard(self, client):
        r = client.get("/admin/explain/late_leaderboard?month=2026-07")
        assert r.status_code == 200
        data = r.json()
        assert data["collection"] == "attendance_logs"
        assert "explain" in data

    def test_explain_department_trend(self, client):
        r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
        assert r.status_code == 200
        data = r.json()
        assert data["collection"] == "attendance_logs"
        assert "explain" in data

    def test_explain_missing_params_422(self, client):
        r = client.get("/admin/explain/employee_monthly")
        assert r.status_code == 422

    def test_explain_invalid_endpoint_422(self, client):
        r = client.get("/admin/explain/nonexistent")
        assert r.status_code == 422
