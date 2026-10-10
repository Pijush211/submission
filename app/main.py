"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import Annotated, Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override real env vars -> real env wins

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, maxPoolSize=100, serverSelectionTimeoutMS=5000, retryWrites=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
PRESENT_LIST = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")

D01 = Decimal("0.01")
D0001 = Decimal("0.0001")
D3600 = Decimal(3600)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- #
# Helpers: time, rounding, validation
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # covers list sort (date desc, emp_code asc) AND date-range matches (index prefix)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index("status")


def truncate_ms(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_utc_dt(ms: int) -> datetime:
    t = truncate_ms(int(ms))
    return datetime.fromtimestamp(t / 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def now_utc_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@lru_cache(maxsize=64)
def _parse_hhmm_cached(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def parse_hhmm(s: str):
    return _parse_hhmm_cached(s)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date_for(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in, except overnight early-morning -> previous day."""
    if punch_utc.tzinfo is None:
        punch_utc = punch_utc.replace(tzinfo=UTC)
    ist = punch_utc.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        se_h, se_m = parse_hhmm(shift_end)
        se_secs = se_h * 3600 + se_m * 60
        ist_secs = ist.hour * 3600 + ist.minute * 60 + ist.second
        if ist_secs < se_secs:
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def shift_start_dt(attendance_date: str, shift_start: str) -> datetime:
    d = dt_date.fromisoformat(attendance_date)
    h, m = parse_hhmm(shift_start)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    d = dt_date.fromisoformat(attendance_date)
    h, m = parse_hhmm(shift_end)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=UTC)
    punch_ist = punch_in.astimezone(IST)
    start_ist = shift_start_dt(attendance_date, shift_start)
    diff = (punch_ist - start_ist).total_seconds()
    if diff <= 600:
        return 0
    return int(diff // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded 2 decimals half-up."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=UTC)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=UTC)
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / D3600).quantize(D01, rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3: floor minutes shift_end->punch_out, only if >=30."""
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=UTC)
    punch_ist = punch_out.astimezone(IST)
    end_ist = shift_end_dt(attendance_date, shift_start, shift_end)
    diff = (punch_ist - end_ist).total_seconds()
    if diff < 1800:
        return 0
    return int(diff // 60)


def round2_half_up(x: float) -> float:
    return float(Decimal(str(x)).quantize(D01, rounding=ROUND_HALF_UP))


def round4_half_up(x: float) -> float:
    return float(Decimal(str(x)).quantize(D0001, rounding=ROUND_HALF_UP))


def validate_date_str(s: str, field="date"):
    if not DATE_RE.match(s):
        raise HTTPException(422, f"invalid {field}")
    try:
        dt_date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid {field}")
    return s


def validate_month_str(s: str):
    if not MONTH_RE.match(s):
        raise HTTPException(422, "invalid month")
    return s


@lru_cache(maxsize=256)
def month_bounds(month: str):
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days_between(start_s: str, end_s: str) -> int:
    # O(1) arithmetic instead of day-by-day loop
    s = dt_date.fromisoformat(start_s)
    e = dt_date.fromisoformat(end_s)
    total = (e - s).days + 1
    if total <= 0:
        return 0
    full_weeks, extra = divmod(total, 7)
    n = full_weeks * 5
    for i in range(extra):
        if (s + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Serializers (Mongo -> API, epoch millis at boundary)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _hist_val_to_api(v: Any) -> Any:
    if isinstance(v, datetime):
        return dt_to_ms(v)
    return v


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    hist = doc.get("history") or []
    out_hist = []
    for h in hist:
        at = h.get("at")
        changes = h.get("changes", {}) or {}
        api_changes = {}
        for k, fv in changes.items():
            if isinstance(fv, dict):
                api_changes[k] = {"from": _hist_val_to_api(fv.get("from")), "to": _hist_val_to_api(fv.get("to"))}
            else:
                api_changes[k] = fv
        out_hist.append({"at": dt_to_ms(at) if isinstance(at, datetime) else at, "by": h.get("by"), "reason": h.get("reason"), "changes": api_changes})
    late = doc.get("late_minutes", 0)
    ot = doc.get("overtime_minutes", 0)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": 0 if late is None else late,
        "overtime_minutes": 0 if ot is None else ot,
        "half_day": bool(doc.get("half_day", False)),
        "history": out_hist,
    }


# --------------------------------------------------------------------------- #
# Models (strict validation; derived fields never accepted -> extra ignore)
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    shift_end: Annotated[str, Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "mongo unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not EMP_RE.match(body.emp_code):
        raise HTTPException(422, "invalid emp_code")
    if not EMAIL_RE.match(body.email):
        raise HTTPException(422, "invalid email")
    if not HHMM_RE.match(body.shift_start or "") or not HHMM_RE.match(body.shift_end or ""):
        raise HTTPException(422, "invalid shift time")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    validate_date_str(body.joined_on, "joined_on")
    if not (1 <= len(body.name) <= 100 and 1 <= len(body.department) <= 50):
        raise HTTPException(422, "invalid employee fields")
    doc = {"emp_code": body.emp_code, "name": body.name, "email": body.email, "department": body.department, "shift_start": body.shift_start, "shift_end": body.shift_end, "joined_on": body.joined_on, "created_at": now_utc_truncated()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_employee(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENT_STATUSES:
        raise HTTPException(422, "invalid status for punch-in")
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    punch_utc = ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    att_date = attendance_date_for(punch_utc, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_utc, emp["shift_start"], att_date)
    doc = {"emp_code": body.emp_code, "date": att_date, "status": body.status, "punch_in": punch_utc, "punch_out": None, "work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc.pop("_id", None)
    return serialize_attendance(doc)


# --------------------------------------------------------------------------- #
# Attendance: punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    punch_utc = ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    # most recent record with punch_in <= punched_at
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_utc}}, sort=[("punch_in", DESCENDING)])
    if rec is None or rec.get("punch_in") is None:
        raise HTTPException(404, "no punch-in found")
    punch_in = rec["punch_in"]
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=UTC)
    if punch_utc <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punch_utc - punch_in).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24h after punch_in")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    att_date = rec["date"]
    work = compute_work_hours(punch_in, punch_utc)
    ot = compute_overtime(punch_utc, emp["shift_start"], emp["shift_end"], att_date)
    half = work < 4.50
    updated = db.attendance_logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": punch_utc, "work_hours": work, "overtime_minutes": ot, "half_day": half}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from is not None:
        validate_date_str(date_from, "date_from")
    if date_to is not None:
        validate_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_attendance(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: regularize (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str = Path(...), date: str = Path(...), body: RegularizeIn = None):  # type: ignore
    validate_date_str(date, "date")
    if body is None:
        raise HTTPException(422, "invalid body")
    if body.status is not None and body.status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "cannot supply punch times with ABSENT/LEAVE")

    old_status = rec.get("status")
    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    old_work = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0) or 0
    old_ot = rec.get("overtime_minutes", 0) or 0
    old_half = bool(rec.get("half_day", False))
    old_hist: List[Any] = rec.get("history") or []

    new_status = body.status if body.status is not None else old_status
    # determine new punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        new_in = ms_to_utc_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_utc_dt(body.punch_out) if body.punch_out is not None else old_out
        # normalize naive -> UTC
        if isinstance(new_in, datetime) and new_in.tzinfo is None:
            new_in = new_in.replace(tzinfo=UTC)
        if isinstance(new_out, datetime) and new_out.tzinfo is None:
            new_out = new_out.replace(tzinfo=UTC)
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        # R1: punch_in must stay on record's attendance date
        calc_date = attendance_date_for(new_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24h of punch_in")

    # recompute derived R2-R5
    if new_status in ("ABSENT", "LEAVE"):
        new_in, new_out = None, None
        new_work = None
        new_late, new_ot, new_half = 0, 0, False
    else:
        assert isinstance(new_in, datetime)
        new_late = compute_late_minutes(new_in, emp["shift_start"], date)
        if new_out is not None:
            new_work = compute_work_hours(new_in, new_out)
            new_ot = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date)
            new_half = new_work < 4.50
        else:
            new_work, new_ot, new_half = None, 0, False

    def ms_or_none(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=UTC)
            return dt_to_ms(v)
        return v

    old_map = {"status": old_status, "punch_in": ms_or_none(old_in), "punch_out": ms_or_none(old_out), "work_hours": old_work, "late_minutes": old_late, "overtime_minutes": old_ot, "half_day": old_half}
    new_map = {"status": new_status, "punch_in": ms_or_none(new_in), "punch_out": ms_or_none(new_out), "work_hours": new_work, "late_minutes": new_late, "overtime_minutes": new_ot, "half_day": new_half}

    changes: Dict[str, Dict[str, Any]] = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old_map[k] != new_map[k]:
            changes[k] = {"from": old_map[k], "to": new_map[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    # storage values: datetimes for punch times
    entry = {"at": now_utc_truncated(), "by": body.regularized_by, "reason": body.reason, "changes": {}}
    for k, fv in changes.items():
        if k in ("punch_in", "punch_out"):
            f = ms_to_utc_dt(fv["from"]) if fv["from"] is not None else None
            t = ms_to_utc_dt(fv["to"]) if fv["to"] is not None else None
            entry["changes"][k] = {"from": f, "to": t}
        else:
            entry["changes"][k] = {"from": fv["from"], "to": fv["to"]}

    set_doc = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": new_work, "late_minutes": new_late, "overtime_minutes": new_ot, "half_day": new_half}
    # optimistic concurrency on history length so concurrent edits don't lose history
    if "history" in rec and isinstance(rec["history"], list):
        filt: Dict[str, Any] = {"_id": rec["_id"], "history": {"$size": len(old_hist)}}
    else:
        filt = {"_id": rec["_id"], "$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    updated = db.attendance_logs.find_one_and_update(filt, {"$set": set_doc, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        # distinguish concurrent (409) vs missing (404)
        if db.attendance_logs.find_one({"_id": rec["_id"]}) is None:
            raise HTTPException(404, "no record for that date")
        raise HTTPException(409, "concurrent modification")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    validate_month_str(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    m_start, m_end = month_bounds(month)
    joined = emp.get("joined_on", m_start)
    eff_start = max(m_start, joined) if joined else m_start
    if eff_start > m_end:
        working = 0
    else:
        working = working_days_between(eff_start, m_end)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": eff_start if eff_start <= m_end else m_end, "$lte": m_end}}},
        {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, "_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
        {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENT_LIST]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late": {"$sum": "$_late"}, "total_ot": {"$sum": "$_ot"}}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    if eff_start > m_end:
        present, leave, late_c, tot_late, tot_ot = 0, 0, 0, 0, 0
    elif rows:
        r = rows[0]
        present, leave, late_c, tot_late, tot_ot = r.get("present_days", 0), r.get("leave_days", 0), r.get("late_count", 0), r.get("total_late", 0), r.get("total_ot", 0)
    else:
        present, leave, late_c, tot_late, tot_ot = 0, 0, 0, 0, 0
    # present may be int; keep number (0.5 steps)
    if isinstance(present, float) and present.is_integer():
        present = int(present)
    pct = None if working == 0 else round2_half_up(present / working * 100)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present, "leave_days": leave, "late_count": late_c, "total_late_minutes": tot_late, "total_overtime_minutes": tot_ot, "attendance_pct": pct}


def _dept_summary_pipeline(m_start: str, m_end: str, dept_filter: Optional[str]):
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if dept_filter is not None:
        match_emp["department"] = dept_filter
    return [
        {"$match": match_emp},
        {"$lookup": {"from": "attendance_logs", "let": {"ec": "$emp_code", "jo": "$joined_on"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$gte": ["$date", m_start]}, {"$lte": ["$date", m_end]}, {"$gte": ["$date", "$$jo"]}]}}}, {"$project": {"_id": 0, "status": 1, "date": 1, "late_minutes": 1, "overtime_minutes": 1, "work_hours": 1, "half_day": 1}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_status": "$logs.status", "_late": {"$ifNull": ["$logs.late_minutes", 0]}, "_ot": {"$ifNull": ["$logs.overtime_minutes", 0]}, "_work": "$logs.work_hours", "_half": {"$ifNull": ["$logs.half_day", False]}, "_dow": {"$cond": [{"$ifNull": ["$logs.date", False]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}}, None]}}},
        {"$group": {"_id": "$department", "emps": {"$addToSet": "$emp_code"}, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$_status", PRESENT_LIST]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}}, "avg_work_hours_raw": {"$avg": {"$cond": [{"$and": [{"$in": ["$_status", PRESENT_LIST]}, {"$ne": ["$_work", None]}]}, "$_work", None]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$_late"}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_status", "ON_DUTY"]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    validate_month_str(month)
    m_start, m_end = month_bounds(month)
    rows = list(db.employees.aggregate(_dept_summary_pipeline(m_start, m_end, department)))
    items = []
    for r in rows:
        emps = r.get("emps", [])
        avg = r.get("avg_work_hours_raw")
        avg_out = None if avg is None else round2_half_up(avg)
        pres = r.get("present_days", 0)
        if isinstance(pres, float) and pres.is_integer():
            pres = int(pres)
        items.append({"department": r["_id"], "headcount": len(emps), "present_days": pres, "avg_work_hours": avg_out, "late_count": r.get("late_count", 0), "total_late_minutes": r.get("total_late_minutes", 0), "leave_count": r.get("leave_count", 0), "on_duty_count": r.get("on_duty_count", 0)})
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(m_start: str, m_end: str, dept: Optional[str], limit: int):
    pipe: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": m_start, "$lte": m_end}}},
        {"$lookup": {"from": "employees", "let": {"ec": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}}}, {"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if dept is not None:
        pipe.append({"$match": {"emp.department": dept}})
    pipe += [
        {"$group": {"_id": "$emp_code", "name": {"$first": "$emp.name"}, "department": {"$first": "$emp.department"}, "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    validate_month_str(month)
    m_start, m_end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(_late_leaderboard_pipeline(m_start, m_end, department, limit)))
    items = [{"rank": r["rank"], "emp_code": r["_id"], "name": r["name"], "department": r["department"], "total_late_minutes": r["total_late_minutes"], "late_count": r["late_count"]} for r in rows]
    return {"month": month, "items": items}


def _trend_pipeline(dept: str, from_s: str, to_s: str):
    f = dt_date.fromisoformat(from_s)
    t = dt_date.fromisoformat(to_s)
    f_utc = datetime(f.year, f.month, f.day, tzinfo=UTC)
    t_excl = datetime(t.year, t.month, t.day, tzinfo=UTC) + timedelta(days=1)
    return [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {"$lookup": {"from": "employees", "let": {"ec": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$eq": ["$department", dept]}]}}}, {"$project": {"_id": 0, "emp_code": 1}}], "as": "emp"}},
        {"$unwind": "$emp"},
        {"$group": {"_id": "$date", "present_sum": {"$sum": {"$cond": [{"$in": ["$status", PRESENT_LIST]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late_sum": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [f_utc, t_excl]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}, "present_sum": {"$ifNull": ["$present_sum", 0]}, "late_sum": {"$ifNull": ["$late_sum", 0]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", dept]}, {"$lte": ["$joined_on", "$$d"]}]}}}, {"$project": {"_id": 0, "joined_on": 1}}, {"$count": "c"}], "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.c", 0]}, 0]}, "dow": {"$dayOfWeek": "$day"}}},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}, "present_count": "$present_sum", "late_count": "$late_sum"}},
        {"$addFields": {"attendance_rate_raw": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_raw": {"$avg": "$attendance_rate_raw", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate_raw": 1, "moving_raw": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_date_str(from_, "from")
    validate_date_str(to, "to")
    if to < from_:
        raise HTTPException(422, "to < from")
    d1, d2 = dt_date.fromisoformat(from_), dt_date.fromisoformat(to)
    if (d2 - d1).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")
    rows = list(db.attendance_logs.aggregate(_trend_pipeline(department, from_, to)))
    items = []
    for r in rows:
        ar = r.get("attendance_rate_raw")
        mv = r.get("moving_raw")
        # working day with no records -> rate 0.0 (already 0/headcount=0); keep
        ar_out = None if ar is None else round4_half_up(ar)
        mv_out = None if mv is None else round4_half_up(mv)
        pres = r.get("present_count", 0)
        if isinstance(pres, float) and pres.is_integer():
            pres = int(pres)
        items.append({"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": r["headcount"], "present_count": pres, "late_count": r["late_count"], "attendance_rate": ar_out, "moving_avg_7d": mv_out})
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain
# --------------------------------------------------------------------------- #
def _explain_find(coll: str, filt: Dict[str, Any], sort: Any, skip: int, limit: int):
    cmd: Dict[str, Any] = {"find": coll, "filter": filt}
    if sort:
        cmd["sort"] = sort
    if skip:
        cmd["skip"] = skip
    if limit:
        cmd["limit"] = limit
    return db.command("explain", cmd, verbosity="executionStats")


def _explain_agg(coll: str, pipeline: List[Dict[str, Any]]):
    return db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        if date_from is not None:
            validate_date_str(date_from, "date_from")
        if date_to is not None:
            validate_date_str(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        q: Dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status
        sort = {"date": -1, "emp_code": 1}
        exp = _explain_find("attendance_logs", q, sort, (page - 1) * page_size, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month required")
        validate_month_str(month)
        emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
        m_start, m_end = month_bounds(month)
        joined = emp.get("joined_on", m_start) if emp else m_start
        eff = max(m_start, joined) if joined else m_start
        lo = eff if eff <= m_end else m_end
        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": m_end}}},
            {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, "_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
            {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late": {"$sum": "$_late"}, "total_ot": {"$sum": "$_ot"}}},
        ]
        exp = _explain_agg("attendance_logs", pipe)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month required")
        validate_month_str(month)
        m_start, m_end = month_bounds(month)
        pipe = _dept_summary_pipeline(m_start, m_end, department)
        exp = _explain_agg("employees", pipe)
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month required")
        validate_month_str(month)
        m_start, m_end = month_bounds(month)
        pipe = _late_leaderboard_pipeline(m_start, m_end, department, limit)
        exp = _explain_agg("attendance_logs", pipe)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from, to required")
        validate_date_str(from_, "from")
        validate_date_str(to, "to")
        if to < from_:
            raise HTTPException(422, "to < from")
        pipe = _trend_pipeline(department, from_, to)
        exp = _explain_agg("attendance_logs", pipe)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    raise HTTPException(422, "unknown endpoint")
