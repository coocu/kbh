import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth_adapter import AuthNotConfigured, verify_license_key
from db import Base, SessionLocal, engine
from models import Academy, AdminCredential, AuthorizedUser, Reservation, Room, UnavailableBlock
from schemas import (
    AcademyCreateRequest,
    AcademyRegistrationVerifyRequest,
    AdminLoginRequest,
    AuthorizedUserCreate,
    BlockCreate,
    CancelRequest,
    PasswordChangeRequest,
    ReservationCreate,
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

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

LEGACY_ACADEMY_NAME = os.getenv("ACADEMY_NAME", "킴스보컬미디학원").strip() or "킴스보컬미디학원"
LEGACY_RECOVERY_NAME = os.getenv("ADMIN_RECOVERY_NAME", "김병현").strip() or "김병현"
LEGACY_RECOVERY_PHONE_LAST4 = os.getenv("ADMIN_RECOVERY_PHONE_LAST4", "0667").strip() or "0667"


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


def reservation_status(r: Reservation, now: datetime | None = None) -> str:
    now = _aware_utc(now or now_utc())
    start_at = _aware_utc(r.start_at)
    end_at = _aware_utc(r.end_at)
    if r.cancelled_at is not None:
        return "취소됨"
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
        "cancel_reason": r.cancel_reason,
        "created_at": r.created_at,
    }


def room_dict(room: Room) -> dict:
    return {
        "id": room.id,
        "name": room.name,
        "is_paused": room.is_paused,
        "pause_reason": room.pause_reason,
    }


def academy_dict(academy: Academy) -> dict:
    return {"id": academy.id, "name": academy.name}


def authorized_user_dict(row: AuthorizedUser) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "phone_last4": row.phone_last4,
        "created_at": row.created_at,
    }


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

    return {
        "academy_id": academy.id,
        "academy_name": academy.name,
        "access_token": create_app_token(academy.id, academy.name, row.name, row.phone_last4),
        "token_type": "bearer",
        "role": "user",
        "name": row.name,
        "phone_last4": row.phone_last4,
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
    return {
        "academy_id": academy.id,
        "academy_name": academy.name,
        "server_time": now_utc(),
        "booking_limit_months": 3,
        "user_name": auth.get("name"),
        "phone_last4": auth.get("phone_last4"),
        "rooms": [room_dict(r) for r in rooms],
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
    return [room_dict(r) for r in rooms]


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
    return [public_reservation(r) for r in rows]


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
            nickname=auth.get("name") or payload.nickname.strip(),
            phone_last4=auth.get("phone_last4") or payload.phone_last4,
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

@app.get("/api/admin/users")
def admin_list_users(request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    rows = db.scalars(
        select(AuthorizedUser)
        .where(AuthorizedUser.academy_id == academy_id)
        .order_by(AuthorizedUser.name.asc(), AuthorizedUser.id.asc())
    ).all()
    return [authorized_user_dict(row) for row in rows]


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: AuthorizedUserCreate, request: Request, db: Session = Depends(get_db)):
    academy_id = _admin_academy_id(request)
    row = AuthorizedUser(
        academy_id=academy_id,
        name=payload.name.strip(),
        phone_last4=payload.phone_last4,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름과 전화번호 끝 4자리가 등록되어 있습니다.")
    return authorized_user_dict(row)


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
    db.delete(row)
    db.commit()
    return {"ok": True}


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
    return [room_dict(r) for r in rooms]


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
            db.commit()
            db.refresh(existing)
            return room_dict(existing)
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")

    room = Room(academy_id=academy_id, name=name)
    db.add(room)
    try:
        db.commit()
        db.refresh(room)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")
    return room_dict(room)


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

    try:
        db.commit()
        db.refresh(room)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")
    return room_dict(room)


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
    if row.cancelled_at is None:
        raise HTTPException(status_code=409, detail="취소된 예약만 삭제할 수 있습니다.")

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
