"""Employee attendance and analytics API."""
import calendar, os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST, UTC = timezone(timedelta(hours=5, minutes=30)), timezone.utc
PRESENCE, STATUSES = {"PRESENT", "WFH", "ON_DUTY"}, {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(value: str) -> date:
    try: return date.fromisoformat(value)
    except ValueError as exc: raise ValueError("invalid date") from exc


def from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000: raise ValueError("must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None: return None
    return int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp() * 1000)


def now() -> datetime: return datetime.now(UTC).replace(microsecond=0)
def local(value: datetime) -> datetime: return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(IST)
def money(value: Decimal, places="0.01") -> float: return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def shift_time(day: date, value: str, next_day=False) -> datetime:
    h, m = map(int, value.split(":")); day += timedelta(days=next_day)
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def derived(emp: dict, pin: Optional[datetime], pout: Optional[datetime]) -> dict:
    if not pin: return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    local_pin = local(pin)
    shift_day = local_pin.date() - timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] and local_pin.strftime("%H:%M") < emp["shift_end"] else local_pin.date()
    start = shift_time(shift_day, emp["shift_start"]); late = int((pin - start).total_seconds() // 60)
    late = late if late > 10 else 0
    if not pout: return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    end_day = local(pin).date() + (timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else timedelta())
    end = shift_time(end_day, emp["shift_end"])
    hours = money(Decimal(str((pout - pin).total_seconds())) / Decimal("3600")); overtime = int((pout - end).total_seconds() // 60)
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.5}


def attendance_date(emp: dict, instant: datetime) -> str:
    value = local(instant)
    if emp["shift_end"] <= emp["shift_start"] and value.strftime("%H:%M") < emp["shift_end"]: value -= timedelta(days=1)
    return value.date().isoformat()


def public(value: Any) -> Any:
    if isinstance(value, datetime): return to_ms(value)
    if isinstance(value, dict): return {k: public(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list): return [public(v) for v in value]
    return value


def record(value: dict) -> dict:
    result = public(value)
    result.setdefault("late_minutes", 0); result.setdefault("overtime_minutes", 0); result.setdefault("half_day", False); result.setdefault("history", [])
    return result


def month_bounds(month: str) -> tuple[date, date]:
    first = parse_date(month + "-01"); return first, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])


def working_days(first: date, last: date) -> int: return sum((first + timedelta(days=i)).weekday() < 5 for i in range((last-first).days + 1))


class Model(BaseModel): model_config = ConfigDict(extra="ignore")
class EmployeeIn(Model):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$"); name: str = Field(min_length=1, max_length=100); email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120); department: str = Field(min_length=1, max_length=50); shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"); shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"); joined_on: str
    @model_validator(mode="after")
    def valid(self):
        parse_date(self.joined_on)
        if self.shift_start == self.shift_end: raise ValueError("shift times must differ")
        return self
class PunchInIn(Model):
    emp_code: str; punched_at: Optional[int] = None; status: str = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def instant(cls, value): return None if value is None else (from_ms(value) and value)
    @field_validator("status")
    @classmethod
    def presence(cls, value):
        if value not in PRESENCE: raise ValueError("invalid presence status")
        return value
class PunchOutIn(Model):
    emp_code: str; punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def instant(cls, value): return None if value is None else (from_ms(value) and value)
class RegularizeIn(Model):
    status: Optional[str] = None; punch_in: Optional[int] = None; punch_out: Optional[int] = None; reason: str = Field(min_length=5, max_length=200); regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def instant(cls, value): return None if value is None else (from_ms(value) and value)
    @field_validator("status")
    @classmethod
    def status_value(cls, value):
        if value is not None and value not in STATUSES: raise ValueError("invalid status")
        return value


@app.on_event("startup")
def startup():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])

