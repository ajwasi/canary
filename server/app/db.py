"""Database models. Times are stored as naive UTC."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_args, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="viewer")      # viewer | manager | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(300))          # encrypted
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    buttons: Mapped[list["Button"]] = relationship(back_populates="person")


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(300), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    buttons: Mapped[list["Button"]] = relationship(back_populates="location")


class Button(Base):
    __tablename__ = "buttons"
    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str | None] = mapped_column(String(20), unique=True)   # set at enrolment
    name: Mapped[str] = mapped_column(String(100))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    claim_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    claim_expires: Mapped[datetime | None] = mapped_column(DateTime)
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime)
    epoch: Mapped[int] = mapped_column(Integer, default=0)                # +1 at every enrolment
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_press_at: Mapped[datetime | None] = mapped_column(DateTime)
    fw: Mapped[str] = mapped_column(String(40), default="")
    target_fw: Mapped[str] = mapped_column(String(40), default="")
    rssi: Mapped[int | None] = mapped_column(Integer)
    channel: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str] = mapped_column(String(45), default="")
    cert_not_after: Mapped[datetime | None] = mapped_column(DateTime)
    audio_sha: Mapped[str] = mapped_column(String(64), default="")
    target_audio_sha: Mapped[str] = mapped_column(String(64), default="")
    nc_fault: Mapped[bool] = mapped_column(Boolean, default=False)
    pending: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    person: Mapped[Person | None] = relationship(back_populates="buttons")
    location: Mapped[Location | None] = relationship(back_populates="buttons")


class Press(Base):
    __tablename__ = "presses"
    __table_args__ = (UniqueConstraint("button_id", "epoch", "seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("buttons.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    pressed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approximate: Mapped[bool] = mapped_column(Boolean, default=False)     # device clock was unknown
    # Who and where the button belonged to when it was pressed.
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), index=True)
    button: Mapped[Button] = relationship()
    person: Mapped[Person | None] = relationship()
    location: Mapped[Location | None] = relationship()

    @property
    def delay_s(self) -> float | None:
        """Seconds from the press to the server receiving it; None if the press time is only approximate."""
        return None if self.approximate else max(0.0, (self.received_at - self.pressed_at).total_seconds())


class Firmware(Base):
    __tablename__ = "firmware"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40), unique=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)           # unguessable download name
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AudioClip(Base):
    __tablename__ = "audio_clips"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(300))


def audit(db, actor: str, action: str) -> None:
    db.add(Audit(actor=actor, action=action[:300]))
