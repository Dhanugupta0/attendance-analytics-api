"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, date as date_type, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Annotated

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

half_up = round_half_up

def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, half-up to 2 decimals."""
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)

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

# ---- endpoints (Phase 4+) ------------------------------------------------ #
# TODO: health, employees, attendance, analytics, explain
