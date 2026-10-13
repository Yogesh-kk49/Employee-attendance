"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)

All code lives in this one file as required by the assignment.
MongoDB 6.0+ is required.
"""
import calendar
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

logger = logging.getLogger("attendance_api")
logging.basicConfig(level=logging.INFO)

# Database configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
INDEXES_READY = False


def _create_indexes(max_retries: int = 3, delay: float = 1.0) -> bool:
    """
    Create required indexes idempotently at application startup with bounded retries.
    Logs errors and prevents silent failures so uniqueness guarantees are maintained.
    """
    global INDEXES_READY
    for attempt in range(1, max_retries + 1):
        try:
            # Enforce uniqueness on emp_code to handle concurrent employee creations
            db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
            # Indexes for joined_on and compound department filtering in analytics
            db.employees.create_index([("joined_on", ASCENDING)], name="joined_on_asc")
            db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")

            # Enforce composite natural key uniqueness to handle concurrent punch-in attempts
            db.attendance_logs.create_index(
                [("emp_code", ASCENDING), ("date", ASCENDING)],
                unique=True,
                name="emp_date_unique",
            )
            # Optimized index for paginated attendance queries sorted by date desc, emp_code asc
            db.attendance_logs.create_index(
                [("date", DESCENDING), ("emp_code", ASCENDING)],
                name="date_desc_emp_asc",
            )
            # Date index for range-filtered analytics queries
            db.attendance_logs.create_index([("date", ASCENDING)], name="date_asc")

            INDEXES_READY = True
            logger.info("MongoDB indexes successfully created/verified")
            return True
        except PyMongoError as exc:
            logger.warning("Attempt %d/%d to create MongoDB indexes failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(delay)
        except Exception as exc:
            logger.error("Unexpected error creating MongoDB indexes: %s", exc)
            break

    INDEXES_READY = False
    logger.error("Failed to initialize required MongoDB indexes after %d attempts", max_retries)
    return False


@asynccontextmanager
async def lifespan(application: FastAPI):
    """FastAPI lifespan context manager for startup tasks."""
    _create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# Timestamp boundary constants for validation
EPOCH_MIN = 100_000_000_000   # Minimum valid epoch milliseconds (avoids seconds-epoch values)
EPOCH_MAX = 4_102_444_800_000  # Year 2100 upper bound

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# Calculation and validation helpers

def parse_date(d_str: str) -> date:
    """Validate and parse a YYYY-MM-DD date string. Raises HTTP 422 for invalid format or non-existent calendar dates."""
    if not isinstance(d_str, str) or not DATE_REGEX.match(d_str):
        raise HTTPException(422, f"Invalid date format: {d_str!r}, expected YYYY-MM-DD")
    try:
        return date.fromisoformat(d_str)
    except (ValueError, TypeError):
        raise HTTPException(422, f"Invalid calendar date: {d_str!r}")


def parse_month(m_str: str) -> tuple[int, int]:
    """Validate and parse a YYYY-MM month string. Raises HTTP 422 for invalid format or calendar months."""
    if not isinstance(m_str, str) or not MONTH_REGEX.match(m_str):
        raise HTTPException(422, f"Invalid month format: {m_str!r}, expected YYYY-MM")
    try:
        dt = datetime.strptime(m_str, "%Y-%m")
        return dt.year, dt.month
    except (ValueError, TypeError):
        raise HTTPException(422, f"Invalid calendar month: {m_str!r}")


def half_up(value: float, decimals: int = 2) -> float:
    """Round to `decimals` places using half-up (not banker's) rounding."""
    fmt = "0." + "0" * decimals
    return float(Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime to epoch milliseconds. Returns None if dt is None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # PyMongo returns naive datetimes by default; assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: determine the attendance date (IST calendar date) for a punch-in.

    Overnight shift (shift_end <= shift_start, e.g. 22:00-06:00):
      if the IST time of punch_in is *earlier than* shift_end
      it belongs to the shift that started the previous IST day.
    """
    ist_punch = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        punch_minutes = ist_punch.hour * 60 + ist_punch.minute
        end_minutes = eh * 60 + em
        if punch_minutes < end_minutes:
            # belongs to previous day's shift
            return (ist_punch - timedelta(days=1)).date().isoformat()

    return ist_punch.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: grace period is 10 minutes.
    Late only if strictly more than 10 minutes after shift_start.
    Returns whole minutes (floor) since shift_start, not since end of grace period.
    """
    h, m = map(int, shift_start.split(":"))
    # Build the shift-start instant on the attendance date in IST
    start_ist = datetime.fromisoformat(att_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    delta_sec = int((punch_in.astimezone(timezone.utc) - start_ist.astimezone(timezone.utc)).total_seconds())
    if delta_sec <= 600:  # <= 10 minutes (including exactly 10:00)
        return 0
    return delta_sec // 60  # whole-minute floor


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: work hours: seconds / 3600, rounded half-up to 2 decimals using Decimal arithmetic."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, att_date: str, is_overnight: bool) -> int:
    """
    R3: overtime = whole minutes (floor) between shift_end and punch_out,
    only if that is >= 30 minutes; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    if is_overnight:
        # shift_end is on the day after att_date
        end_date = (datetime.fromisoformat(att_date) + timedelta(days=1)).date().isoformat()
    else:
        end_date = att_date
    end_ist = datetime.fromisoformat(end_date).replace(
        hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST
    )
    delta_sec = int((punch_out.astimezone(timezone.utc) - end_ist.astimezone(timezone.utc)).total_seconds())
    if delta_sec < 1800:  # < 30 minutes
        return 0
    return delta_sec // 60


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh * 60 + em) <= (sh * 60 + sm)


def recompute_derived(punch_in: Optional[datetime], punch_out: Optional[datetime],
                      status: str, shift_start: str, shift_end: str, att_date: str) -> dict:
    """Recompute all four derived fields from current state."""
    overnight = is_overnight_shift(shift_start, shift_end)
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    late = compute_late_minutes(punch_in, shift_start, att_date)

    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime_minutes(punch_out, shift_end, att_date, overnight)
    hd = wh < 4.50  # R5: rounded work_hours < 4.50
    return {"late_minutes": late, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}


def fmt_record(doc: dict) -> dict:
    """
    Format an attendance_log document for API output:
    - convert BSON datetimes to epoch milliseconds
    - do the same inside history[].changes
    - strip _id
    """
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])

    for entry in doc["history"]:
        entry["at"] = dt_to_ms(entry.get("at"))
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = dt_to_ms(changes[field].get("from"))
                changes[field]["to"] = dt_to_ms(changes[field].get("to"))

    return doc


def fmt_employee(doc: dict) -> dict:
    """Format an employee document: strip _id, convert created_at to epoch-ms."""
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return doc


# Request and response schemas

def validate_epoch_ms(v):
    """Reject values that look like seconds, or are out of reasonable range."""
    if v is None:
        return v
    if not isinstance(v, int):
        raise ValueError("must be an integer (epoch milliseconds)")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(
            f"epoch milliseconds must be between {EPOCH_MIN} and {EPOCH_MAX}; "
            "did you pass seconds instead of milliseconds?"
        )
    return v


EpochMs = Annotated[int, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]
EpochMsOpt = Annotated[Optional[int], Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str):
        if not isinstance(v, str) or not DATE_REGEX.match(v):
            raise ValueError(f"joined_on must be YYYY-MM-DD: {v!r}")
        try:
            date.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError(f"joined_on must be a valid calendar date: {v!r}")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: EpochMsOpt = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: EpochMsOpt = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: EpochMsOpt = None
    punch_out: EpochMsOpt = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def check_no_explicit_nulls(cls, data):
        if isinstance(data, dict):
            if "punch_in" in data and data["punch_in"] is None:
                raise ValueError("punch_in cannot be null; omit field to leave unchanged")
            if "punch_out" in data and data["punch_out"] is None:
                raise ValueError("punch_out cannot be null; omit field to leave unchanged")
        return data

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ALL_STATUSES if v is not None else False:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @model_validator(mode="after")
    def absent_leave_rules(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "punch_in and punch_out must be omitted when setting status ABSENT or LEAVE"
                )
        return self


# System endpoints

@app.get("/health")
def health():
    """R: Returns 200 only if MongoDB answers a ping and required indexes are ready, otherwise 503."""
    try:
        client.admin.command("ping")
    except Exception as exc:
        logger.error("MongoDB ping failed: %s", exc)
        raise HTTPException(503, "MongoDB is unavailable")

    if not INDEXES_READY:
        if not _create_indexes(max_retries=1, delay=0):
            raise HTTPException(503, "Database indexes not initialized")

    return {"status": "ok"}


# Employee management endpoints

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee. emp_code uniqueness enforced by DB index (race-safe)."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return fmt_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees, sorted by emp_code ascending. total reflects filtered set."""
    q: dict = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for doc in items:
        doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# Attendance tracking endpoints

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Atomically safe via unique (emp_code, date) index.
    R1: date determined in IST, truncated to whole seconds.
    R2: late_minutes computed.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    now_ms = body.punched_at if body.punched_at is not None else dt_to_ms(datetime.now(tz=timezone.utc))
    punch_dt = ms_to_dt(now_ms)  # UTC-aware, truncated to whole seconds

    att_date = attendance_date_for(punch_dt, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee {body.emp_code!r} already punched in for {att_date}")

    return fmt_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Close the employee's most recent open record.
    R3: overtime_minutes, R4: work_hours, R5: half_day.
    Atomic update via findOneAndUpdate; concurrent punch-outs yield one 200 and rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    now_ms = body.punched_at if body.punched_at is not None else dt_to_ms(datetime.now(tz=timezone.utc))
    punch_out_dt = ms_to_dt(now_ms)

    # Find the most recent open record whose punch_in <= punch_out_dt
    open_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not open_record:
        # Check if an open record exists with punch_in > punch_out_dt (client attempted punch-out before punch-in)
        future_open = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": punch_out_dt},
                "punch_out": None,
            },
            sort=[("punch_in", ASCENDING)],
        )
        if future_open:
            raise HTTPException(422, "punched_at must be after punch_in")

        # Check if a closed record exists with punch_in <= punch_out_dt (already punched out)
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": punch_out_dt},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(409, "Record is already punched out")

        raise HTTPException(404, "No open punch-in record found for this employee")

    punch_in_dt = open_record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422: punch_out must be strictly after punch_in and within 24 hours
    delta = punch_out_dt - punch_in_dt
    if delta.total_seconds() <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if delta.total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    att_date = open_record["date"]
    overnight = is_overnight_shift(emp["shift_start"], emp["shift_end"])
    wh = compute_work_hours(punch_in_dt, punch_out_dt)
    ot = compute_overtime_minutes(punch_out_dt, emp["shift_end"], att_date, overnight)
    hd = wh < 4.50

    # Atomic update: only succeed if punch_out is still None
    result = db.attendance_logs.find_one_and_update(
        {"_id": open_record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_out_dt, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record was already punched out by a concurrent request")

    return fmt_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records.
    Sorted by date descending, emp_code ascending.
    DB-side sort + skip + limit — does not load collection into memory.
    """
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be later than date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, f"Invalid status: {status!r}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [fmt_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Manually correct one day's attendance record.
    Appends one history entry. Recomputes derived fields.
    Atomic: uses optimistic concurrency on history array length to prevent lost updates.
    """
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, f"No attendance record found for {emp_code} on {date}")

    # Resolve final field values (apply patch on top of existing)
    new_status = body.status if body.status is not None else record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    if body.punch_in is not None:
        new_punch_in = ms_to_dt(body.punch_in)
    else:
        new_punch_in = old_punch_in
        if new_punch_in and new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

    if body.punch_out is not None:
        new_punch_out = ms_to_dt(body.punch_out)
    else:
        new_punch_out = old_punch_out
        if new_punch_out and new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

    # ABSENT/LEAVE: clear punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None

    # Presence status requires a punch_in
    if new_status in PRESENCE_STATUSES and new_punch_in is None:
        raise HTTPException(422, "A presence status requires a punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        pi_ist = new_punch_in.astimezone(IST)
        pi_date = attendance_date_for(new_punch_in, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, f"punch_in would move the attendance date to {pi_date}, must stay {date}")

    # punch_out must be after punch_in and within 24 hours
    if new_punch_in is not None and new_punch_out is not None:
        delta = new_punch_out - new_punch_in
        if delta.total_seconds() <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if delta.total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    overnight = is_overnight_shift(emp["shift_start"], emp["shift_end"])
    derived = recompute_derived(new_punch_in, new_punch_out, new_status, emp["shift_start"], emp["shift_end"], date)

    # Build changes dict — only fields that actually changed
    def ms_or_none(dt):
        return dt_to_ms(dt) if dt is not None else None

    old_wh = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    changes = {}
    old_pi_ms = ms_or_none(old_punch_in)
    new_pi_ms = ms_or_none(new_punch_in)
    old_po_ms = ms_or_none(old_punch_out)
    new_po_ms = ms_or_none(new_punch_out)

    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}
    if old_pi_ms != new_pi_ms:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}  # store as BSON dates
    if old_po_ms != new_po_ms:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if old_wh != derived["work_hours"]:
        changes["work_hours"] = {"from": old_wh, "to": derived["work_hours"]}
    if old_late != derived["late_minutes"]:
        changes["late_minutes"] = {"from": old_late, "to": derived["late_minutes"]}
    if old_ot != derived["overtime_minutes"]:
        changes["overtime_minutes"] = {"from": old_ot, "to": derived["overtime_minutes"]}
    if old_hd != derived["half_day"]:
        changes["half_day"] = {"from": old_hd, "to": derived["half_day"]}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update: condition on current field values and history length to detect concurrent edits / punch-outs
    current_history_len = len(record.get("history") or [])
    update_result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "punch_in": old_punch_in,
            "punch_out": old_punch_out,
            "status": record["status"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if update_result is None:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return fmt_record(update_result)


# Analytics helper functions

def month_date_range(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM month string."""
    import calendar
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    first = f"{month}-01"
    last = f"{month}-{last_day:02d}"
    return first, last


def working_days_in_range(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days between start_str and end_str inclusive."""
    start = datetime.fromisoformat(start_str).date()
    end = datetime.fromisoformat(end_str).date()
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        current += timedelta(days=1)
    return count


# Analytics endpoints

def build_employee_monthly_pipeline(emp_code: str, first_day: str, last_day: str) -> list:
    """Build aggregation pipeline for monthly employee attendance summary."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                # present_days: PRESENT/WFH/ON_DUTY on Mon-Fri; half_day counts 0.5
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee.
    R7: working_days from joined_on (if mid-month) or month start.
    All presence metrics computed entirely inside the MongoDB aggregation pipeline.
    """
    parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    first_day, last_day = month_date_range(month)

    # R7: if employee joined mid-month, count working days from joined_on
    eff_start = max(first_day, emp["joined_on"]) if emp["joined_on"] > first_day else first_day
    working_days = working_days_in_range(eff_start, last_day)

    pipeline = build_employee_monthly_pipeline(emp_code, first_day, last_day)
    rows = list(db.attendance_logs.aggregate(pipeline))

    if not rows:
        agg = {
            "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0,
            "present_days": 0.0,
        }
    else:
        agg = rows[0]

    present_days = float(agg["present_days"])
    attendance_pct = (
        half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present_days, 2),
        "leave_days": agg["leave_days"],
        "late_count": agg["late_count"],
        "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(first_day: str, last_day: str, department: Optional[str] = None) -> list:
    """Build single aggregation pipeline combining employees and attendance_logs via $lookup."""
    match_stage: dict = {"joined_on": {"$lte": last_day}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.date", None]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 5]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month.
    Single aggregation pipeline combining employees and attendance_logs via $lookup.
    Headcount: employees with joined_on <= last_day_of_month (R9).
    Employees with zero logs still count toward headcount.
    """
    parse_month(month)
    first_day, last_day = month_date_range(month)
    pipeline = build_department_summary_pipeline(first_day, last_day, department)
    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = half_up(d["avg_work_hours"], 2) if d["avg_work_hours"] is not None else None
        items.append({
            "department": d["department"],
            "headcount": d["headcount"],
            "present_days": half_up(d["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(first_day: str, last_day: str, limit: int, department: Optional[str] = None) -> list:
    """Build aggregation pipeline for late leaderboard with DB-side $setWindowFields ranking and filtering."""
    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard. Standard competition ranking (1, 2, 2, 4) computed via $setWindowFields in MongoDB.
    Returns all employees whose rank <= limit (ties at the cutoff are included).
    """
    parse_month(month)
    first_day, last_day = month_date_range(month)
    pipeline = build_late_leaderboard_pipeline(first_day, last_day, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_: str, to: str, from_dt: datetime, to_dt: datetime) -> list:
    """Build aggregation pipeline for daily attendance trend with $densify, $lookup, and $setWindowFields."""
    return [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with a single document from employees to guarantee densify always has input even if logs are empty
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ]
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department.
    One row per calendar day (gap-filled via $densify in MongoDB).
    R9 headcount per day. moving_avg_7d via $setWindowFields in MongoDB.
    """
    parse_date(from_)
    parse_date(to)
    if to < from_:
        raise HTTPException(422, "to must not be before from")

    start_date = date.fromisoformat(from_)
    end_date = date.fromisoformat(to)
    if (end_date - start_date).days > 91:  # > 92 days means > 91 difference
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, f"Department {department!r} not found")

    from_dt = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
    to_dt = (datetime.fromisoformat(to) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = build_department_trend_pipeline(department, from_, to, from_dt, to_dt)
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in docs:
        att_rate = half_up(doc["attendance_rate"], 4) if doc.get("attendance_rate") is not None else None
        mov_avg = half_up(doc["moving_avg_7d"], 4) if doc.get("moving_avg_7d") is not None else None
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": half_up(doc["present_count"], 2),
            "late_count": int(doc["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {"department": department, "items": items}


# Admin and query analysis endpoints

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Return MongoDB explain(executionStats) for the main query of the named endpoint.
    Mirrors the exact filter/sort/pipeline that the endpoint uses.
    """
    valid_endpoints = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid_endpoints)}")

    collection_name = "attendance_logs"
    explain_result = None

    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be later than date_to")
        if status and status not in ALL_STATUSES:
            raise HTTPException(422, f"Invalid status: {status!r}")
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": skip,
            "limit": page_size,
        }
        explain_result = db.command("explain", find_cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        parse_month(month)
        first_day, last_day = month_date_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, first_day, last_day)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        parse_month(month)
        first_day, last_day = month_date_range(month)
        pipeline = build_department_summary_pipeline(first_day, last_day, department)
        collection_name = "employees"
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        parse_month(month)
        first_day, last_day = month_date_range(month)
        pipeline = build_late_leaderboard_pipeline(first_day, last_day, limit, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        parse_date(from_)
        parse_date(to)
        if to < from_:
            raise HTTPException(422, "to must not be before from")
        if (date.fromisoformat(to) - date.fromisoformat(from_)).days > 91:
            raise HTTPException(422, "Date range must not exceed 92 days")
        from_dt = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
        to_dt = (datetime.fromisoformat(to) + timedelta(days=1)).replace(tzinfo=timezone.utc)
        pipeline = build_department_trend_pipeline(department, from_, to, from_dt, to_dt)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", cmd, verbosity="executionStats")

    # Convert ObjectId / non-serializable types to strings for JSON output
    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sanitize(i) for i in obj]
        try:
            import json
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": sanitize(explain_result),
    }
