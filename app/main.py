"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, date as date_type, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---- config and client --------------------------------------------------- #
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# ---- constants ------------------------------------------------------------ #
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

# Strict int that rejects floats, strings and bools
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]

# ---- time helpers --------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)

def dt_to_ms(dt: datetime) -> int:
    """UTC-aware datetime -> epoch milliseconds."""
    return int(dt.timestamp() * 1000)

def now_dt() -> datetime:
    """Current UTC time, truncated to whole seconds."""
    return ms_to_dt(int(datetime.now(timezone.utc).timestamp() * 1000))

def to_ist(dt: datetime) -> datetime:
    """Convert aware datetime to IST."""
    return dt.astimezone(IST)

# ---- date and shift helpers ----------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """IST calendar date of the record (R1).
    Overnight exception: punch-in IST time earlier than shift_end -> previous day."""
    ist = to_ist(punch_in)
    d = ist.date()
    if shift_end <= shift_start:  # overnight shift
        eh, em = map(int, shift_end.split(":"))
        punch_secs = ist.hour * 3600 + ist.minute * 60 + ist.second
        if punch_secs < eh * 3600 + em * 60:
            d -= timedelta(days=1)
    return d.isoformat()

def build_shift_times(date_str: str, shift_start: str, shift_end: str):
    """Build IST-aware shift_start and shift_end datetimes for a date."""
    d = date_type.fromisoformat(date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end_d = d + timedelta(days=1) if shift_end <= shift_start else d
    end = datetime(end_d.year, end_d.month, end_d.day, eh, em, tzinfo=IST)
    return start, end

def parse_date(s: str) -> date_type:
    """Validate and return date object, or raise 422."""
    try:
        return date_type.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(422, f"Invalid date format: {s}")

def month_bounds(month_str: str):
    """Return (first_day_str, last_day_str, first_date, last_date) for YYYY-MM."""
    try:
        y, m = map(int, month_str.split("-"))
        last_day = calendar.monthrange(y, m)[1]
        d1 = date_type(y, m, 1)
        d2 = date_type(y, m, last_day)
        return d1.isoformat(), d2.isoformat(), d1, d2
    except Exception:
        raise HTTPException(422, f"Invalid month format: {month_str}")

def build_attendance_list(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    filt: dict = {}
    if emp_code:
        filt["emp_code"] = emp_code
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, f"Invalid status: {status}")
        filt["status"] = status
    if date_from or date_to:
        d_range: dict = {}
        if date_from:
            parse_date(date_from)
            d_range["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            d_range["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        filt["date"] = d_range
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    return filt, sort, skip, page_size

# ---- business rule helpers ------------------------------------------------ #
def calc_late(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly > 600s after shift_start, then floor(s/60)."""
    secs = (punch_in - shift_start_dt).total_seconds()
    return int(secs // 60) if secs > 600 else 0

def calc_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes after shift_end, only if >= 30."""
    mins = int((punch_out - shift_end_dt).total_seconds() // 60)
    return mins if mins >= 30 else 0

def round_half_up(value, places: int = 2) -> float:
    """Round half-up. Never use Python round() for reported numbers."""
    q = Decimal(10) ** -places
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: Decimal(seconds) / Decimal(3600), half-up to 2 decimals."""
    secs = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(secs) / Decimal(3600), 2)

def is_half_day(work_hours: float) -> bool:
    """R5: true when rounded work_hours < 4.50."""
    return work_hours < 4.50

# ---- Pydantic models ----------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_date(cls, v):
        try:
            date_type.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError("must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v not in PRESENT_STATUSES:
            raise ValueError(f"must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"must be PRESENT, WFH, ON_DUTY, ABSENT or LEAVE")
        return v

# ---- serializers (Mongo doc -> API JSON) ---------------------------------- #
def serialize_emp(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }

def serialize_att(doc: dict) -> dict:
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pi) if pi else None,
        "punch_out": dt_to_ms(po) if po else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_ser_hist(h) for h in doc.get("history", [])],
    }

