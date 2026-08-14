import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import Base, SessionLocal, engine
from models import AdminCredential, AuthorizedUser, Reservation, Room, UnavailableBlock
from schemas import (
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
    create_admin_app_token,
    create_admin_recovery_token,
    create_admin_session,
    create_app_token,
    hash_password,
    require_admin,
    require_app_token,
    verify_admin_recovery_token,
    verify_password,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ACADEMY_NAME = os.getenv("ACADEMY_NAME", "킴스보컬미디학원")
ADMIN_RECOVERY_NAME = os.getenv("ADMIN_RECOVERY_NAME", "김병현").strip()
ADMIN_RECOVERY_PHONE_LAST4 = os.getenv("ADMIN_RECOVERY_PHONE_LAST4", "0667").strip()
ADMIN_RECOVERY_DEVELOPER_NAME = os.getenv("ADMIN_RECOVERY_DEVELOPER_NAME", "코드노트").strip()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def get_admin_credential(db: Session) -> AdminCredential:
    credential = db.get(AdminCredential, 1)
    if credential is None:
        raise HTTPException(status_code=503, detail="관리자 비밀번호가 초기화되지 않았습니다.")
    return credential


def verify_admin_password(db: Session, password: str) -> bool:
    credential = get_admin_credential(db)
    return verify_password(password, credential.password_hash)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        credential = db.get(AdminCredential, 1)
        if credential is None:
            initial_password = os.getenv("WEB_ADMIN_PASSWORD", "").strip()
            if not initial_password:
                raise RuntimeError("Initial WEB_ADMIN_PASSWORD is required once to seed the admin password.")
            db.add(AdminCredential(id=1, password_hash=hash_password(initial_password)))
            db.commit()

    if os.getenv("RENDER") == "true":
        if not os.getenv("ADMIN_SESSION_SECRET"):
            raise RuntimeError("Render production requires ADMIN_SESSION_SECRET.")
        if os.getenv("DATABASE_URL", "").startswith("sqlite"):
            raise RuntimeError("Render production must use Postgres DATABASE_URL, not local SQLite.")

    yield


app = FastAPI(
    title="킴스보컬미디학원 예약 서버",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"ok": True, "service": "kbh-reservation-api"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


# -----------------------------
# App authentication - local KBH server DB only
# -----------------------------

@app.post("/api/v1/auth/login")
def user_app_login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    name = payload.name.strip()
    row = db.scalar(
        select(AuthorizedUser).where(
            AuthorizedUser.name == name,
            AuthorizedUser.phone_last4 == payload.phone_last4,
        )
    )
    if row is None:
        raise HTTPException(status_code=401, detail="등록된 이름과 전화번호 끝 4자리를 확인해 주세요.")

    return {
        "academy_name": ACADEMY_NAME,
        "access_token": create_app_token(ACADEMY_NAME, row.name, row.phone_last4),
        "token_type": "bearer",
        "role": "user",
        "name": row.name,
        "phone_last4": row.phone_last4,
    }


@app.post("/api/v1/auth/admin-login")
def admin_app_login(payload: AdminLoginRequest, db: Session = Depends(get_db)):
    credential = get_admin_credential(db)
    if not verify_password(payload.password, credential.password_hash):
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")

    return {
        "academy_name": ACADEMY_NAME,
        "access_token": create_admin_app_token(ACADEMY_NAME, credential.password_hash),
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
    rooms = db.scalars(
        select(Room).where(Room.is_deleted.is_(False)).order_by(Room.name.asc())
    ).all()
    return {
        "academy_name": ACADEMY_NAME,
        "server_time": now_utc(),
        "booking_limit_months": 3,
        "user_name": auth.get("name"),
        "phone_last4": auth.get("phone_last4"),
        "rooms": [room_dict(r) for r in rooms],
    }


@app.get("/api/v1/rooms")
def list_rooms(request: Request, db: Session = Depends(get_db)):
    require_app_token(request)
    rooms = db.scalars(
        select(Room).where(Room.is_deleted.is_(False)).order_by(Room.name.asc())
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
    require_app_token(request)

    stmt = select(Reservation).where(Reservation.cancelled_at.is_(None))
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
    require_app_token(request)
    stmt = select(UnavailableBlock)
    if room_id is not None:
        stmt = stmt.where(UnavailableBlock.room_id == room_id)
    if from_at is not None:
        if from_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="from_at에는 시간대가 필요합니다.")
        stmt = stmt.where(UnavailableBlock.end_at > normalize_utc(from_at))
    if to_at is not None:
        if to_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="to_at에는 시간대가 필요합니다.")
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
            .where(Room.id == payload.room_id, Room.is_deleted.is_(False))
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
                UnavailableBlock.room_id == room.id,
                UnavailableBlock.start_at < end,
                UnavailableBlock.end_at > start,
            )
        )
        if conflict_block:
            raise HTTPException(status_code=409, detail="관리자가 예약불가로 지정한 시간입니다.")

        reservation = Reservation(
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
# Admin web login
# -----------------------------

@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request):
    try:
        require_admin(request)
        logged_in = True
    except HTTPException:
        logged_in = False

    if not logged_in:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "academy_name": ACADEMY_NAME,
                "login_error": request.query_params.get("error") == "1",
                "password_reset": request.query_params.get("reset") == "1",
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"academy_name": ACADEMY_NAME},
    )


