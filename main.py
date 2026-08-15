import hmac
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint, delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator

from auth_adapter import AuthNotConfigured, verify_license_key
from db import Base, SessionLocal, engine
from models import (
    Academy, AdminCredential, AuthorizedUser, MemberCategory, MemberPolicy,
    Reservation, Room, RoomSchedule, UnavailableBlock,
)
from schemas import (
    AcademyCreateRequest,
    AcademyDeleteRequest,
    AcademyManagementRequest,
    AcademyRegistrationVerifyRequest,
    AdminLoginRequest,
    AdminMemberReservationCreate,
    AdminReservationUpdate,
    AuthorizedUserCreate,
    AuthorizedUserUpdate,
    BlockCreate,
    CancelRequest,
    MemberCategoryCreate,
    MemberCategoryUpdate,
    PasswordChangeRequest,
    ReservationCreate,
    ReservationMoveRequest,
    RoomCreate,
    RoomUpdate,
    UserLoginRequest,
)
from security import (
    create_academy_registration_token,
    create_admin_app_token,
    create_admin_recovery_token,
    create_admin_session,
    create_app_token,
    hash_password,
    require_admin,
    require_app_token,
    verify_academy_registration_token,
    verify_admin_recovery_token,
    verify_password,
)


class PracticeJournalCreatePayload(BaseModel):
    content: str = Field(min_length=1, max_length=3000)

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("연습일지 내용을 입력해 주세요.")
        return value


class AdminPracticeJournalCreatePayload(BaseModel):
    user_id: int
    reservation_id: str = Field(min_length=1, max_length=36)
    content: str = Field(min_length=1, max_length=3000)

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("연습일지 내용을 입력해 주세요.")
        return value


class PracticeJournalUpdatePayload(BaseModel):
    content: str = Field(min_length=1, max_length=3000)

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("연습일지 내용을 입력해 주세요.")
        return value


