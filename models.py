import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Academy(Base):
    __tablename__ = "academies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    recovery_name: Mapped[str] = mapped_column(String(40), nullable=False)
    recovery_phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Room(Base):
    __tablename__ = "academy_rooms"
    __table_args__ = (
        UniqueConstraint("academy_id", "name", name="uq_academy_room_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Reservation(Base):
    __tablename__ = "academy_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("academy_rooms.id"), index=True, nullable=False)
    nickname: Mapped[str] = mapped_column(String(40), nullable=False)
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UnavailableBlock(Base):
    __tablename__ = "academy_unavailable_blocks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("academy_rooms.id"), index=True, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuthorizedUser(Base):
    __tablename__ = "academy_authorized_users"
    __table_args__ = (
        UniqueConstraint("academy_id", "name", "phone_last4", name="uq_academy_authorized_user_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AdminCredential(Base):
    __tablename__ = "academy_admin_credentials"

    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

class MemberCategory(Base):
    __tablename__ = "academy_member_categories"
    __table_args__ = (
        UniqueConstraint("academy_id", "name", name="uq_academy_member_category_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemberPolicy(Base):
    __tablename__ = "academy_member_policies"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("academy_authorized_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("academy_member_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    booking_limit_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_additional_booking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class RoomSchedule(Base):
    __tablename__ = "academy_room_schedules"

    room_id: Mapped[int] = mapped_column(
        ForeignKey("academy_rooms.id", ondelete="CASCADE"),
        primary_key=True,
    )
    academy_id: Mapped[int] = mapped_column(ForeignKey("academies.id", ondelete="CASCADE"), nullable=False, index=True)
    open_hour: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    close_hour: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

