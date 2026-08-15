from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


def validate_last4_value(value: str) -> str:
    value = value.strip()
    if len(value) != 4 or not value.isdigit():
        raise ValueError("전화번호 끝 4자리는 숫자 4자리여야 합니다.")
    return value


class UserLoginRequest(BaseModel):
    academy_id: int
    name: str = Field(min_length=1, max_length=40)
    phone_last4: str

    @field_validator("phone_last4")
    @classmethod
    def validate_last4(cls, value: str) -> str:
        return validate_last4_value(value)


class AdminLoginRequest(BaseModel):
    academy_id: int
    password: str = Field(min_length=1, max_length=128)


class AcademyRegistrationVerifyRequest(BaseModel):
    license_key: str = Field(min_length=1, max_length=200)


class AcademyCreateRequest(BaseModel):
    registration_token: str = Field(min_length=1)
    academy_name: str = Field(min_length=1, max_length=100)
    recovery_name: str = Field(min_length=1, max_length=40)
    recovery_phone_last4: str
    admin_password: str = Field(min_length=4, max_length=128)

    @field_validator("recovery_phone_last4")
    @classmethod
    def validate_recovery_last4(cls, value: str) -> str:
        return validate_last4_value(value)


class AcademyManagementRequest(BaseModel):
    registration_token: str = Field(min_length=1)


class AcademyDeleteRequest(BaseModel):
    registration_token: str = Field(min_length=1)
    academy_id: int


class AuthorizedUserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    phone_last4: str
    category_id: int | None = None
    booking_limit_hours: int | None = Field(default=None, ge=1, le=24)
    allow_additional_booking: bool = False

    @field_validator("phone_last4")
    @classmethod
    def validate_last4(cls, value: str) -> str:
        return validate_last4_value(value)


class AuthorizedUserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    phone_last4: str | None = None
    category_id: int | None = None
    booking_limit_hours: int | None = Field(default=None, ge=1, le=24)
    allow_additional_booking: bool | None = None

    @field_validator("phone_last4")
    @classmethod
    def validate_optional_last4(cls, value: str | None) -> str | None:
        return validate_last4_value(value) if value is not None else None


class MemberCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class MemberCategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=4, max_length=128)


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    open_hour: int = Field(default=0, ge=0, le=23)
    close_hour: int = Field(default=24, ge=1, le=24)

    @model_validator(mode="after")
    def validate_hours(self):
        if self.close_hour <= self.open_hour:
            raise ValueError("마감시간은 오픈시간보다 뒤여야 합니다.")
        return self


class RoomUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    is_paused: bool | None = None
    pause_reason: str | None = Field(default=None, max_length=200)
    open_hour: int | None = Field(default=None, ge=0, le=23)
    close_hour: int | None = Field(default=None, ge=1, le=24)


class ReservationCreate(BaseModel):
    room_id: int
    nickname: str = Field(min_length=1, max_length=40)
    phone_last4: str
    start_at: datetime
    end_at: datetime

    @field_validator("phone_last4")
    @classmethod
    def validate_last4(cls, value: str) -> str:
        return validate_last4_value(value)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("예약 시간에는 시간대(UTC offset)가 포함되어야 합니다.")
        if self.end_at <= self.start_at:
            raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
        return self


class BlockCreate(BaseModel):
    room_id: int
    start_at: datetime
    end_at: datetime
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("예약불가 시간에는 시간대가 포함되어야 합니다.")
        if self.end_at <= self.start_at:
            raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
        return self


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class ReservationMoveRequest(BaseModel):
    room_id: int


class AdminReservationUpdate(BaseModel):
    room_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None

    @model_validator(mode="after")
    def validate_optional_range(self):
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("예약시간 수정 시 시작시간과 종료시간을 함께 입력해 주세요.")
        if self.start_at is not None and self.end_at is not None:
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
                raise ValueError("예약 시간에는 시간대(UTC offset)가 포함되어야 합니다.")
            if self.end_at <= self.start_at:
                raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
        return self