def _ser_hist(h: dict) -> dict:
    changes = {}
    for fld, ch in h["changes"].items():
        f, t = ch["from"], ch["to"]
        if fld in ("punch_in", "punch_out"):
            f = dt_to_ms(f) if f else None
            t = dt_to_ms(t) if t else None
        changes[fld] = {"from": f, "to": t}
    return {"at": dt_to_ms(h["at"]), "by": h["by"],
            "reason": h["reason"], "changes": changes}

# ---- index setup (lifespan) ---------------------------------------------- #
@asynccontextmanager
async def lifespan(app_instance):
    # employees indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index("joined_on")
    # attendance_logs indexes
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    yield

app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# ---- endpoints ----------------------------------------------------------- #

# -- health --
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}

# -- employees --
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee {body.emp_code} already exists")
    return serialize_emp(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt: dict = {}
    if department is not None:
        filt["department"] = department
    total = db.employees.count_documents(filt)
    skip = (page - 1) * page_size
    docs = db.employees.find(filt).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [serialize_emp(d) for d in docs],
            "total": total, "page": page, "page_size": page_size}

# -- punch-in --
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code} not found")

    punch_dt = ms_to_dt(body.punched_at) if body.punched_at else now_dt()
    date_str = attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])
    shift_s, shift_e = build_shift_times(date_str, emp["shift_start"], emp["shift_end"])

    late = calc_late(punch_dt, shift_s)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
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
        raise HTTPException(409, f"Already punched in for {date_str}")
    return serialize_att(doc)

# -- punch-out --
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code} not found")

    punch_dt = ms_to_dt(body.punched_at) if body.punched_at else now_dt()

    # Find latest record with punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "No punch-in record found")

    # Already punched out? -> 409
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    pi = rec["punch_in"]
    # Validate: punch_out must be after punch_in and within 24 hours
    diff = (punch_dt - pi).total_seconds()
    if diff <= 0:
        raise HTTPException(422, "punch_out must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    shift_s, shift_e = build_shift_times(rec["date"], emp["shift_start"], emp["shift_end"])
    wh = calc_work_hours(pi, punch_dt)
    ot = calc_overtime(punch_dt, shift_e)
    hd = is_half_day(wh)

    # Atomic update: only if punch_out is still null
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_dt,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "Already punched out")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_att(updated)

# -- list attendance --
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt, sort, skip, limit = build_attendance_list(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = db.attendance_logs.count_documents(filt)
    docs = db.attendance_logs.find(filt).sort(sort).skip(skip).limit(limit)
    return {"items": [serialize_att(d) for d in docs],
            "total": total, "page": page, "page_size": page_size}

# -- regularize (PATCH) --
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code} not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, f"No record for {emp_code} on {date}")

    sent = body.model_fields_set
    new_status = body.status if "status" in sent else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            raise HTTPException(422, f"Cannot set punch times for {new_status}")
        new_in = None
        new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent and body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent and body.punch_out is not None else rec.get("punch_out")

        if new_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        if "punch_in" in sent:
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
                raise HTTPException(422, f"punch_in must be on attendance date {rec['date']}")

        if new_out is not None:
            diff = (new_out - new_in).total_seconds()
            if diff <= 0 or diff > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    shift_s, shift_e = build_shift_times(rec["date"], emp["shift_start"], emp["shift_end"])
    if new_status in ("ABSENT", "LEAVE"):
        new_wh, new_late, new_ot, new_hd = None, 0, 0, False
    else:
        new_late = calc_late(new_in, shift_s)
        if new_out is not None:
            new_wh = calc_work_hours(new_in, new_out)
            new_ot = calc_overtime(new_out, shift_e)
            new_hd = is_half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    old_vals = {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    new_vals = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    changes = {}
    for fld in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old_vals[fld] != new_vals[fld]:
            changes[fld] = {"from": old_vals[fld], "to": new_vals[fld]}

    if not changes:
        raise HTTPException(422, "No fields changed")

    entry = {
        "at": now_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    guard = {"history": {"$size": len(rec["history"])}} if "history" in rec else {"history": {"$exists": False}}
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], **guard},
        {"$set": new_vals, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "Concurrent edit detected, please retry")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_att(updated)