@app.get("/admin/forgot", response_class=HTMLResponse, include_in_schema=False)
def admin_forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            "academy_name": ACADEMY_NAME,
            "error": request.query_params.get("error") == "1",
            "not_configured": request.query_params.get("not_configured") == "1",
        },
    )


@app.post("/admin/forgot/verify", include_in_schema=False)
def admin_forgot_password_verify(
    name: str = Form(...),
    phone_last4: str = Form(...),
    developer_name: str = Form(...),
):
    if not (
        ADMIN_RECOVERY_NAME
        and ADMIN_RECOVERY_PHONE_LAST4
        and ADMIN_RECOVERY_DEVELOPER_NAME
    ):
        return RedirectResponse(url="/admin/forgot?not_configured=1", status_code=303)

    supplied_name = name.strip()
    supplied_phone = phone_last4.strip()
    supplied_developer = developer_name.strip()

    matched = (
        hmac.compare_digest(
            supplied_name.encode("utf-8"),
            ADMIN_RECOVERY_NAME.encode("utf-8"),
        )
        and hmac.compare_digest(
            supplied_phone.encode("utf-8"),
            ADMIN_RECOVERY_PHONE_LAST4.encode("utf-8"),
        )
        and hmac.compare_digest(
            supplied_developer.encode("utf-8"),
            ADMIN_RECOVERY_DEVELOPER_NAME.encode("utf-8"),
        )
    )

    if not matched:
        return RedirectResponse(url="/admin/forgot?error=1", status_code=303)

    response = RedirectResponse(url="/admin/reset-password", status_code=303)
    response.set_cookie(
        "kbh_admin_recovery",
        create_admin_recovery_token(),
        httponly=True,
        secure=os.getenv("RENDER") == "true",
        samesite="strict",
        max_age=60 * 10,
    )
    return response


@app.get("/admin/reset-password", response_class=HTMLResponse, include_in_schema=False)
def admin_reset_password_page(request: Request):
    token = request.cookies.get("kbh_admin_recovery", "")
    if not token:
        return RedirectResponse(url="/admin/forgot", status_code=303)

    try:
        verify_admin_recovery_token(token)
    except HTTPException:
        response = RedirectResponse(url="/admin/forgot?error=1", status_code=303)
        response.delete_cookie("kbh_admin_recovery")
        return response

    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "academy_name": ACADEMY_NAME,
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
        verify_admin_recovery_token(token)
    except HTTPException:
        response = RedirectResponse(url="/admin/forgot?error=1", status_code=303)
        response.delete_cookie("kbh_admin_recovery")
        return response

    if len(new_password) < 4:
        return RedirectResponse(url="/admin/reset-password?error=short", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse(url="/admin/reset-password?error=mismatch", status_code=303)

    credential = get_admin_credential(db)
    credential.password_hash = hash_password(new_password)
    credential.updated_at = now_utc()
    db.add(credential)
    db.commit()

    with SessionLocal() as verify_db:
        saved = verify_db.get(AdminCredential, 1)
        if saved is None or not verify_password(new_password, saved.password_hash):
            return RedirectResponse(url="/admin/reset-password?error=save", status_code=303)

    response = RedirectResponse(url="/admin?reset=1", status_code=303)
    response.delete_cookie("kbh_admin_recovery")
    response.delete_cookie("kbh_admin")
    return response


@app.post("/admin/login", include_in_schema=False)
def admin_login(password: str = Form(...), db: Session = Depends(get_db)):
    credential = get_admin_credential(db)
    if not verify_password(password, credential.password_hash):
        return RedirectResponse(url="/admin?error=1", status_code=303)

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "kbh_admin",
        create_admin_session(credential.password_hash),
        httponly=True,
        secure=os.getenv("RENDER") == "true",
        samesite="strict",
        max_age=60 * 60 * 12,
    )
    return response