# 연습일지는 기존 예약/회원/학원 테이블을 변경하지 않고 별도 테이블 하나만 추가한다.
practice_journals_table = Table(
    "academy_practice_journals",
    Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("academy_id", Integer, ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("user_id", Integer, ForeignKey("academy_authorized_users.id", ondelete="SET NULL"), nullable=True, index=True),
    Column("reservation_id", String(36), ForeignKey("academy_reservations.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("content", Text, nullable=False),
    Column("authored_by", String(20), nullable=False, default="student"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("reservation_id", name="uq_academy_practice_journal_reservation"),
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

LEGACY_ACADEMY_NAME = os.getenv("ACADEMY_NAME", "킴스보컬미디학원").strip() or "킴스보컬미디학원"
LEGACY_RECOVERY_NAME = os.getenv("ADMIN_RECOVERY_NAME", "김병현").strip() or "김병현"
LEGACY_RECOVERY_PHONE_LAST4 = os.getenv("ADMIN_RECOVERY_PHONE_LAST4", "0667").strip() or "0667"

BOOKING_TIMEZONE_NAME = os.getenv("BOOKING_TIMEZONE", "Asia/Seoul").strip() or "Asia/Seoul"
BOOKING_TIMEZONE = ZoneInfo(BOOKING_TIMEZONE_NAME)

# 이용 중 조기 종료는 기존 DB 스키마를 변경하지 않기 위해 cancel_reason에
# 내부 마커를 붙여 저장한다. cancelled_at은 설정하지 않으므로 취소 예약과 구분된다.
EARLY_END_REASON_PREFIX = "__EARLY_END__:"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _legacy_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text_value = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text_value)
        except ValueError:
            return None
    return value


def _is_early_ended(r: Reservation) -> bool:
    return (
        r.cancelled_at is None
        and isinstance(r.cancel_reason, str)
        and r.cancel_reason.startswith(EARLY_END_REASON_PREFIX)
    )


def _visible_reservation_reason(r: Reservation) -> str | None:
    if not r.cancel_reason:
        return None
    if _is_early_ended(r):
        value = r.cancel_reason[len(EARLY_END_REASON_PREFIX):].strip()
        return value or None
    return r.cancel_reason


def reservation_status(r: Reservation, now: datetime | None = None) -> str:
    now = _aware_utc(now or now_utc())
    start_at = _aware_utc(r.start_at)
    end_at = _aware_utc(r.end_at)
    if r.cancelled_at is not None:
        return "취소됨"
    if _is_early_ended(r):
        return "이용중 종료"
    if now < start_at:
        return "예약중"
    if start_at <= now < end_at:
        return "사용중"
    return "사용종료"


def public_reservation(r: Reservation) -> dict:
    return {
        "id": r.id,
        "room_id": r.room_id,
        "start_at": r.start_at,
        "end_at": r.end_at,
        "status": reservation_status(r),
    }


def admin_reservation(r: Reservation) -> dict:
    return {
        **public_reservation(r),
        "nickname": r.nickname,
        "phone_last4": r.phone_last4,
        "cancelled_at": r.cancelled_at,
        "cancel_reason": _visible_reservation_reason(r),
        "created_at": r.created_at,
    }


def _practice_duration_hours(start_at: datetime, end_at: datetime) -> float:
    seconds = (_aware_utc(end_at) - _aware_utc(start_at)).total_seconds()
    return max(0.0, seconds / 3600.0)


def _current_member(db: Session, academy_id: int, name: str | None, phone_last4: str | None) -> AuthorizedUser | None:
    if not name or not phone_last4:
        return None
    return db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.academy_id == academy_id,
            AuthorizedUser.name == name,
            AuthorizedUser.phone_last4 == phone_last4,
        )
    )


def _practice_journal_for_reservation(db: Session, reservation_id: str):
    return db.execute(
        select(practice_journals_table).where(
            practice_journals_table.c.reservation_id == reservation_id
        )
    ).mappings().first()


def _practice_journal_dict(db: Session, row, reservation: Reservation) -> dict:
    room_name = db.scalar(
        select(Room.name).where(Room.id == reservation.room_id)
    )
    return {
        "id": row["id"],
        "reservation_id": reservation.id,
        "room_id": reservation.room_id,
        "room_name": room_name,
        "start_at": reservation.start_at,
        "end_at": reservation.end_at,
        "duration_hours": _practice_duration_hours(reservation.start_at, reservation.end_at),
        "content": row["content"],
        "authored_by": row["authored_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _practice_pending_dict(db: Session, reservation: Reservation) -> dict:
    room_name = db.scalar(select(Room.name).where(Room.id == reservation.room_id))
    return {
        "reservation_id": reservation.id,
        "room_id": reservation.room_id,
        "room_name": room_name,
        "start_at": reservation.start_at,
        "end_at": reservation.end_at,
        "duration_hours": _practice_duration_hours(reservation.start_at, reservation.end_at),
    }


def _practice_total_hours_for_member(db: Session, academy_id: int, name: str, phone_last4: str) -> float:
    rows = db.scalars(
        select(Reservation).where(
            Reservation.academy_id == academy_id,
            Reservation.nickname == name,
            Reservation.phone_last4 == phone_last4,
            Reservation.cancelled_at.is_(None),
            Reservation.end_at <= now_utc(),
        )
    ).all()
    return sum(_practice_duration_hours(row.start_at, row.end_at) for row in rows)


def _schedule_values(schedule: RoomSchedule | None) -> tuple[int, int]:
    if schedule is None:
        return 0, 24
    return schedule.open_hour, schedule.close_hour


def room_dict(room: Room, schedule: RoomSchedule | None = None) -> dict:
    open_hour, close_hour = _schedule_values(schedule)
    return {
        "id": room.id,
        "name": room.name,
        "is_paused": room.is_paused,
        "pause_reason": room.pause_reason,
        "open_hour": open_hour,
        "close_hour": close_hour,
    }


def academy_dict(academy: Academy) -> dict:
    return {"id": academy.id, "name": academy.name}


def category_dict(row: MemberCategory) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at,
    }


def authorized_user_dict(
    row: AuthorizedUser,
    policy: MemberPolicy | None = None,
    category: MemberCategory | None = None,
) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "phone_last4": row.phone_last4,
        "category_id": policy.category_id if policy else None,
        "category_name": category.name if category else None,
        "booking_limit_hours": policy.booking_limit_hours if policy else None,
        "allow_additional_booking": bool(policy.allow_additional_booking) if policy else False,
        "created_at": row.created_at,
    }


def _get_room_schedule(db: Session, room: Room, create: bool = False) -> RoomSchedule | None:
    schedule = db.get(RoomSchedule, room.id)
    if schedule is None and create:
        schedule = RoomSchedule(
            room_id=room.id,
            academy_id=room.academy_id,
            open_hour=0,
            close_hour=24,
        )
        db.add(schedule)
        db.flush()
    return schedule


def _get_member_policy(db: Session, user: AuthorizedUser, create: bool = False) -> MemberPolicy | None:
    policy = db.get(MemberPolicy, user.id)
    if policy is None and create:
        policy = MemberPolicy(
            user_id=user.id,
            academy_id=user.academy_id,
            category_id=None,
            booking_limit_hours=None,
            allow_additional_booking=False,
        )
        db.add(policy)
        db.flush()
    return policy


def _validate_category_for_academy(db: Session, academy_id: int, category_id: int | None) -> MemberCategory | None:
    if category_id is None:
        return None
    category = db.scalar(
        select(MemberCategory).where(
            MemberCategory.id == category_id,
            MemberCategory.academy_id == academy_id,
        )
    )
    if category is None:
        raise HTTPException(status_code=404, detail="회원 카테고리를 찾을 수 없습니다.")
    return category


def _local_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=BOOKING_TIMEZONE)
    return start, start + timedelta(days=1)


def _room_open_bounds(day: date, open_hour: int, close_hour: int) -> tuple[datetime, datetime]:
    day_start, next_day = _local_day_bounds(day)
    open_at = day_start + timedelta(hours=open_hour)
    close_at = next_day if close_hour == 24 else day_start + timedelta(hours=close_hour)
    return open_at, close_at


def _is_within_room_schedule(
    start_at: datetime,
    end_at: datetime,
    schedule: RoomSchedule | None,
) -> bool:
    open_hour, close_hour = _schedule_values(schedule)
    local_start = _aware_utc(start_at).astimezone(BOOKING_TIMEZONE)
    local_end = _aware_utc(end_at).astimezone(BOOKING_TIMEZONE)
    open_at, close_at = _room_open_bounds(local_start.date(), open_hour, close_hour)
    return local_start >= open_at and local_end <= close_at and local_end > local_start


def _operating_hours_error(schedule: RoomSchedule | None) -> str:
    open_hour, close_hour = _schedule_values(schedule)
    return f"이 녹음실의 운영시간은 {open_hour:02d}:00 ~ {close_hour:02d}:00입니다."


def _schedule_blocks_for_range(
    room: Room,
    schedule: RoomSchedule | None,
    from_at: datetime,
    to_at: datetime,
) -> list[dict]:
    open_hour, close_hour = _schedule_values(schedule)
    if open_hour == 0 and close_hour == 24:
        return []

    local_from = _aware_utc(from_at).astimezone(BOOKING_TIMEZONE)
    local_to = _aware_utc(to_at).astimezone(BOOKING_TIMEZONE)
    day = local_from.date()
    last_day = local_to.date()
    rows: list[dict] = []

    while day <= last_day:
        day_start, next_day = _local_day_bounds(day)
        open_at, close_at = _room_open_bounds(day, open_hour, close_hour)

        if open_at > day_start:
            start = max(day_start, local_from)
            end = min(open_at, local_to)
            if end > start:
                rows.append({
                    "id": f"schedule-{room.id}-{day.isoformat()}-before",
                    "room_id": room.id,
                    "start_at": start.astimezone(timezone.utc),
                    "end_at": end.astimezone(timezone.utc),
                    "reason": "운영시간 외",
                    "system_generated": True,
                })

        if close_at < next_day:
            start = max(close_at, local_from)
            end = min(next_day, local_to)
            if end > start:
                rows.append({
                    "id": f"schedule-{room.id}-{day.isoformat()}-after",
                    "room_id": room.id,
                    "start_at": start.astimezone(timezone.utc),
                    "end_at": end.astimezone(timezone.utc),
                    "reason": "운영시간 외",
                    "system_generated": True,
                })
        day += timedelta(days=1)

    return rows


def _member_remaining_reserved_hours(
    db: Session,
    user: AuthorizedUser,
    now: datetime,
    exclude_reservation_id: str | None = None,
) -> float:
    stmt = select(Reservation).where(
        Reservation.academy_id == user.academy_id,
        Reservation.nickname == user.name,
        Reservation.phone_last4 == user.phone_last4,
        Reservation.cancelled_at.is_(None),
        Reservation.end_at > now,
    )
    if exclude_reservation_id is not None:
        stmt = stmt.where(Reservation.id != exclude_reservation_id)

    total_seconds = 0.0
    for reservation in db.scalars(stmt).all():
        start_at = max(_aware_utc(reservation.start_at), now)
        end_at = _aware_utc(reservation.end_at)
        if end_at > start_at:
            total_seconds += (end_at - start_at).total_seconds()
    return total_seconds / 3600.0


def _enforce_member_booking_policy(
    db: Session,
    user: AuthorizedUser,
    start_at: datetime,
    end_at: datetime,
    exclude_reservation_id: str | None = None,
) -> None:
    policy = _get_member_policy(db, user, create=False)
    if policy is None or policy.booking_limit_hours is None:
        return

    limit = float(policy.booking_limit_hours)
    duration = (_aware_utc(end_at) - _aware_utc(start_at)).total_seconds() / 3600.0
    if duration > limit + 1e-9:
        raise HTTPException(
            status_code=409,
            detail=f"회원 예약시간 제한은 1회 최대 {policy.booking_limit_hours}시간입니다.",
        )

    if policy.allow_additional_booking:
        return

    now = now_utc()
    reserved = _member_remaining_reserved_hours(
        db,
        user,
        now,
        exclude_reservation_id=exclude_reservation_id,
    )
    if reserved + duration > limit + 1e-9:
        available = max(0.0, limit - reserved)
        if available <= 1e-9:
            raise HTTPException(
                status_code=409,
                detail=f"현재 예약 가능한 {policy.booking_limit_hours}시간을 모두 사용 중입니다. 기존 예약시간이 지나면 다시 예약할 수 있습니다.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"현재 추가로 예약 가능한 시간은 최대 {available:g}시간입니다. 기존 예약시간이 지나면 예약 가능 시간이 다시 늘어납니다.",
        )


def _find_logged_in_user_for_update(db: Session, auth: dict) -> AuthorizedUser:
    row = db.scalar(
        select(AuthorizedUser)
        .where(
            AuthorizedUser.academy_id == auth["academy_id"],
            AuthorizedUser.name == auth.get("name"),
            AuthorizedUser.phone_last4 == auth.get("phone_last4"),
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=401, detail="등록된 회원 정보를 다시 확인해 주세요.")
    return row


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_academy(db: Session, academy_id: int) -> Academy:
    academy = db.get(Academy, academy_id)
    if academy is None or not academy.is_active:
        raise HTTPException(status_code=404, detail="등록된 학원을 찾을 수 없습니다.")
    return academy


def get_admin_credential(db: Session, academy_id: int) -> AdminCredential:
    credential = db.get(AdminCredential, academy_id)
    if credential is None:
        raise HTTPException(status_code=503, detail="관리자 비밀번호가 설정되어 있지 않습니다.")
    return credential


def _admin_academy_id(request: Request) -> int:
    auth = require_admin(request)
    academy_id = auth.get("academy_id")
    if not isinstance(academy_id, int):
        raise HTTPException(status_code=401, detail="관리자 로그인을 다시 해 주세요.")
    return academy_id


def _has_legacy_data(db: Session, tables: set[str]) -> bool:
    candidates = ["admin_credentials", "rooms", "authorized_users", "reservations", "unavailable_blocks"]
    for table_name in candidates:
        if table_name not in tables:
            continue
        try:
            count = db.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        except Exception:
            continue
        if int(count or 0) > 0:
            return True
    return False


def _migrate_legacy_single_academy():
    """
    기존 1개 학원용 테이블은 삭제하지 않고 그대로 보존한다.
    최초 1회만 새 다중학원 테이블로 복사해 현재 킴스 학원 데이터가 사라지지 않게 한다.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with SessionLocal() as db:
        if db.scalar(select(func.count(Academy.id))) not in (None, 0):
            return
        if not _has_legacy_data(db, tables):
            return

        academy = Academy(
            name=LEGACY_ACADEMY_NAME,
            recovery_name=LEGACY_RECOVERY_NAME,
            recovery_phone_last4=LEGACY_RECOVERY_PHONE_LAST4,
        )
        db.add(academy)
        db.flush()

        legacy_hash = None
        if "admin_credentials" in tables:
            row = db.execute(text("SELECT password_hash FROM admin_credentials WHERE id = 1")).mappings().first()
            if row:
                legacy_hash = row.get("password_hash")
        if not legacy_hash:
            initial_password = os.getenv("WEB_ADMIN_PASSWORD", "").strip()
            if initial_password:
                legacy_hash = hash_password(initial_password)
        if not legacy_hash:
            raise RuntimeError("기존 학원 관리자 비밀번호를 이전할 수 없습니다.")

        db.add(AdminCredential(academy_id=academy.id, password_hash=legacy_hash))

        room_map: dict[int, int] = {}
        if "rooms" in tables:
            rows = db.execute(text(
                "SELECT id, name, is_paused, pause_reason, is_deleted, created_at, updated_at FROM rooms ORDER BY id"
            )).mappings().all()
            for old in rows:
                room = Room(
                    academy_id=academy.id,
                    name=old["name"],
                    is_paused=bool(old["is_paused"]),
                    pause_reason=old["pause_reason"],
                    is_deleted=bool(old["is_deleted"]),
                    created_at=_legacy_datetime(old["created_at"]) or now_utc(),
                    updated_at=_legacy_datetime(old["updated_at"]) or now_utc(),
                )
                db.add(room)
                db.flush()
                room_map[int(old["id"])] = room.id

        if "authorized_users" in tables:
            rows = db.execute(text(
                "SELECT name, phone_last4, created_at FROM authorized_users ORDER BY id"
            )).mappings().all()
            for old in rows:
                db.add(AuthorizedUser(
                    academy_id=academy.id,
                    name=old["name"],
                    phone_last4=old["phone_last4"],
                    created_at=_legacy_datetime(old["created_at"]) or now_utc(),
                ))

        if "reservations" in tables:
            rows = db.execute(text(
                "SELECT id, room_id, nickname, phone_last4, start_at, end_at, cancelled_at, cancel_reason, created_at "
                "FROM reservations"
            )).mappings().all()
            for old in rows:
                new_room_id = room_map.get(int(old["room_id"]))
                if not new_room_id:
                    continue
                db.add(Reservation(
                    id=str(old["id"]),
                    academy_id=academy.id,
                    room_id=new_room_id,
                    nickname=old["nickname"],
                    phone_last4=old["phone_last4"],
                    start_at=_legacy_datetime(old["start_at"]),
                    end_at=_legacy_datetime(old["end_at"]),
                    cancelled_at=_legacy_datetime(old["cancelled_at"]),
                    cancel_reason=old["cancel_reason"],
                    created_at=_legacy_datetime(old["created_at"]) or now_utc(),
                ))

        if "unavailable_blocks" in tables:
            rows = db.execute(text(
                "SELECT id, room_id, start_at, end_at, reason, created_at FROM unavailable_blocks"
            )).mappings().all()
            for old in rows:
                new_room_id = room_map.get(int(old["room_id"]))
                if not new_room_id:
                    continue
                db.add(UnavailableBlock(
                    id=str(old["id"]),
                    academy_id=academy.id,
                    room_id=new_room_id,
                    start_at=_legacy_datetime(old["start_at"]),
                    end_at=_legacy_datetime(old["end_at"]),
                    reason=old["reason"],
                    created_at=_legacy_datetime(old["created_at"]) or now_utc(),
                ))

        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_legacy_single_academy()

    if os.getenv("RENDER") == "true":
        if not os.getenv("ADMIN_SESSION_SECRET"):
            raise RuntimeError("Render production requires ADMIN_SESSION_SECRET.")
        if os.getenv("DATABASE_URL", "").startswith("sqlite"):
            raise RuntimeError("Render production must use Postgres DATABASE_URL, not local SQLite.")

    yield


app = FastAPI(
    title="녹음실 예약 시스템 서버",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"ok": True, "service": "recording-room-reservation-api"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


# -----------------------------
# Academy registration / selection
# -----------------------------

@app.get("/api/v1/academies")
def list_academies(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Academy).where(Academy.is_active.is_(True)).order_by(Academy.name.asc(), Academy.id.asc())
    ).all()
    return [academy_dict(row) for row in rows]


@app.post("/api/v1/academy-registration/verify")
async def verify_academy_registration_key(payload: AcademyRegistrationVerifyRequest):
    try:
        verified = await verify_license_key(payload.license_key)
    except AuthNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not verified:
        raise HTTPException(status_code=401, detail="인증키를 확인해 주세요.")

    return {
        "verified": True,
        "registration_token": create_academy_registration_token(),
    }


@app.post("/api/v1/academies", status_code=201)
def register_academy(payload: AcademyCreateRequest, db: Session = Depends(get_db)):
    verify_academy_registration_token(payload.registration_token)

    academy_name = payload.academy_name.strip()
    recovery_name = payload.recovery_name.strip()

    if db.scalar(select(Academy).where(Academy.name == academy_name)) is not None:
        raise HTTPException(status_code=409, detail="이미 등록된 학원 이름입니다.")

    academy = Academy(
        name=academy_name,
        recovery_name=recovery_name,
        recovery_phone_last4=payload.recovery_phone_last4,
    )
    db.add(academy)
    try:
        db.flush()
        db.add(AdminCredential(
            academy_id=academy.id,
            password_hash=hash_password(payload.admin_password),
        ))
        db.commit()
        db.refresh(academy)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 등록된 학원 이름입니다.")

    return academy_dict(academy)


@app.post("/api/v1/academy-management/list")
def list_academies_for_management(payload: AcademyManagementRequest, db: Session = Depends(get_db)):
    # 인증키 검증 후 발급된 짧은 유효시간의 등록 토큰만 허용한다.
    verify_academy_registration_token(payload.registration_token)
    rows = db.scalars(
        select(Academy)
        .where(Academy.is_active.is_(True))
        .order_by(Academy.name.asc(), Academy.id.asc())
    ).all()
    return [academy_dict(row) for row in rows]


def _delete_legacy_academy_data_if_needed(db: Session, academy_name: str) -> None:
    # 최초 단일학원 데이터를 다중학원 DB로 이전했던 학원을 삭제하는 경우,
    # 남아 있는 구형 테이블 데이터도 함께 지워 서버 재시작 때 다시 복원되지 않게 한다.
    if academy_name != LEGACY_ACADEMY_NAME:
        return

    tables = set(inspect(engine).get_table_names())
    for table_name in (
        "unavailable_blocks",
        "reservations",
        "authorized_users",
        "rooms",
        "admin_credentials",
    ):
        if table_name in tables:
            db.execute(text(f"DELETE FROM {table_name}"))


@app.post("/api/v1/academy-management/delete")
def delete_academy(payload: AcademyDeleteRequest, db: Session = Depends(get_db)):
    # 삭제 화면 진입 때 인증키로 발급받은 토큰을 다시 검증한다.
    verify_academy_registration_token(payload.registration_token)
    academy = get_academy(db, payload.academy_id)
    academy_name = academy.name

    # 학원에 속한 자료를 자식 -> 부모 순서로 명시적으로 삭제한다.
    # DB의 cascade 설정 여부에 기대지 않아 예약/녹음실 FK 충돌을 피한다.
    db.execute(delete(UnavailableBlock).where(UnavailableBlock.academy_id == academy.id))
    db.execute(delete(practice_journals_table).where(practice_journals_table.c.academy_id == academy.id))
    db.execute(delete(Reservation).where(Reservation.academy_id == academy.id))
    db.execute(delete(MemberPolicy).where(MemberPolicy.academy_id == academy.id))
    db.execute(delete(MemberCategory).where(MemberCategory.academy_id == academy.id))
    db.execute(delete(AuthorizedUser).where(AuthorizedUser.academy_id == academy.id))
    db.execute(delete(AdminCredential).where(AdminCredential.academy_id == academy.id))
    db.execute(delete(RoomSchedule).where(RoomSchedule.academy_id == academy.id))
    db.execute(delete(Room).where(Room.academy_id == academy.id))
    db.delete(academy)

    _delete_legacy_academy_data_if_needed(db, academy_name)
    db.commit()

    return {
        "deleted": True,
        "academy_id": payload.academy_id,
        "academy_name": academy_name,
    }


# -----------------------------
# App authentication
# -----------------------------

@app.post("/api/v1/auth/login")
def user_app_login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    academy = get_academy(db, payload.academy_id)
    name = payload.name.strip()
    row = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.academy_id == academy.id,
            AuthorizedUser.name == name,
            AuthorizedUser.phone_last4 == payload.phone_last4,
        )
    )
    if row is None:
        raise HTTPException(status_code=401, detail="선택한 학원에 등록된 이름과 전화번호 끝 4자리를 확인해 주세요.")

    policy = _get_member_policy(db, row, create=False)
    category = _validate_category_for_academy(db, academy.id, policy.category_id) if policy and policy.category_id else None

    return {
        "academy_id": academy.id,
        "academy_name": academy.name,
        "access_token": create_app_token(academy.id, academy.name, row.name, row.phone_last4),
        "token_type": "bearer",
        "role": "user",
        "name": row.name,
        "phone_last4": row.phone_last4,
        "category_id": policy.category_id if policy else None,
        "category_name": category.name if category else None,
        "booking_limit_hours": policy.booking_limit_hours if policy else None,
        "allow_additional_booking": bool(policy.allow_additional_booking) if policy else False,
    }


@app.post("/api/v1/auth/admin-login")
def admin_app_login(payload: AdminLoginRequest, db: Session = Depends(get_db)):
    academy = get_academy(db, payload.academy_id)
    credential = get_admin_credential(db, academy.id)
    if not verify_password(payload.password, credential.password_hash):
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")

    return {
        "academy_id": academy.id,
        "academy_name": academy.name,
        "access_token": create_admin_app_token(academy.id, academy.name, credential.password_hash),
        "token_type": "bearer",
        "role": "admin",
        "name": None,
        "phone_last4": None,
    }


# -----------------------------
# Public iOS app API
# -----------------------------

@app.get("/api/v1/bootstrap")
def bootstrap(request: Request, db: Session = Depends(get_db)):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    academy = get_academy(db, academy_id)
    rooms = db.scalars(
        select(Room).where(
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        ).order_by(Room.name.asc())
    ).all()
    schedules = {
        row.room_id: row
        for row in db.scalars(
            select(RoomSchedule).where(RoomSchedule.academy_id == academy_id)
        ).all()
    }

    user = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.academy_id == academy_id,
            AuthorizedUser.name == auth.get("name"),
            AuthorizedUser.phone_last4 == auth.get("phone_last4"),
        )
    )
    policy = _get_member_policy(db, user, create=False) if user else None
    category = (
        _validate_category_for_academy(db, academy_id, policy.category_id)
        if policy and policy.category_id
        else None
    )

    return {
        "academy_id": academy.id,
        "academy_name": academy.name,
        "server_time": now_utc(),
        "booking_limit_months": 3,
        "user_name": auth.get("name"),
        "phone_last4": auth.get("phone_last4"),
        "category_id": policy.category_id if policy else None,
        "category_name": category.name if category else None,
        "booking_limit_hours": policy.booking_limit_hours if policy else None,
        "allow_additional_booking": bool(policy.allow_additional_booking) if policy else False,
        "rooms": [room_dict(r, schedules.get(r.id)) for r in rooms],
    }


@app.get("/api/v1/rooms")
def list_rooms(request: Request, db: Session = Depends(get_db)):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    rooms = db.scalars(
        select(Room).where(
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        ).order_by(Room.name.asc())
    ).all()
    schedules = {
        row.room_id: row
        for row in db.scalars(
            select(RoomSchedule).where(RoomSchedule.academy_id == academy_id)
        ).all()
    }
    return [room_dict(r, schedules.get(r.id)) for r in rooms]


@app.get("/api/v1/reservations")
def list_public_reservations(
    request: Request,
    room_id: int | None = None,
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]

    stmt = select(Reservation).where(
        Reservation.academy_id == academy_id,
        Reservation.cancelled_at.is_(None),
    )
    if room_id is not None:
        stmt = stmt.where(Reservation.room_id == room_id)
    if from_at is not None:
        if from_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="from_at에는 시간대가 필요합니다.")
        stmt = stmt.where(Reservation.end_at > normalize_utc(from_at))
    if to_at is not None:
        if to_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="to_at에는 시간대가 필요합니다.")
        stmt = stmt.where(Reservation.start_at < normalize_utc(to_at))

    rows = db.scalars(stmt.order_by(Reservation.start_at.asc())).all()
    room_names = {
        row.id: row.name
        for row in db.scalars(
            select(Room).where(Room.academy_id == academy_id)
        ).all()
    }
    result = []
    for reservation in rows:
        item = public_reservation(reservation)
        item["room_name"] = room_names.get(reservation.room_id)
        item["is_mine"] = (
            reservation.nickname == auth.get("name")
            and reservation.phone_last4 == auth.get("phone_last4")
        )
        result.append(item)
    return result


@app.get("/api/v1/blocks")
def list_public_blocks(
    request: Request,
    room_id: int | None = None,
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]

    stmt = select(UnavailableBlock).where(UnavailableBlock.academy_id == academy_id)
    if room_id is not None:
        stmt = stmt.where(UnavailableBlock.room_id == room_id)
    if from_at is not None:
        if from_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="from_at에는 시간대가 포함되어야 합니다.")
        stmt = stmt.where(UnavailableBlock.end_at > normalize_utc(from_at))
    if to_at is not None:
        if to_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="to_at에는 시간대가 포함되어야 합니다.")
        stmt = stmt.where(UnavailableBlock.start_at < normalize_utc(to_at))

    rows = db.scalars(stmt.order_by(UnavailableBlock.start_at.asc())).all()
    result = [
        {
            "id": b.id,
            "room_id": b.room_id,
            "start_at": b.start_at,
            "end_at": b.end_at,
            "reason": b.reason,
            "system_generated": False,
        }
        for b in rows
    ]

    # 기존 iOS 앱은 예약불가 목록을 이용해 시간 버튼을 비활성화하므로,
    # 오픈/마감시간 바깥 구간도 서버가 예약불가 항목처럼 동적으로 내려준다.
    local_now = now_utc().astimezone(BOOKING_TIMEZONE)
    synthetic_from = from_at or datetime.combine(local_now.date(), time.min, tzinfo=BOOKING_TIMEZONE)
    synthetic_to = to_at or (synthetic_from + relativedelta(months=3) + timedelta(days=1))
    if synthetic_from.tzinfo is None or synthetic_to.tzinfo is None:
        raise HTTPException(status_code=422, detail="예약불가 조회 시간에는 시간대가 포함되어야 합니다.")

    room_stmt = select(Room).where(
        Room.academy_id == academy_id,
        Room.is_deleted.is_(False),
    )
    if room_id is not None:
        room_stmt = room_stmt.where(Room.id == room_id)
    room_rows = db.scalars(room_stmt).all()
    schedule_map = {
        row.room_id: row
        for row in db.scalars(
            select(RoomSchedule).where(RoomSchedule.academy_id == academy_id)
        ).all()
    }
    for room in room_rows:
        result.extend(
            _schedule_blocks_for_range(
                room,
                schedule_map.get(room.id),
                synthetic_from,
                synthetic_to,
            )
        )

    result.sort(key=lambda item: _aware_utc(item["start_at"]))
    return result


@app.post("/api/v1/reservations", status_code=201)
def create_reservation(
    payload: ReservationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]

    start = normalize_utc(payload.start_at)
    end = normalize_utc(payload.end_at)
    now = now_utc()
    max_start = now + relativedelta(months=3)

    if start < now:
        raise HTTPException(status_code=409, detail="지난 시간은 예약할 수 없습니다.")
    if start > max_start:
        raise HTTPException(status_code=409, detail="예약은 현재 시점부터 최대 3개월까지만 가능합니다.")

    try:
        user = _find_logged_in_user_for_update(db, auth)
        _enforce_member_booking_policy(db, user, start, end)

        room = db.scalar(
            select(Room)
            .where(
                Room.id == payload.room_id,
                Room.academy_id == academy_id,
                Room.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")
        if room.is_paused:
            raise HTTPException(
                status_code=409,
                detail=room.pause_reason or "현재 이 녹음실은 일시 사용중지 상태입니다.",
            )

        schedule = _get_room_schedule(db, room, create=False)
        if not _is_within_room_schedule(start, end, schedule):
            raise HTTPException(status_code=409, detail=_operating_hours_error(schedule))

        conflict_reservation = db.scalar(
            select(Reservation).where(
                Reservation.academy_id == academy_id,
                Reservation.room_id == room.id,
                Reservation.cancelled_at.is_(None),
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
        )
        if conflict_reservation:
            raise HTTPException(status_code=409, detail="이미 예약된 시간과 겹칩니다.")

        conflict_block = db.scalar(
            select(UnavailableBlock).where(
                UnavailableBlock.academy_id == academy_id,
                UnavailableBlock.room_id == room.id,
                UnavailableBlock.start_at < end,
                UnavailableBlock.end_at > start,
            )
        )
        if conflict_block:
            raise HTTPException(status_code=409, detail="관리자가 예약불가로 지정한 시간입니다.")

        reservation = Reservation(
            academy_id=academy_id,
            room_id=room.id,
            nickname=user.name,
            phone_last4=user.phone_last4,
            start_at=start,
            end_at=end,
        )
        db.add(reservation)
        db.commit()
        db.refresh(reservation)
        return public_reservation(reservation)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@app.get("/api/v1/my-reservations")
def list_my_reservations(
    request: Request,
    include_cancelled: bool = False,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    stmt = select(Reservation).where(
        Reservation.academy_id == academy_id,
        Reservation.nickname == auth.get("name"),
        Reservation.phone_last4 == auth.get("phone_last4"),
    )
    if not include_cancelled:
        stmt = stmt.where(Reservation.cancelled_at.is_(None))

    rows = db.scalars(stmt.order_by(Reservation.start_at.desc())).all()
    room_names = {
        room.id: room.name
        for room in db.scalars(select(Room).where(Room.academy_id == academy_id)).all()
    }
    result = []
    for row in rows:
        item = public_reservation(row)
        item["room_name"] = room_names.get(row.room_id)
        current = now_utc()
        item["can_move"] = (
            row.cancelled_at is None
            and _aware_utc(row.end_at) > current
        )
        result.append(item)
    return result


@app.patch("/api/v1/reservations/{reservation_id}/move")
def move_my_reservation(
    reservation_id: str,
    payload: ReservationMoveRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]

    try:
        reservation = db.scalar(
            select(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.academy_id == academy_id,
                Reservation.nickname == auth.get("name"),
                Reservation.phone_last4 == auth.get("phone_last4"),
            )
            .with_for_update()
        )
        if reservation is None:
            raise HTTPException(status_code=404, detail="본인 예약을 찾을 수 없습니다.")
        if reservation.cancelled_at is not None:
            raise HTTPException(status_code=409, detail="취소된 예약은 이동할 수 없습니다.")

        current = now_utc()
        reservation_start = _aware_utc(reservation.start_at)
        reservation_end = _aware_utc(reservation.end_at)
        if reservation_end <= current:
            raise HTTPException(status_code=409, detail="이미 종료된 예약은 이동할 수 없습니다.")

        # 예약 시작 전이면 기존처럼 예약 전체를 이동한다.
        # 이미 이용 중이면 '지금부터 종료시간까지' 남은 구간만 새 녹음실로 이동한다.
        move_start = reservation_start if current < reservation_start else current

        if payload.room_id == reservation.room_id:
            item = public_reservation(reservation)
            room_name = db.scalar(
                select(Room.name).where(
                    Room.id == reservation.room_id,
                    Room.academy_id == academy_id,
                )
            )
            item["room_name"] = room_name
            item["can_move"] = True
            return item

        room = db.scalar(
            select(Room)
            .where(
                Room.id == payload.room_id,
                Room.academy_id == academy_id,
                Room.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="이동할 녹음실을 찾을 수 없습니다.")
        if room.is_paused:
            raise HTTPException(status_code=409, detail=room.pause_reason or "해당 녹음실은 현재 사용중지 상태입니다.")

        schedule = _get_room_schedule(db, room, create=False)
        if not _is_within_room_schedule(move_start, reservation_end, schedule):
            raise HTTPException(status_code=409, detail=_operating_hours_error(schedule))

        conflict_reservation = db.scalar(
            select(Reservation).where(
                Reservation.academy_id == academy_id,
                Reservation.room_id == room.id,
                Reservation.id != reservation.id,
                Reservation.cancelled_at.is_(None),
                Reservation.start_at < reservation_end,
                Reservation.end_at > move_start,
            )
        )
        if conflict_reservation:
            raise HTTPException(status_code=409, detail="이동하려는 녹음실에 남은 이용시간과 겹치는 예약이 있습니다.")

        conflict_block = db.scalar(
            select(UnavailableBlock).where(
                UnavailableBlock.academy_id == academy_id,
                UnavailableBlock.room_id == room.id,
                UnavailableBlock.start_at < reservation_end,
                UnavailableBlock.end_at > move_start,
            )
        )
        if conflict_block:
            raise HTTPException(status_code=409, detail="이동하려는 녹음실은 남은 이용시간 중 예약불가 시간이 있습니다.")

        if current < reservation_start:
            # 아직 시작 전: 기존 예약의 녹음실만 변경.
            reservation.room_id = room.id
            db.commit()
            db.refresh(reservation)
            moved_reservation = reservation
        else:
            # 이용 중: 현재 녹음실의 사용 이력은 보존하고 현재 시각에서 예약을 분리한다.
            original_end = reservation.end_at
            reservation.end_at = move_start

            moved_reservation = Reservation(
                academy_id=academy_id,
                room_id=room.id,
                nickname=reservation.nickname,
                phone_last4=reservation.phone_last4,
                start_at=move_start,
                end_at=original_end,
            )
            db.add(moved_reservation)
            db.commit()
            db.refresh(moved_reservation)

        item = public_reservation(moved_reservation)
        item["room_name"] = room.name
        item["can_move"] = _aware_utc(moved_reservation.end_at) > now_utc()
        return item
    except HTTPException:
        db.rollback()
        raise



@app.post("/api/v1/reservations/{reservation_id}/cancel")
def cancel_my_reservation(
    reservation_id: str,
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="취소 사유를 입력해 주세요.")

    row = db.scalar(
        select(Reservation).where(
            Reservation.id == reservation_id,
            Reservation.academy_id == academy_id,
            Reservation.nickname == auth.get("name"),
            Reservation.phone_last4 == auth.get("phone_last4"),
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="본인 예약을 찾을 수 없습니다.")
    if row.cancelled_at is not None:
        raise HTTPException(status_code=409, detail="이미 취소된 예약입니다.")
    if _aware_utc(row.start_at) <= now_utc():
        raise HTTPException(status_code=409, detail="이미 시작된 예약은 직접 취소할 수 없습니다. 관리자에게 문의해 주세요.")

    row.cancelled_at = now_utc()
    row.cancel_reason = reason
    db.commit()
    db.refresh(row)
    return admin_reservation(row)


@app.post("/api/v1/reservations/{reservation_id}/end")
def end_my_reservation(
    reservation_id: str,
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="이용종료 사유를 입력해 주세요.")

    try:
        row = db.scalar(
            select(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.academy_id == academy_id,
                Reservation.nickname == auth.get("name"),
                Reservation.phone_last4 == auth.get("phone_last4"),
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="본인 예약을 찾을 수 없습니다.")
        if row.cancelled_at is not None:
            raise HTTPException(status_code=409, detail="취소된 예약은 이용종료할 수 없습니다.")
        if _is_early_ended(row):
            raise HTTPException(status_code=409, detail="이미 이용종료 처리된 예약입니다.")

        current = now_utc()
        start_at = _aware_utc(row.start_at)
        end_at = _aware_utc(row.end_at)

        if current < start_at:
            raise HTTPException(status_code=409, detail="예약 시작 전에는 이용종료할 수 없습니다. 예약 취소를 이용해 주세요.")
        if current >= end_at:
            raise HTTPException(status_code=409, detail="이미 이용시간이 종료된 예약입니다.")

        # 실제 종료 시각까지만 이용한 것으로 반영한다.
        # 남은 예약시간은 즉시 비워져 다른 회원이 다시 예약할 수 있고,
        # 이용현황/연습일지도 실제 시작~종료 시간으로 계산된다.
        row.end_at = current
        row.cancel_reason = EARLY_END_REASON_PREFIX + reason
        db.commit()
        db.refresh(row)

        result = admin_reservation(row)
        result["room_name"] = db.scalar(
            select(Room.name).where(
                Room.id == row.room_id,
                Room.academy_id == academy_id,
            )
        )
        result["can_move"] = False
        return result
    except HTTPException:
        db.rollback()
        raise


@app.get("/api/v1/practice-journals")
def list_my_practice_journals(request: Request, db: Session = Depends(get_db)):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    member = _current_member(db, academy_id, auth.get("name"), auth.get("phone_last4"))
    if member is None:
        raise HTTPException(status_code=401, detail="등록된 회원 정보를 다시 확인해 주세요.")

    journal_rows = db.execute(
        select(practice_journals_table)
        .where(
            practice_journals_table.c.academy_id == academy_id,
            practice_journals_table.c.user_id == member.id,
        )
        .order_by(practice_journals_table.c.created_at.desc())
    ).mappings().all()

    items = []
    for journal in journal_rows:
        reservation = db.scalar(
            select(Reservation).where(
                Reservation.id == journal["reservation_id"],
                Reservation.academy_id == academy_id,
            )
        )
        if reservation is not None:
            items.append(_practice_journal_dict(db, journal, reservation))
    items.sort(key=lambda item: _aware_utc(item["start_at"]), reverse=True)

    return {
        "items": items,
        "total_hours": _practice_total_hours_for_member(db, academy_id, member.name, member.phone_last4),
        "count": len(items),
    }


@app.get("/api/v1/practice-journals/pending")
def list_my_pending_practice_journals(request: Request, db: Session = Depends(get_db)):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    member = _current_member(db, academy_id, auth.get("name"), auth.get("phone_last4"))
    if member is None:
        raise HTTPException(status_code=401, detail="등록된 회원 정보를 다시 확인해 주세요.")

    reservations = db.scalars(
        select(Reservation).where(
            Reservation.academy_id == academy_id,
            Reservation.nickname == member.name,
            Reservation.phone_last4 == member.phone_last4,
            Reservation.cancelled_at.is_(None),
            Reservation.end_at <= now_utc(),
        ).order_by(Reservation.end_at.desc())
    ).all()

    return [
        _practice_pending_dict(db, reservation)
        for reservation in reservations
        if _practice_journal_for_reservation(db, reservation.id) is None
    ]


@app.post("/api/v1/practice-journals/{reservation_id}", status_code=201)
def create_my_practice_journal(
    reservation_id: str,
    payload: PracticeJournalCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_app_token(request)
    academy_id = auth["academy_id"]
    member = _current_member(db, academy_id, auth.get("name"), auth.get("phone_last4"))
    if member is None:
        raise HTTPException(status_code=401, detail="등록된 회원 정보를 다시 확인해 주세요.")

    reservation = db.scalar(
        select(Reservation).where(
            Reservation.id == reservation_id,
            Reservation.academy_id == academy_id,
            Reservation.nickname == member.name,
            Reservation.phone_last4 == member.phone_last4,
        )
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="본인 예약을 찾을 수 없습니다.")
    if reservation.cancelled_at is not None:
        raise HTTPException(status_code=409, detail="취소된 예약에는 연습일지를 작성할 수 없습니다.")
    if _aware_utc(reservation.end_at) > now_utc():
        raise HTTPException(status_code=409, detail="예약시간이 종료된 후 연습일지를 작성할 수 있습니다.")
    if _practice_journal_for_reservation(db, reservation.id) is not None:
        raise HTTPException(status_code=409, detail="이미 작성된 연습일지가 있습니다. 학생은 작성 후 수정할 수 없습니다.")

    now = now_utc()
    journal_id = str(uuid.uuid4())
    db.execute(
        practice_journals_table.insert().values(
            id=journal_id,
            academy_id=academy_id,
            user_id=member.id,
            reservation_id=reservation.id,
            content=payload.content,
            authored_by="student",
            created_at=now,
            updated_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 작성된 연습일지가 있습니다.")

    row = _practice_journal_for_reservation(db, reservation.id)
    return _practice_journal_dict(db, row, reservation)



# -----------------------------
# Admin web login / password recovery
# -----------------------------

def _academy_options(db: Session):
    return db.scalars(
        select(Academy).where(Academy.is_active.is_(True)).order_by(Academy.name.asc(), Academy.id.asc())
    ).all()


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request, db: Session = Depends(get_db)):
    try:
        auth = require_admin(request)
        academy = get_academy(db, auth["academy_id"])
    except HTTPException:
        academy = None

    if academy is None:
        selected_id = request.query_params.get("academy_id", "")
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "academies": _academy_options(db),
                "selected_academy_id": selected_id,
                "login_error": request.query_params.get("error") == "1",
                "password_reset": request.query_params.get("reset") == "1",
                "reauth": request.query_params.get("reauth") == "1",
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"academy_name": academy.name},
    )


@app.get("/admin/forgot", response_class=HTMLResponse, include_in_schema=False)
def admin_forgot_password_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            "academies": _academy_options(db),
            "selected_academy_id": request.query_params.get("academy_id", ""),
            "error": request.query_params.get("error") == "1",
        },
    )


@app.post("/admin/forgot/verify", include_in_schema=False)
def admin_forgot_password_verify(
    academy_id: int = Form(...),
    name: str = Form(...),
    phone_last4: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        academy = get_academy(db, academy_id)
    except HTTPException:
        return RedirectResponse(url="/admin/forgot?error=1", status_code=303)

    supplied_name = name.strip()
    supplied_phone = phone_last4.strip()

    matched = (
        hmac.compare_digest(
            supplied_name.encode("utf-8"),
            academy.recovery_name.encode("utf-8"),
        )
        and hmac.compare_digest(
            supplied_phone.encode("utf-8"),
            academy.recovery_phone_last4.encode("utf-8"),
        )
    )
    if not matched:
        return RedirectResponse(url=f"/admin/forgot?error=1&academy_id={academy.id}", status_code=303)

    response = RedirectResponse(url="/admin/reset-password", status_code=303)
    response.set_cookie(
        "kbh_admin_recovery",
        create_admin_recovery_token(academy.id),
        httponly=True,
        secure=os.getenv("RENDER") == "true",
        samesite="strict",
        max_age=60 * 10,
    )
    return response


@app.get("/admin/reset-password", response_class=HTMLResponse, include_in_schema=False)
def admin_reset_password_page(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("kbh_admin_recovery", "")
    if not token:
        return RedirectResponse(url="/admin/forgot", status_code=303)

    try:
        data = verify_admin_recovery_token(token)
        academy = get_academy(db, data["academy_id"])
    except HTTPException:
        response = RedirectResponse(url="/admin/forgot?error=1", status_code=303)
        response.delete_cookie("kbh_admin_recovery")
        return response

    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "academy_name": academy.name,
            "error": request.query_params.get("error"),
        },
    )


@app.post("/admin/reset-password", include_in_schema=False)
def admin_reset_password(
    request: Request,
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    token = request.cookies.get("kbh_admin_recovery", "")
    if not token:
        return RedirectResponse(url="/admin/forgot", status_code=303)

    try:
        data = verify_admin_recovery_token(token)
        academy_id = data["academy_id"]
        academy = get_academy(db, academy_id)
    except HTTPException:
        response = RedirectResponse(url="/admin/forgot?error=1", status_code=303)
        response.delete_cookie("kbh_admin_recovery")
        return response

    if len(new_password) < 4:
        return RedirectResponse(url="/admin/reset-password?error=short", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse(url="/admin/reset-password?error=mismatch", status_code=303)

    credential = get_admin_credential(db, academy_id)
    credential.password_hash = hash_password(new_password)
    credential.updated_at = now_utc()
    db.add(credential)
    db.commit()

    with SessionLocal() as verify_db:
        saved = verify_db.get(AdminCredential, academy_id)
        if saved is None or not verify_password(new_password, saved.password_hash):
            return RedirectResponse(url="/admin/reset-password?error=save", status_code=303)

    response = RedirectResponse(url=f"/admin?reset=1&academy_id={academy.id}", status_code=303)
    response.delete_cookie("kbh_admin_recovery")
    response.delete_cookie("kbh_admin")
    return response


@app.post("/admin/login", include_in_schema=False)
def admin_login(
    academy_id: int = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        academy = get_academy(db, academy_id)
        credential = get_admin_credential(db, academy.id)
    except HTTPException:
        return RedirectResponse(url="/admin?error=1", status_code=303)

    if not verify_password(password, credential.password_hash):
        return RedirectResponse(url=f"/admin?error=1&academy_id={academy.id}", status_code=303)

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "kbh_admin",
        create_admin_session(academy.id, credential.password_hash),
        httponly=True,
        secure=os.getenv("RENDER") == "true",
        samesite="strict",
        max_age=60 * 60 * 12,
    )
    return response


@app.get("/admin/app-login", include_in_schema=False)
def admin_app_web_login(request: Request, db: Session = Depends(get_db)):
    auth = require_admin(request)
    academy = get_academy(db, auth["academy_id"])
    credential = get_admin_credential(db, academy.id)

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "kbh_admin",
        create_admin_session(academy.id, credential.password_hash),
        httponly=True,
        secure=os.getenv("RENDER") == "true",
        samesite="strict",
        max_age=60 * 60 * 12,
    )
    return response


@app.post("/admin/logout", include_in_schema=False)
def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("kbh_admin")
    return response


# -----------------------------
# Admin API - scoped to selected academy
# -----------------------------

@app.get("/api/admin/categories")
def admin_list_categories(request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    rows = db.scalars(
        select(MemberCategory)
        .where(MemberCategory.academy_id == academy_id)
        .order_by(MemberCategory.name.asc(), MemberCategory.id.asc())
    ).all()
    return [category_dict(row) for row in rows]


@app.post("/api/admin/categories", status_code=201)
def admin_create_category(
    payload: MemberCategoryCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = MemberCategory(academy_id=academy_id, name=payload.name.strip())
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 카테고리가 있습니다.")
    return category_dict(row)


@app.patch("/api/admin/categories/{category_id}")
def admin_update_category(
    category_id: int,
    payload: MemberCategoryUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(MemberCategory).where(
            MemberCategory.id == category_id,
            MemberCategory.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없습니다.")
    row.name = payload.name.strip()
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 카테고리가 있습니다.")
    return category_dict(row)


@app.delete("/api/admin/categories/{category_id}")
def admin_delete_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(MemberCategory).where(
            MemberCategory.id == category_id,
            MemberCategory.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없습니다.")

    policies = db.scalars(
        select(MemberPolicy).where(
            MemberPolicy.academy_id == academy_id,
            MemberPolicy.category_id == category_id,
        )
    ).all()
    for policy in policies:
        policy.category_id = None
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/users")
def admin_list_users(request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    rows = db.scalars(
        select(AuthorizedUser)
        .where(AuthorizedUser.academy_id == academy_id)
        .order_by(AuthorizedUser.name.asc(), AuthorizedUser.id.asc())
    ).all()
    policies = {
        row.user_id: row
        for row in db.scalars(
            select(MemberPolicy).where(MemberPolicy.academy_id == academy_id)
        ).all()
    }
    categories = {
        row.id: row
        for row in db.scalars(
            select(MemberCategory).where(MemberCategory.academy_id == academy_id)
        ).all()
    }
    return [
        authorized_user_dict(
            row,
            policies.get(row.id),
            categories.get(policies[row.id].category_id) if row.id in policies and policies[row.id].category_id else None,
        )
        for row in rows
    ]


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: AuthorizedUserCreate, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    category = _validate_category_for_academy(db, academy_id, payload.category_id)
    row = AuthorizedUser(
        academy_id=academy_id,
        name=payload.name.strip(),
        phone_last4=payload.phone_last4,
    )
    db.add(row)
    try:
        db.flush()
        policy = MemberPolicy(
            user_id=row.id,
            academy_id=academy_id,
            category_id=category.id if category else None,
            booking_limit_hours=payload.booking_limit_hours,
            allow_additional_booking=payload.allow_additional_booking,
        )
        db.add(policy)
        db.commit()
        db.refresh(row)
        db.refresh(policy)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름과 전화번호 끝 4자리가 등록되어 있습니다.")
    return authorized_user_dict(row, policy, category)


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    payload: AuthorizedUserUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.id == user_id,
            AuthorizedUser.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="등록 사용자를 찾을 수 없습니다.")

    policy = _get_member_policy(db, row, create=True)

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.phone_last4 is not None:
        row.phone_last4 = payload.phone_last4

    fields = payload.model_fields_set
    if "category_id" in fields:
        category = _validate_category_for_academy(db, academy_id, payload.category_id)
        policy.category_id = category.id if category else None
    else:
        category = _validate_category_for_academy(db, academy_id, policy.category_id) if policy.category_id else None

    if "booking_limit_hours" in fields:
        policy.booking_limit_hours = payload.booking_limit_hours
    if "allow_additional_booking" in fields and payload.allow_additional_booking is not None:
        policy.allow_additional_booking = payload.allow_additional_booking
    policy.updated_at = now_utc()

    try:
        db.commit()
        db.refresh(row)
        db.refresh(policy)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름과 전화번호 끝 4자리가 등록되어 있습니다.")
    return authorized_user_dict(row, policy, category)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.id == user_id,
            AuthorizedUser.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="등록 사용자를 찾을 수 없습니다.")
    db.execute(delete(MemberPolicy).where(MemberPolicy.user_id == row.id))
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reservations", status_code=201)
def admin_create_member_reservation(
    user_id: int,
    payload: AdminMemberReservationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    start = normalize_utc(payload.start_at)
    end = normalize_utc(payload.end_at)
    current = now_utc()
    max_start = current + relativedelta(months=3)

    if start < current:
        raise HTTPException(status_code=409, detail="지난 시간은 예약할 수 없습니다.")
    if start > max_start:
        raise HTTPException(status_code=409, detail="예약은 현재 시점부터 최대 3개월까지만 가능합니다.")

    try:
        user = db.scalar(
            select(AuthorizedUser)
            .where(
                AuthorizedUser.id == user_id,
                AuthorizedUser.academy_id == academy_id,
            )
            .with_for_update()
        )
        if user is None:
            raise HTTPException(status_code=404, detail="등록 사용자를 찾을 수 없습니다.")

        # 회원에게 설정된 예약시간 제한을 그대로 적용한다.
        # 제한을 초과하는 경우에만 관리자 비밀번호를 다시 확인하여 이번 예약 1건을 예외 승인한다.
        policy_error = None
        try:
            _enforce_member_booking_policy(db, user, start, end)
        except HTTPException as exc:
            policy_error = exc

        if policy_error is not None:
            credential = get_admin_credential(db, academy_id)
            password = (payload.admin_password or "").strip()
            if not password or not verify_password(password, credential.password_hash):
                detail = getattr(policy_error, "detail", "회원 예약시간 제한을 초과했습니다.")
                raise HTTPException(
                    status_code=409,
                    detail=f"{detail} 관리자 비밀번호를 입력하면 이번 예약에 한해 추가시간을 승인할 수 있습니다.",
                )

        room = db.scalar(
            select(Room)
            .where(
                Room.id == payload.room_id,
                Room.academy_id == academy_id,
                Room.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")
        if room.is_paused:
            raise HTTPException(
                status_code=409,
                detail=room.pause_reason or "현재 이 녹음실은 일시 사용중지 상태입니다.",
            )

        schedule = _get_room_schedule(db, room, create=False)
        if not _is_within_room_schedule(start, end, schedule):
            raise HTTPException(status_code=409, detail=_operating_hours_error(schedule))

        conflict_reservation = db.scalar(
            select(Reservation).where(
                Reservation.academy_id == academy_id,
                Reservation.room_id == room.id,
                Reservation.cancelled_at.is_(None),
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
        )
        if conflict_reservation:
            raise HTTPException(status_code=409, detail="이미 예약된 시간과 겹칩니다.")

        conflict_block = db.scalar(
            select(UnavailableBlock).where(
                UnavailableBlock.academy_id == academy_id,
                UnavailableBlock.room_id == room.id,
                UnavailableBlock.start_at < end,
                UnavailableBlock.end_at > start,
            )
        )
        if conflict_block:
            raise HTTPException(status_code=409, detail="관리자가 예약불가로 지정한 시간입니다.")

        reservation = Reservation(
            academy_id=academy_id,
            room_id=room.id,
            nickname=user.name,
            phone_last4=user.phone_last4,
            start_at=start,
            end_at=end,
        )
        db.add(reservation)
        db.commit()
        db.refresh(reservation)
        result = admin_reservation(reservation)
        result["room_name"] = room.name
        result["admin_override"] = policy_error is not None
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise



@app.get("/api/admin/practice-journals")
def admin_list_practice_journals(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    user = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.id == user_id,
            AuthorizedUser.academy_id == academy_id,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail="회원을 찾을 수 없습니다.")

    journal_rows = db.execute(
        select(practice_journals_table)
        .where(
            practice_journals_table.c.academy_id == academy_id,
            practice_journals_table.c.user_id == user.id,
        )
    ).mappings().all()

    items = []
    for journal in journal_rows:
        reservation = db.scalar(
            select(Reservation).where(
                Reservation.id == journal["reservation_id"],
                Reservation.academy_id == academy_id,
            )
        )
        if reservation is not None:
            items.append(_practice_journal_dict(db, journal, reservation))
    items.sort(key=lambda item: _aware_utc(item["start_at"]), reverse=True)

    return {
        "items": items,
        "total_hours": _practice_total_hours_for_member(db, academy_id, user.name, user.phone_last4),
        "count": len(items),
    }


@app.get("/api/admin/practice-journals/pending")
def admin_list_pending_practice_journals(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    user = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.id == user_id,
            AuthorizedUser.academy_id == academy_id,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail="회원을 찾을 수 없습니다.")

    reservations = db.scalars(
        select(Reservation).where(
            Reservation.academy_id == academy_id,
            Reservation.nickname == user.name,
            Reservation.phone_last4 == user.phone_last4,
            Reservation.cancelled_at.is_(None),
            Reservation.end_at <= now_utc(),
        ).order_by(Reservation.end_at.desc())
    ).all()

    return [
        _practice_pending_dict(db, reservation)
        for reservation in reservations
        if _practice_journal_for_reservation(db, reservation.id) is None
    ]


@app.post("/api/admin/practice-journals", status_code=201)
def admin_create_practice_journal(
    payload: AdminPracticeJournalCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    user = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.id == payload.user_id,
            AuthorizedUser.academy_id == academy_id,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail="회원을 찾을 수 없습니다.")

    reservation = db.scalar(
        select(Reservation).where(
            Reservation.id == payload.reservation_id,
            Reservation.academy_id == academy_id,
            Reservation.nickname == user.name,
            Reservation.phone_last4 == user.phone_last4,
        )
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="회원의 예약을 찾을 수 없습니다.")
    if reservation.cancelled_at is not None:
        raise HTTPException(status_code=409, detail="취소된 예약에는 연습일지를 등록할 수 없습니다.")
    if _aware_utc(reservation.end_at) > now_utc():
        raise HTTPException(status_code=409, detail="예약시간이 종료된 후 연습일지를 등록할 수 있습니다.")
    if _practice_journal_for_reservation(db, reservation.id) is not None:
        raise HTTPException(status_code=409, detail="이미 등록된 연습일지가 있습니다.")

    now = now_utc()
    db.execute(
        practice_journals_table.insert().values(
            id=str(uuid.uuid4()),
            academy_id=academy_id,
            user_id=user.id,
            reservation_id=reservation.id,
            content=payload.content,
            authored_by="admin",
            created_at=now,
            updated_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 등록된 연습일지가 있습니다.")

    row = _practice_journal_for_reservation(db, reservation.id)
    return _practice_journal_dict(db, row, reservation)


@app.patch("/api/admin/practice-journals/{journal_id}")
def admin_update_practice_journal(
    journal_id: str,
    payload: PracticeJournalUpdatePayload,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = db.execute(
        select(practice_journals_table).where(
            practice_journals_table.c.id == journal_id,
            practice_journals_table.c.academy_id == academy_id,
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="연습일지를 찾을 수 없습니다.")

    db.execute(
        practice_journals_table.update()
        .where(practice_journals_table.c.id == journal_id)
        .values(content=payload.content, updated_at=now_utc())
    )
    db.commit()

    updated = db.execute(
        select(practice_journals_table).where(practice_journals_table.c.id == journal_id)
    ).mappings().first()
    reservation = db.scalar(select(Reservation).where(Reservation.id == updated["reservation_id"]))
    if reservation is None:
        raise HTTPException(status_code=404, detail="연결된 예약을 찾을 수 없습니다.")
    return _practice_journal_dict(db, updated, reservation)



@app.post("/api/admin/password")
def admin_change_password(
    payload: PasswordChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    credential = get_admin_credential(db, academy_id)

    if not verify_password(payload.current_password, credential.password_hash):
        raise HTTPException(status_code=400, detail="현재 관리자 비밀번호가 올바르지 않습니다.")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=409, detail="새 비밀번호는 현재 비밀번호와 다르게 입력해 주세요.")

    credential.password_hash = hash_password(payload.new_password)
    credential.updated_at = now_utc()
    db.add(credential)
    db.commit()

    with SessionLocal() as verify_db:
        saved = verify_db.get(AdminCredential, academy_id)
        if saved is None or not verify_password(payload.new_password, saved.password_hash):
            raise HTTPException(status_code=500, detail="관리자 비밀번호 변경을 저장하지 못했습니다.")

    return {"ok": True, "logout_required": True}


@app.get("/api/admin/rooms")
def admin_list_rooms(request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    rooms = db.scalars(
        select(Room).where(
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        ).order_by(Room.name.asc())
    ).all()
    schedules = {
        row.room_id: row
        for row in db.scalars(
            select(RoomSchedule).where(RoomSchedule.academy_id == academy_id)
        ).all()
    }
    return [room_dict(r, schedules.get(r.id)) for r in rooms]


@app.post("/api/admin/rooms", status_code=201)
def admin_create_room(payload: RoomCreate, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    name = payload.name.strip()

    existing = db.scalar(
        select(Room).where(
            Room.academy_id == academy_id,
            Room.name == name,
        )
    )
    if existing is not None:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.is_paused = False
            existing.pause_reason = None
            schedule = _get_room_schedule(db, existing, create=True)
            schedule.open_hour = payload.open_hour
            schedule.close_hour = payload.close_hour
            schedule.updated_at = now_utc()
            db.commit()
            db.refresh(existing)
            db.refresh(schedule)
            return room_dict(existing, schedule)
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")

    room = Room(academy_id=academy_id, name=name)
    db.add(room)
    try:
        db.flush()
        schedule = RoomSchedule(
            room_id=room.id,
            academy_id=academy_id,
            open_hour=payload.open_hour,
            close_hour=payload.close_hour,
        )
        db.add(schedule)
        db.commit()
        db.refresh(room)
        db.refresh(schedule)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")
    return room_dict(room, schedule)


@app.patch("/api/admin/rooms/{room_id}")
def admin_update_room(
    room_id: int,
    payload: RoomUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    room = db.scalar(
        select(Room).where(
            Room.id == room_id,
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        )
    )
    if room is None:
        raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")

    if payload.name is not None:
        room.name = payload.name.strip()
    if payload.is_paused is not None:
        room.is_paused = payload.is_paused
    if payload.pause_reason is not None or payload.is_paused is False:
        room.pause_reason = payload.pause_reason if payload.is_paused is not False else None

    schedule = _get_room_schedule(db, room, create=True)
    open_hour = payload.open_hour if payload.open_hour is not None else schedule.open_hour
    close_hour = payload.close_hour if payload.close_hour is not None else schedule.close_hour
    if close_hour <= open_hour:
        raise HTTPException(status_code=422, detail="마감시간은 오픈시간보다 뒤여야 합니다.")
    schedule.open_hour = open_hour
    schedule.close_hour = close_hour
    schedule.updated_at = now_utc()

    try:
        db.commit()
        db.refresh(room)
        db.refresh(schedule)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")
    return room_dict(room, schedule)


@app.delete("/api/admin/rooms/{room_id}")
def admin_delete_room(room_id: int, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    room = db.scalar(
        select(Room).where(
            Room.id == room_id,
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        )
    )
    if room is None:
        raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")
    room.is_deleted = True
    room.is_paused = True
    db.commit()
    return {"ok": True}


@app.get("/api/admin/reservations")
def admin_list_reservations(
    request: Request,
    room_id: int | None = None,
    include_cancelled: bool = False,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    stmt = select(Reservation).where(Reservation.academy_id == academy_id)
    if room_id is not None:
        stmt = stmt.where(Reservation.room_id == room_id)
    if not include_cancelled:
        stmt = stmt.where(Reservation.cancelled_at.is_(None))
    rows = db.scalars(stmt.order_by(Reservation.start_at.desc())).all()
    return [admin_reservation(r) for r in rows]


@app.patch("/api/admin/reservations/{reservation_id}")
def admin_update_reservation(
    reservation_id: str,
    payload: AdminReservationUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)

    try:
        row = db.scalar(
            select(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.academy_id == academy_id,
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="예약을 찾을 수 없습니다.")
        if row.cancelled_at is not None:
            raise HTTPException(status_code=409, detail="취소된 예약은 수정할 수 없습니다.")
        if _aware_utc(row.end_at) <= now_utc():
            raise HTTPException(status_code=409, detail="이미 종료된 예약은 수정할 수 없습니다.")

        room_id = payload.room_id if payload.room_id is not None else row.room_id
        start = normalize_utc(payload.start_at) if payload.start_at is not None else _aware_utc(row.start_at)
        end = normalize_utc(payload.end_at) if payload.end_at is not None else _aware_utc(row.end_at)

        if end <= start:
            raise HTTPException(status_code=422, detail="종료 시간은 시작 시간보다 뒤여야 합니다.")

        room = db.scalar(
            select(Room)
            .where(
                Room.id == room_id,
                Room.academy_id == academy_id,
                Room.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")
        if room.is_paused:
            raise HTTPException(status_code=409, detail=room.pause_reason or "해당 녹음실은 현재 사용중지 상태입니다.")

        schedule = _get_room_schedule(db, room, create=False)
        if not _is_within_room_schedule(start, end, schedule):
            raise HTTPException(status_code=409, detail=_operating_hours_error(schedule))

        conflict_reservation = db.scalar(
            select(Reservation).where(
                Reservation.academy_id == academy_id,
                Reservation.room_id == room.id,
                Reservation.id != row.id,
                Reservation.cancelled_at.is_(None),
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
        )
        if conflict_reservation:
            raise HTTPException(status_code=409, detail="수정하려는 시간에 다른 예약이 있습니다.")

        conflict_block = db.scalar(
            select(UnavailableBlock).where(
                UnavailableBlock.academy_id == academy_id,
                UnavailableBlock.room_id == room.id,
                UnavailableBlock.start_at < end,
                UnavailableBlock.end_at > start,
            )
        )
        if conflict_block:
            raise HTTPException(status_code=409, detail="수정하려는 시간은 예약불가로 지정되어 있습니다.")

        row.room_id = room.id
        row.start_at = start
        row.end_at = end
        db.commit()
        db.refresh(row)
        return admin_reservation(row)
    except HTTPException:
        db.rollback()
        raise


@app.post("/api/admin/reservations/{reservation_id}/cancel")
def admin_cancel_reservation(
    reservation_id: str,
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(Reservation).where(
            Reservation.id == reservation_id,
            Reservation.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="예약을 찾을 수 없습니다.")
    if _is_early_ended(row):
        raise HTTPException(status_code=409, detail="이용중 종료 기록은 취소 예약으로 변경할 수 없습니다.")
    if row.cancelled_at is None:
        row.cancelled_at = now_utc()
        row.cancel_reason = payload.reason
        db.commit()
        db.refresh(row)
    return admin_reservation(row)


@app.delete("/api/admin/reservations/{reservation_id}")
def admin_delete_cancelled_reservation(
    reservation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(Reservation).where(
            Reservation.id == reservation_id,
            Reservation.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="예약을 찾을 수 없습니다.")
    if row.cancelled_at is None and not _is_early_ended(row):
        raise HTTPException(status_code=409, detail="취소 또는 이용중 종료 기록만 삭제할 수 있습니다.")

    db.execute(
        delete(practice_journals_table).where(practice_journals_table.c.reservation_id == row.id)
    )
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/blocks")
def admin_list_blocks(request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    rows = db.scalars(
        select(UnavailableBlock)
        .where(UnavailableBlock.academy_id == academy_id)
        .order_by(UnavailableBlock.start_at.desc())
    ).all()
    return [
        {
            "id": b.id,
            "room_id": b.room_id,
            "start_at": b.start_at,
            "end_at": b.end_at,
            "reason": b.reason,
        }
        for b in rows
    ]


@app.post("/api/admin/blocks", status_code=201)
def admin_create_block(payload: BlockCreate, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    start = normalize_utc(payload.start_at)
    end = normalize_utc(payload.end_at)

    room = db.scalar(
        select(Room)
        .where(
            Room.id == payload.room_id,
            Room.academy_id == academy_id,
            Room.is_deleted.is_(False),
        )
        .with_for_update()
    )
    if room is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")

    existing_reservation = db.scalar(
        select(Reservation).where(
            Reservation.academy_id == academy_id,
            Reservation.room_id == room.id,
            Reservation.cancelled_at.is_(None),
            Reservation.start_at < end,
            Reservation.end_at > start,
        )
    )
    if existing_reservation:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="해당 시간에 기존 예약이 있습니다. 먼저 예약을 취소한 뒤 예약불가로 지정해 주세요.",
        )

    block = UnavailableBlock(
        academy_id=academy_id,
        room_id=room.id,
        start_at=start,
        end_at=end,
        reason=payload.reason,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return {
        "id": block.id,
        "room_id": block.room_id,
        "start_at": block.start_at,
        "end_at": block.end_at,
        "reason": block.reason,
    }


@app.delete("/api/admin/blocks/{block_id}")
def admin_delete_block(block_id: str, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    row = db.scalar(
        select(UnavailableBlock).where(
            UnavailableBlock.id == block_id,
            UnavailableBlock.academy_id == academy_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="예약불가 항목을 찾을 수 없습니다.")
    db.delete(row)
    db.commit()
    return {"ok": True}
