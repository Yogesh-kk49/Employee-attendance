"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)

All code lives in this one file as required by the assignment.
MongoDB 6.0+ is required.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Database configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def _create_indexes():
    """Create required indexes idempotently at application startup."""
    # Enforce uniqueness on emp_code to handle concurrent employee creations
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")

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


# Calculation helpers

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
    """R4: work hours, rounded half-up to 2 decimal places."""
    seconds = int((punch_out - punch_in).total_seconds())
    raw = seconds / 3600
    return half_up(raw, 2)


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

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
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
    """R: Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")
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

    # Find the most recent open record
    open_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not open_record:
        # Check if a closed record exists (already punched out) to give 409 vs 404
        any_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if any_record and any_record.get("punch_out") is not None:
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

    # Atomic update: condition on current history length to detect concurrent edits
    current_history_len = len(record.get("history") or [])
    update_result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            # Optimistic concurrency: only apply if history hasn't changed since we read it
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

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee.
    R7: working_days from joined_on (if mid-month) or month start.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    first_day, last_day = month_date_range(month)

    # R7: if employee joined mid-month, count working days from joined_on
    eff_start = max(first_day, emp["joined_on"]) if emp["joined_on"] > first_day else first_day
    working_days = working_days_in_range(eff_start, last_day)

    # Aggregation: read stored derived values (do not recompute)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                # present_days: PRESENT/WFH/ON_DUTY on Mon-Fri; half_day counts 0.5
                # We compute per-record presence contribution
                "records": {
                    "$push": {
                        "status": "$status",
                        "date": "$date",
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    if not rows:
        agg = {
            "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0, "records": []
        }
    else:
        agg = rows[0]

    # Compute present_days: only Mon-Fri, half_day = 0.5
    present_days = 0.0
    for r in agg["records"]:
        if r["status"] not in PRESENCE_STATUSES:
            continue
        d = datetime.fromisoformat(r["date"]).date()
        if d.weekday() >= 5:  # weekend – R7 says Mon-Fri only for present_days
            continue
        present_days += 0.5 if r["half_day"] else 1.0

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


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month.
    Headcount: employees with joined_on <= last_day_of_month (R9).
    Employees with zero logs still count toward headcount.
    """
    first_day, last_day = month_date_range(month)

    # Step 1: get headcount per department (employees joined on or before last_day)
    emp_q: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_q["department"] = department

    emp_pipeline = [
        {"$match": emp_q},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
    ]
    dept_map = {r["_id"]: r for r in db.employees.aggregate(emp_pipeline)}

    if not dept_map:
        return {"month": month, "items": []}

    # Step 2: aggregate attendance_logs for the month, grouped by dept (via lookup)
    log_pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        # Lookup employee to get department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        # Filter dept if requested
        *(
            [{"$match": {"emp.department": department}}]
            if department
            else []
        ),
        {
            "$group": {
                "_id": "$emp.department",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                # avg_work_hours: only presence records with work_hours != null
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                # present_days (Mon-Fri only)
                "present_records": {
                    "$push": {
                        "status": "$status",
                        "date": "$date",
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            }
        },
    ]

    log_results = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept_name in sorted(dept_map.keys()):
        dm = dept_map[dept_name]
        lr = log_results.get(dept_name, {})

        headcount = dm["headcount"]

        # present_days
        present_days = 0.0
        for r in lr.get("present_records", []):
            if r["status"] not in PRESENCE_STATUSES:
                continue
            d = datetime.fromisoformat(r["date"]).date()
            if d.weekday() >= 5:
                continue
            present_days += 0.5 if r["half_day"] else 1.0

        wh_count = lr.get("work_hours_count", 0)
        avg_wh = (
            half_up(lr["work_hours_sum"] / wh_count, 2) if wh_count > 0 else None
        )

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": half_up(present_days, 2),
            "avg_work_hours": avg_wh,
            "late_count": lr.get("late_count", 0),
            "total_late_minutes": lr.get("total_late_minutes", 0),
            "leave_count": lr.get("leave_count", 0),
            "on_duty_count": lr.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard. Standard competition ranking (1, 2, 2, 4).
    Returns all employees whose rank <= limit (ties at the cutoff are included).
    """
    first_day, last_day = month_date_range(month)

    # Build pipeline
    match_stage: dict = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join employee info
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        # Filter by department if given (ranking inside department only)
        *(
            [{"$match": {"emp.department": department}}]
            if department
            else []
        ),
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Compute standard competition ranking in Python (tied scores share rank)
    items = []
    rank = 0
    prev_minutes = None
    skipped = 0
    for i, row in enumerate(rows):
        if row["total_late_minutes"] != prev_minutes:
            rank = i + 1  # standard competition rank
            prev_minutes = row["total_late_minutes"]
        if rank > limit:
            break
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department.
    One row per calendar day (gap-filled via $densify).
    R9 headcount per day. moving_avg_7d over non-null rates in window.
    """
    if to < from_:
        raise HTTPException(422, "to must not be before from")

    start_date = datetime.fromisoformat(from_).date()
    end_date = datetime.fromisoformat(to).date()
    if (end_date - start_date).days > 91:  # > 92 days means > 91 difference
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, f"Department {department!r} not found")

    # Generate full sequence of calendar days in range to ensure all dates are present
    all_dates = []
    cur = start_date
    while cur <= end_date:
        all_dates.append(cur.isoformat())
        cur += timedelta(days=1)

    # Get all employees in this department with their joined_on
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    # Aggregate attendance logs for this department + date range
    log_pipeline = [
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
                "records": {
                    "$push": {
                        "status": "$status",
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            }
        },
    ]

    log_by_date = {r["_id"]: r["records"] for r in db.attendance_logs.aggregate(log_pipeline)}

    # Build result rows
    raw_items = []
    for d_str in all_dates:
        d = datetime.fromisoformat(d_str).date()
        is_working = d.weekday() < 5

        # headcount: employees joined on or before this day
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= d_str)

        records = log_by_date.get(d_str, [])

        present_count = 0.0
        late_count = 0
        for r in records:
            if r["status"] in PRESENCE_STATUSES:
                present_count += 0.5 if r["half_day"] else 1.0
            if r["late_minutes"] > 0:
                late_count += 1

        # attendance_rate: only on working days with headcount > 0
        if is_working and headcount > 0:
            attendance_rate = half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        raw_items.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": half_up(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,  # filled below
        })

    # Compute moving_avg_7d: mean of non-null rates in a window of up to 7 rows
    # (current + up to 6 preceding) within the requested range only
    for i, row in enumerate(raw_items):
        window = raw_items[max(0, i - 6): i + 1]
        non_null_rates = [r["attendance_rate"] for r in window if r["attendance_rate"] is not None]
        if non_null_rates:
            row["moving_avg_7d"] = half_up(sum(non_null_rates) / len(non_null_rates), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": raw_items}


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
        explain_result = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        first_day, last_day = month_date_range(month)
        q = {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}
        explain_result = db.attendance_logs.find(q).explain()

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        first_day, last_day = month_date_range(month)
        q = {"date": {"$gte": first_day, "$lte": last_day}}
        explain_result = db.attendance_logs.find(q).explain()

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        first_day, last_day = month_date_range(month)
        q = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}
        explain_result = db.attendance_logs.find(q).explain()

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        q = {"date": {"$gte": from_, "$lte": to}}
        explain_result = db.attendance_logs.find(q).explain()

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