@app.get("/health")
def health():
    try: client.admin.command("ping")
    except Exception as exc: raise HTTPException(503, "MongoDB unavailable") from exc
    return {"status": "ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = now()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError as exc: raise HTTPException(409, "emp_code already exists") from exc
    return public(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department else {}; docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [public(x) for x in docs], "total": db.employees.count_documents(query), "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pin = from_ms(body.punched_at) if body.punched_at is not None else now(); doc = {"emp_code": body.emp_code, "date": attendance_date(emp, pin), "status": body.status, "punch_in": pin, "punch_out": None, **derived(emp, pin, None), "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc: raise HTTPException(409, "already punched in for this date") from exc
    return record(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pout = from_ms(body.punched_at) if body.punched_at is not None else now(); doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": pout}}, sort=[("punch_in", DESCENDING)])
    if not doc: raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    if pout <= doc["punch_in"] or pout-doc["punch_in"] > timedelta(hours=24): raise HTTPException(422, "invalid punch-out time")
    updated = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_out": pout, **derived(emp, doc["punch_in"], pout)}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "already punched out")
    return record(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "invalid date range")
    query: dict[str, Any] = {}; query.update({"emp_code": emp_code} if emp_code else {})
    if status not in (None, *STATUSES): raise HTTPException(422, "invalid status")
    if status: query["status"] = status
    if date_from or date_to: query["date"] = {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    total = db.attendance_logs.count_documents(query); docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [record(x) for x in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date_value}")
def regularize(emp_code: str, date_value: str, body: RegularizeIn):
    parse_date(date_value); emp = db.employees.find_one({"emp_code": emp_code}); doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_value})
    if not emp or not doc: raise HTTPException(404, "employee or attendance record not found")
    old = {k: doc.get(k, 0 if k in {"late_minutes", "overtime_minutes"} else False if k == "half_day" else None) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    status = body.status or old["status"]
    if status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None): raise HTTPException(422, "absence cannot have punch times")
    pin = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]; pout = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
    if status in {"ABSENT", "LEAVE"}: pin = pout = None
    if status in PRESENCE and pin is None: raise HTTPException(422, "presence requires punch_in")
    if pin and attendance_date(emp, pin) != date_value: raise HTTPException(422, "punch_in must match attendance date")
    if pout and (pin is None or pout <= pin or pout-pin > timedelta(hours=24)): raise HTTPException(422, "invalid punch_out")
    final = {"status": status, "punch_in": pin, "punch_out": pout, **(derived(emp, pin, pout) if status in PRESENCE else {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})}
    changes = {k: {"from": old[k], "to": final[k]} for k in final if old[k] != final[k]}
    if not changes: raise HTTPException(422, "request changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "history": doc.get("history", [])}, {"$set": final, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "concurrent correction")
    return record(updated)


def month_logs(code: str, month: str):
    first, last = month_bounds(month); return list(db.attendance_logs.aggregate([{"$match": {"emp_code": code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}]))

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last = month_bounds(month); emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    start = max(first, parse_date(emp["joined_on"])); working = working_days(start, last); logs = month_logs(emp_code, month); present = sum(Decimal("0.5") if x.get("half_day") else 1 for x in logs if x.get("status") in PRESENCE and parse_date(x["date"]).weekday() < 5); late = [x for x in logs if x.get("late_minutes", 0) > 0]
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": float(present), "leave_days": sum(x.get("status") == "LEAVE" for x in logs), "late_count": len(late), "total_late_minutes": sum(x.get("late_minutes", 0) for x in logs), "total_overtime_minutes": sum(x.get("overtime_minutes", 0) for x in logs), "attendance_pct": None if not working else money(present / Decimal(working) * 100)}

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    _, last = month_bounds(month); query: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}; query.update({"department": department} if department else {}); employees = list(db.employees.find(query, {"_id": 0})); result = []
    for dep in sorted({x["department"] for x in employees}):
        codes = [x["emp_code"] for x in employees if x["department"] == dep]; logs = list(db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": codes}, "date": {"$gte": month+"-01", "$lte": last.isoformat()}}}])); present = sum(Decimal("0.5") if x.get("half_day") else 1 for x in logs if x.get("status") in PRESENCE and parse_date(x["date"]).weekday() < 5); hours = [x["work_hours"] for x in logs if x.get("status") in PRESENCE and x.get("work_hours") is not None]
        result.append({"department": dep, "headcount": len(codes), "present_days": float(present), "avg_work_hours": None if not hours else money(sum((Decimal(str(x)) for x in hours), Decimal()) / len(hours)), "late_count": sum(x.get("late_minutes", 0) > 0 for x in logs), "total_late_minutes": sum(x.get("late_minutes", 0) for x in logs), "leave_count": sum(x.get("status") == "LEAVE" for x in logs), "on_duty_count": sum(x.get("status") == "ON_DUTY" for x in logs)})
    return {"month": month, "items": result}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    _, last = month_bounds(month); query: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}; query.update({"department": department} if department else {}); employees = {x["emp_code"]: x for x in db.employees.find(query, {"_id": 0})}; rows = list(db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": list(employees)}, "date": {"$gte": month+"-01", "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "count": {"$sum": 1}}}, {"$sort": {"total": -1, "_id": 1}}])); result = []; previous = None
    for index, row in enumerate(rows):
        rank = index + 1 if previous != row["total"] else result[-1]["rank"]; previous = row["total"]
        if rank > limit: break
        emp = employees[row["_id"]]; result.append({"rank": rank, "emp_code": row["_id"], "name": emp["name"], "department": emp["department"], "total_late_minutes": row["total"], "late_count": row["count"]})
    return {"month": month, "items": result}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    first, last = parse_date(from_), parse_date(to)
    if last < first or (last-first).days > 91: raise HTTPException(422, "invalid trend range")
    employees = list(db.employees.find({"department": department}, {"_id": 0}))
    if not employees: raise HTTPException(404, "department not found")
    logs = list(db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": [x["emp_code"] for x in employees]}, "date": {"$gte": from_, "$lte": to}}}])); rows = []
    for i in range((last-first).days+1):
        day = first + timedelta(days=i); day_logs = [x for x in logs if x["date"] == day.isoformat()]; head = sum(parse_date(x["joined_on"]) <= day for x in employees); present = sum(Decimal("0.5") if x.get("half_day") else 1 for x in day_logs if x.get("status") in PRESENCE); rate = money(present/head, "0.0001") if day.weekday() < 5 and head else None; rates = [x["attendance_rate"] for x in rows[-6:] + ([{"attendance_rate": rate}] if rate is not None else []) if x["attendance_rate"] is not None]
        rows.append({"date": day.isoformat(), "is_working_day": day.weekday() < 5, "headcount": head, "present_count": float(present), "late_count": sum(x.get("late_minutes", 0) > 0 for x in day_logs), "attendance_rate": rate, "moving_avg_7d": None if not rates else money(sum(map(Decimal, map(str, rates)))/len(rates), "0.0001")})
    return {"department": department, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}; query.update({"emp_code": emp_code} if emp_code else {}); query.update({"date": {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}} if date_from or date_to else {}); query.update({"status": status} if status else {}); command = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        command = {"aggregate": "attendance_logs", "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$regex": "^"+month}}}], "cursor": {}}
    else: raise HTTPException(422, "unsupported explain endpoint")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": public(db.command("explain", command, verbosity="executionStats"))}