@app.get("/admin/app-login", include_in_schema=False)
def admin_app_web_login(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    credential = get_admin_credential(db)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "kbh_admin",
        create_admin_session(credential.password_hash),
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
# Admin API - web cookie OR admin app bearer token
# -----------------------------

@app.get("/api/admin/users")
def admin_list_users(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rows = db.scalars(select(AuthorizedUser).order_by(AuthorizedUser.name.asc(), AuthorizedUser.id.asc())).all()
    return [authorized_user_dict(row) for row in rows]


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: AuthorizedUserCreate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    row = AuthorizedUser(name=payload.name.strip(), phone_last4=payload.phone_last4)
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
    require_admin(request)
    row = db.get(AuthorizedUser, user_id)
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
    require_admin(request)
    credential = get_admin_credential(db)

    if not verify_password(payload.current_password, credential.password_hash):
        # 현재 비밀번호 불일치는 "로그인 세션 만료"가 아니므로 401을 쓰지 않는다.
        raise HTTPException(status_code=400, detail="현재 관리자 비밀번호가 올바르지 않습니다.")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=409, detail="새 비밀번호는 현재 비밀번호와 다르게 입력해 주세요.")

    credential.password_hash = hash_password(payload.new_password)
    credential.updated_at = now_utc()
    db.add(credential)
    db.commit()

    # 같은 SQLAlchemy 객체가 아니라 완전히 새 DB 세션으로 다시 읽어서
    # PostgreSQL에 실제로 저장됐는지 확인한다.
    with SessionLocal() as verify_db:
        saved = verify_db.get(AdminCredential, 1)
        if saved is None or not verify_password(payload.new_password, saved.password_hash):
            raise HTTPException(status_code=500, detail="관리자 비밀번호 변경을 저장하지 못했습니다.")

    # password hash가 바뀌었으므로 기존 웹 세션/앱 관리자 토큰은 이후 요청부터 자동 무효화된다.
    return {"ok": True, "logout_required": True}


@app.get("/api/admin/rooms")
def admin_list_rooms(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rooms = db.scalars(
        select(Room).where(Room.is_deleted.is_(False)).order_by(Room.name.asc())
    ).all()
    return [room_dict(r) for r in rooms]


@app.post("/api/admin/rooms", status_code=201)
def admin_create_room(payload: RoomCreate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    name = payload.name.strip()

    existing = db.scalar(select(Room).where(Room.name == name))
    if existing is not None:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.is_paused = False
            existing.pause_reason = None
            db.commit()
            db.refresh(existing)
            return room_dict(existing)
        raise HTTPException(status_code=409, detail="이미 같은 이름의 녹음실이 있습니다.")

    room = Room(name=name)
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
    require_admin(request)
    room = db.get(Room, room_id)
    if room is None or room.is_deleted:
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
    require_admin(request)
    room = db.get(Room, room_id)
    if room is None or room.is_deleted:
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
    require_admin(request)
    stmt = select(Reservation)
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
    require_admin(request)
    row = db.get(Reservation, reservation_id)
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
    require_admin(request)
    row = db.get(Reservation, reservation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="예약을 찾을 수 없습니다.")
    if row.cancelled_at is None:
        raise HTTPException(status_code=409, detail="취소된 예약만 삭제할 수 있습니다.")

    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/blocks")
def admin_list_blocks(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rows = db.scalars(select(UnavailableBlock).order_by(UnavailableBlock.start_at.desc())).all()
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
    require_admin(request)
    start = normalize_utc(payload.start_at)
    end = normalize_utc(payload.end_at)

    room = db.scalar(
        select(Room)
        .where(Room.id == payload.room_id, Room.is_deleted.is_(False))
        .with_for_update()
    )
    if room is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="녹음실을 찾을 수 없습니다.")

    existing_reservation = db.scalar(
        select(Reservation).where(
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
    require_admin(request)
    row = db.get(UnavailableBlock, block_id)
    if row is None:
        raise HTTPException(status_code=404, detail="예약불가 항목을 찾을 수 없습니다.")
    db.delete(row)
    db.commit()
    return {"ok": True}
