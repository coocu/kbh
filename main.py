\
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth_adapter import AuthNotConfigured, verify_license_key
from db import Base, SessionLocal, engine
from models import Reservation, Room, UnavailableBlock
from schemas import BlockCreate, CancelRequest, LicenseRequest, ReservationCreate, RoomCreate, RoomUpdate
from security import create_admin_session, create_app_token, require_admin, require_app_token

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ACADEMY_NAME = os.getenv("ACADEMY_NAME", "킴스보컬미디학원")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def reservation_status(r: Reservation, now: datetime | None = None) -> str:
    now = now or now_utc()
    if r.cancelled_at is not None:
        return "취소됨"
    if now < r.start_at:
        return "예약중"
    if r.start_at <= now < r.end_at:
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # For first deployment/prototype. Later we can switch to Alembic migrations.
    Base.metadata.create_all(bind=engine)

    if os.getenv("RENDER") == "true":
        if not os.getenv("ADMIN_SESSION_SECRET"):
            raise RuntimeError("Render production requires ADMIN_SESSION_SECRET.")
        if not os.getenv("WEB_ADMIN_PASSWORD"):
            raise RuntimeError("Render production requires WEB_ADMIN_PASSWORD.")
        if os.getenv("DATABASE_URL", "").startswith("sqlite"):
            raise RuntimeError("Render production must use Postgres DATABASE_URL, not local SQLite.")

    yield


app = FastAPI(
    title="킴스보컬미디학원 예약 서버",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"ok": True, "service": "kbh-reservation-api"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


# -----------------------------
# App authentication
# -----------------------------

@app.post("/api/v1/auth/register")
async def register_app(payload: LicenseRequest):
    try:
        verified = await verify_license_key(payload.license_key.strip())
    except AuthNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not verified:
        raise HTTPException(status_code=401, detail="유효하지 않은 인증키입니다.")

    return {
        "academy_name": ACADEMY_NAME,
        "access_token": create_app_token(ACADEMY_NAME),
        "token_type": "bearer",
    }


# -----------------------------
# Public iOS app API
# -----------------------------

@app.get("/api/v1/bootstrap")
def bootstrap(request: Request, db: Session = Depends(get_db)):
    require_app_token(request)
    rooms = db.scalars(
        select(Room).where(Room.is_deleted.is_(False)).order_by(Room.name.asc())
    ).all()
    return {
        "academy_name": ACADEMY_NAME,
        "server_time": now_utc(),
        "booking_limit_months": 3,
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
    require_app_token(request)

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
            nickname=payload.nickname.strip(),
            phone_last4=payload.phone_last4,
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
            context={"academy_name": ACADEMY_NAME},
        )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"academy_name": ACADEMY_NAME},
    )


@app.post("/admin/login", include_in_schema=False)
def admin_login(password: str = Form(...)):
    expected = os.getenv("WEB_ADMIN_PASSWORD", "")
    if not expected or password != expected:
        return RedirectResponse(url="/admin?error=1", status_code=303)

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "kbh_admin",
        create_admin_session(),
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
# Admin API
# -----------------------------

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
