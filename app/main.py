"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import json
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
    return "attendance_logs", filt, sort, skip, page_size

def build_employee_monthly(emp_code: str, month: str):
    first_day, last_day, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "_present": {"$in": ["$status", list(PRESENT_STATUSES)]},
            "_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
            "_half_day": {"$ifNull": ["$half_day", False]},
            "_late": {"$ifNull": ["$late_minutes", 0]},
            "_ot": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "units": {"$sum": {
                "$cond": [{"$and": ["$_present", "$_weekday"]}, {"$cond": ["$_half_day", 1, 2]}, 0]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "total_overtime_minutes": {"$sum": "$_ot"},
        }},
    ]
    return "attendance_logs", pipeline

def build_department_summary(month: str, department: Optional[str] = None):
    first_day, last_day, _, _ = month_bounds(month)
    match_cond: dict = {"joined_on": {"$lte": last_day}}
    if department:
        match_cond["department"] = department
        hint = {"department": 1, "emp_code": 1}
    else:
        hint = {"joined_on": 1}

    pipeline = [
        {"$match": match_cond},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "units": {"$sum": {"$map": {
                "input": "$logs",
                "as": "l",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$l.status", list(PRESENT_STATUSES)]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                    ]},
                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 1, 2]},
                    0
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
            }}},
            "late_total": {"$sum": {"$map": {
                "input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}
            }}},
            "leave_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}
            }}},
            "on_duty_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
            }}},
            "wh_logs": {"$filter": {
                "input": "$logs", "as": "l", "cond": {"$and": [
                    {"$in": ["$$l.status", list(PRESENT_STATUSES)]},
                    {"$ne": ["$$l.work_hours", None]}
                ]}
            }},
        }},
        {"$addFields": {
            "wh_cents": {"$sum": {"$map": {
                "input": "$wh_logs", "as": "l",
                "in": {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}
            }}},
            "wh_count": {"$size": "$wh_logs"},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "units": {"$sum": "$units"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_total"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "wh_cents": {"$sum": "$wh_cents"},
            "wh_count": {"$sum": "$wh_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline, hint

def build_late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    first_day, last_day, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return "attendance_logs", pipeline

def build_department_trend(emp_codes: list, joined: list, from_str: str, to_str: str):
    d_from = parse_date(from_str)
    d_to = parse_date(to_str)
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)
    seed_dt = from_dt - timedelta(days=1)
    densify_end = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_str, "$lte": to_str}}},
        {"$group": {
            "_id": "$date",
            "ph": {"$sum": {
                "$cond": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]},
                    0
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$match": {"emp_code": emp_codes[0]}},
                {"$project": {"_id": 0, "day": seed_dt}},
            ],
        }},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [from_dt, densify_end]}}},
        {"$match": {"day": {"$gte": from_dt}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "ph": {"$ifNull": ["$ph", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}
            }}}
        }},
        {"$addFields": {
            "rate_ticks": {
                "$cond": [
                    {"$and": ["$is_working", {"$gt": ["$headcount", 0]}]},
                    {"$floor": {"$divide": [
                        {"$add": [{"$multiply": [10000, "$ph"]}, "$headcount"]},
                        {"$multiply": [2, "$headcount"]}
                    ]}},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "sum_ticks": {"$sum": "$rate_ticks", "window": {"documents": [-6, 0]}},
                "n_ticks": {"$sum": {"$cond": [{"$ne": ["$rate_ticks", None]}, 1, 0]}, "window": {"documents": [-6, 0]}},
            },
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working": 1, "headcount": 1, "ph": 1,
            "late_count": 1, "rate_ticks": 1, "sum_ticks": 1, "n_ticks": 1,
        }},
    ]
    return "attendance_logs", pipeline

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
    _, filt, sort, skip, limit = build_attendance_list(
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

# ---- analytics endpoints ------------------------------------------------- #

# 6a. employee monthly
@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code} not found")

    _, _, d1, d2 = month_bounds(month)
    coll, pipeline = build_employee_monthly(emp_code, month)
    docs = list(db[coll].aggregate(pipeline))

    if docs:
        d = docs[0]
        units, leave_days, late_count = d["units"], d["leave_days"], d["late_count"]
        total_late, total_ot = d["total_late_minutes"], d["total_overtime_minutes"]
    else:
        units = leave_days = late_count = total_late = total_ot = 0

    joined_d = parse_date(emp["joined_on"])
    if joined_d > d2:
        working_days = 0
    else:
        cur = max(d1, joined_d)
        working_days = 0
        while cur <= d2:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    present_days = float(units) / 2.0
    att_pct = round_half_up(Decimal(units) * Decimal(50) / Decimal(working_days), 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }

# 6b. department summary
@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    coll, pipeline, hint = build_department_summary(month, department)
    docs = list(db[coll].aggregate(pipeline, hint=hint))

    items = []
    for d in docs:
        wh_cnt = d["wh_count"]
        avg_wh = round_half_up(Decimal(str(int(d["wh_cents"]))) / (Decimal(wh_cnt) * Decimal(100)), 2) if wh_cnt > 0 else None
        items.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": float(d["units"]) / 2.0,
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"],
        })
    return {"month": month, "items": items}

# 6c. late leaderboard
@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    coll, pipeline = build_late_leaderboard(month, limit, department)
    items = list(db[coll].aggregate(pipeline))
    return {"month": month, "items": items}

# 6d. department trend
@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from = parse_date(from_date)
    d_to = parse_date(to_date)
    if d_to < d_from:
        raise HTTPException(422, "'to' date must be >= 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, f"Department '{department}' not found")

    emp_codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]

    coll, pipeline = build_department_trend(emp_codes, joined, from_date, to_date)
    docs = list(db[coll].aggregate(pipeline))

    items = []
    for r in docs:
        p_count = float(r["ph"]) / 2.0
        r_ticks = r["rate_ticks"]
        att_rate = float(Decimal(r_ticks) / Decimal(10000)) if r_ticks is not None else None

        n_ticks = r["n_ticks"]
        if not n_ticks:
            mov_avg = None
        else:
            avg_ticks = (2 * int(r["sum_ticks"]) + int(n_ticks)) // (2 * int(n_ticks))
            mov_avg = float(Decimal(avg_ticks) / Decimal(10000))

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working"]),
            "headcount": r["headcount"],
            "present_count": p_count,
            "late_count": r["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })
    return {"department": department, "items": items}

# ---- explain endpoint (Step 7) ------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll, filt, sort, skip, limit_val = build_attendance_list(
            emp_code=emp_code, date_from=date_from, date_to=date_to,
            status=status, page=page, page_size=page_size,
        )
        cmd = {"find": coll, "filter": filt, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": limit_val}
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        coll, pipeline = build_employee_monthly(emp_code, month)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        coll, pipeline, hint = build_department_summary(month, department)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        coll, pipeline = build_late_leaderboard(month, limit or 10, department)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        d_from = parse_date(from_date)
        d_to = parse_date(to_date)
        if d_to < d_from:
            raise HTTPException(422, "'to' date must be >= 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")
        emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        if not emps:
            raise HTTPException(404, f"Department '{department}' not found")
        emp_codes = [e["emp_code"] for e in emps]
        joined = [e["joined_on"] for e in emps]
        coll, pipeline = build_department_trend(emp_codes, joined, from_date, to_date)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    else:
        raise HTTPException(422, f"Unknown endpoint {endpoint}")

    raw_explain = db.command({"explain": cmd, "verbosity": "executionStats"})
    converted = json.loads(json_util.dumps(raw_explain, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": converted}
