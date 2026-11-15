"""Web UI, device enrolment API and file downloads."""
import csv
import hashlib
import io
import logging
import secrets
import threading
import time
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, available_timezones

import pyotp
import segno
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session as Db

from . import ca, chart, mqtt, security
from .config import settings
from .db import (AudioClip, Audit, Base, Button, Firmware, Location, Person, Press, Session, SessionLocal, User,
                 audit, engine, get_db, utcnow)
from .security import LoginRequired, need

log = logging.getLogger("app")
HERE = Path(__file__).parent
FILES = settings.data_dir / "files"
MAX_FIRMWARE = 0x300000            # size of one firmware slot on the device
MAX_AUDIO = 1500 * 1024
PAGE = 100
ROLES = ["viewer", "manager", "admin"]
DUMMY_HASH = security.hash_password("not-a-real-password")
TZ_NAMES = sorted(available_timezones())


# ---------- startup ----------

def bootstrap() -> None:
    Base.metadata.create_all(engine)
    ca.ensure_ca()
    for sub in ("fw", "audio"):
        (FILES / sub).mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        if db.query(User).count() == 0:
            if settings.admin_password:
                db.add(User(email=settings.admin_email.lower(), name="Administrator", role="admin",
                            password_hash=security.hash_password(settings.admin_password), must_change=False))
                db.commit()
            else:
                print("\n*** There are no users yet. Create the first administrator with:\n"
                      "***     python -m app.create_admin\n"
                      "*** (in Docker: docker compose exec app python -m app.create_admin)\n", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    mqtt.start()
    yield
    mqtt.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- helpers ----------

def localtime(dt: datetime | None, tzname: str | None = None) -> str:
    if not dt:
        return "never"
    try:
        tz = ZoneInfo(tzname or settings.default_tz)
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = max(0, int((utcnow() - dt).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def press_time(p: Press) -> str:
    """A press is shown in the time zone of the location it was made at."""
    return localtime(p.pressed_at, p.location.timezone if p.location else None)


templates.env.filters.update(localtime=localtime, ago=ago, press_time=press_time, delay=chart.fmt_delay)


# One-time messages ("Button added. Claim code: ...") wait here for the next page. The browser
# holds only a random id, so a claim code or temporary password is never stored in a cookie.
# Like the rest of the app this assumes a single server process.
FLASH_SECONDS = 60
_flash: dict[str, tuple[float, str]] = {}
_flash_lock = threading.Lock()


def _take_flash(token: str | None) -> str | None:
    now = time.monotonic()
    with _flash_lock:
        for key in [k for k, (expires, _) in _flash.items() if expires < now]:
            del _flash[key]
        entry = _flash.pop(token, None) if token else None
    return entry[1] if entry else None


def render(request: Request, name: str, **ctx):
    token = request.cookies.get("flash")
    ctx.update(user=getattr(request.state, "user", None), csrf=getattr(request.state, "csrf", ""),
               flash=_take_flash(token), path=request.url.path)
    resp = templates.TemplateResponse(request, name, ctx)
    if token:
        resp.delete_cookie("flash")
    return resp


def local_path(url: str) -> str:
    """Only paths on this site are ever redirected to; anything else becomes the home page."""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not url.startswith("/") or url.startswith("//") or "\\" in url \
            or any(ord(c) < 32 for c in url):
        return "/"
    return url


def go(url: str, msg: str | None = None):
    resp = RedirectResponse(local_path(url), 303)
    if msg:
        token = secrets.token_urlsafe(16)
        with _flash_lock:
            _flash[token] = (time.monotonic() + FLASH_SECONDS, msg)
        resp.set_cookie("flash", token, max_age=FLASH_SECONDS, httponly=True, samesite="lax",
                        secure=settings.cookie_secure)
    return resp


def opt_int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def get_or_404(db: Db, model, item_id: int):
    item = db.get(model, item_id)
    if not item:
        raise HTTPException(404, "Not found.")
    return item


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (303, 307) and exc.headers:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "detail": exc.detail},
                                      status_code=exc.status_code)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- sign-in ----------

@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), code: str = Form(""),
          db: Db = Depends(get_db)):
    user = db.query(User).filter_by(email=email.strip().lower()).first()
    now = utcnow()
    locked = bool(user and user.locked_until and user.locked_until > now)
    ok = security.verify_password(user.password_hash if user else DUMMY_HASH, password)
    ok = ok and user is not None and user.active and not locked
    if ok and user.totp_enabled:
        ok = security.totp_ok(user, code)
    if not ok:
        if user and not locked:
            user.failed_logins += 1
            if user.failed_logins >= security.MAX_FAILED:
                user.locked_until = now + timedelta(minutes=security.LOCK_MINUTES)
                user.failed_logins = 0
                audit(db, user.email, "account locked after repeated failed sign-ins")
            db.commit()
        return render(request, "login.html", error="Email, password or code not accepted.", email=email)
    user.failed_logins, user.locked_until = 0, None
    token = security.start_session(db, user)
    db.query(Session).filter(Session.expires_at < now).delete()
    audit(db, user.email, "signed in")
    db.commit()
    resp = RedirectResponse("/", 303)
    resp.set_cookie(security.COOKIE, token, max_age=settings.session_hours * 3600, httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@app.post("/logout")
def logout(request: Request, user: User = Depends(need()), db: Db = Depends(get_db)):
    db.query(Session).filter_by(token_hash=security.sha256_hex(request.cookies.get(security.COOKIE, ""))).delete()
    db.commit()
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(security.COOKIE)
    return resp


# ---------- overview ----------

@app.get("/")
def overview(request: Request, by: str = "person", user: User = Depends(need()), db: Db = Depends(get_db)):
    by = "location" if by == "location" else "person"
    owners = db.query(Location if by == "location" else Person).filter_by(archived=False).all()
    owners.sort(key=lambda o: o.name.lower())
    buttons = db.query(Button).order_by(Button.name).all()
    since = utcnow() - timedelta(hours=24)
    counts = dict(db.query(Press.button_id, func.count()).filter(Press.pressed_at >= since)
                  .group_by(Press.button_id).all())
    key = (lambda b: b.location_id) if by == "location" else (lambda b: b.person_id)
    groups = [(o, [b for b in buttons if key(b) == o.id]) for o in owners]
    loose = [b for b in buttons if key(b) is None]
    return render(request, "overview.html", by=by, groups=groups, loose=loose, counts=counts)


# ---------- activity ----------

def press_query(db: Db, person: int | None, location: int | None, button: int | None, start: str, end: str):
    q = db.query(Press)
    if person:
        q = q.filter(Press.person_id == person)
    if location:
        q = q.filter(Press.location_id == location)
    if button:
        q = q.filter(Press.button_id == button)
    tz = ZoneInfo(settings.default_tz)
    try:
        if start:
            q = q.filter(Press.pressed_at >= datetime.fromisoformat(start).replace(tzinfo=tz)
                         .astimezone(timezone.utc).replace(tzinfo=None))
        if end:
            q = q.filter(Press.pressed_at < (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=tz)
                         .astimezone(timezone.utc).replace(tzinfo=None))
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-11-14.")
    return q.order_by(Press.pressed_at.desc())


@app.get("/activity")
def activity(request: Request, person: str = "", location: str = "", button: str = "", start: str = "",
             end: str = "", group: str = "", page: int = 1, user: User = Depends(need()),
             db: Db = Depends(get_db)):
    q = press_query(db, opt_int(person), opt_int(location), opt_int(button), start, end)
    total = q.count()
    rows = [tuple(r) for r in q.with_entities(Press.pressed_at, Press.received_at, Press.approximate)
            .order_by(None).limit(200000)]
    group = group if group in chart.GROUPS else ""
    page = max(1, page)
    presses = q.offset((page - 1) * PAGE).limit(PAGE).all()
    filters = {k: v for k, v in dict(person=person, location=location, button=button, start=start, end=end,
                                     group=group).items() if v}
    keep = {k: v for k, v in filters.items() if k not in ("start", "end")}
    presets = [(label, urlencode({**keep, **({"start": a, "end": b} if a else {})}), (a, b) == (start, end))
               for label, a, b in chart.presets(settings.default_tz)]
    return render(request, "activity.html", presses=presses, total=total, page=page, pages=max(1, -(-total // PAGE)),
                  filters=filters, query=urlencode(filters), tz=settings.default_tz, presets=presets,
                  groups=chart.GROUPS, chart=chart.build(rows, start, end, group, settings.default_tz),
                  people=db.query(Person).order_by(Person.name).all(),
                  locations=db.query(Location).order_by(Location.name).all(),
                  buttons=db.query(Button).order_by(Button.name).all())


@app.get("/activity.csv")
def activity_csv(person: str = "", location: str = "", button: str = "", start: str = "", end: str = "",
                 user: User = Depends(need()), db: Db = Depends(get_db)):
    rows = press_query(db, opt_int(person), opt_int(location), opt_int(button), start, end).limit(100000).all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["pressed_at_utc", "pressed_at_local", "button", "serial", "person", "location", "received_at_utc",
                "delay_seconds", "time_approximate"])
    for p in rows:
        w.writerow([p.pressed_at.isoformat(sep=" ", timespec="milliseconds"), press_time(p), p.button.name,
                    p.button.serial or "", p.person.name if p.person else "", p.location.name if p.location else "",
                    p.received_at.isoformat(sep=" ", timespec="milliseconds"),
                    "" if p.delay_s is None else f"{p.delay_s:.3f}", "yes" if p.approximate else ""])
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=presses.csv"})


# ---------- buttons ----------

def set_claim(button: Button) -> str:
    code = security.new_claim_code()
    button.claim_hash = security.sha256_hex(security.normalise_claim(code))
    button.claim_expires = utcnow() + timedelta(days=7)
    return code


def owner_choices(db: Db) -> dict:
    return dict(people=db.query(Person).filter_by(archived=False).order_by(Person.name).all(),
                locations=db.query(Location).filter_by(archived=False).order_by(Location.name).all())


@app.get("/buttons")
def buttons(request: Request, user: User = Depends(need()), db: Db = Depends(get_db)):
    return render(request, "buttons.html", buttons=db.query(Button).order_by(Button.name).all(), **owner_choices(db))


@app.post("/buttons")
def button_create(name: str = Form(""), person_id: str = Form(""), location_id: str = Form(""),
                  user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    if not name.strip():
        raise HTTPException(400, "A name is required.")
    b = Button(name=name.strip()[:100], person_id=opt_int(person_id), location_id=opt_int(location_id))
    code = set_claim(b)
    db.add(b)
    audit(db, user.email, f"added button {b.name}")
    db.commit()
    return go(f"/buttons/{b.id}", f"Button added. Claim code: {code}. It is shown only once and is valid for 7 days.")


@app.get("/buttons/{button_id}")
def button_detail(request: Request, button_id: int, user: User = Depends(need()), db: Db = Depends(get_db)):
    b = get_or_404(db, Button, button_id)
    presses = db.query(Press).filter_by(button_id=b.id).order_by(Press.pressed_at.desc()).limit(50).all()
    return render(request, "button.html", b=b, presses=presses, now=utcnow(), cert_days=settings.cert_days,
                  **owner_choices(db))


@app.post("/buttons/{button_id}")
def button_update(button_id: int, name: str = Form(""), person_id: str = Form(""), location_id: str = Form(""),
                  user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    b = get_or_404(db, Button, button_id)
    if not name.strip():
        raise HTTPException(400, "A name is required.")
    renamed = name.strip()[:100] != b.name
    b.name, b.person_id, b.location_id = name.strip()[:100], opt_int(person_id), opt_int(location_id)
    audit(db, user.email, f"edited button {b.name} (person {b.person_id}, location {b.location_id})")
    db.commit()
    if renamed and b.serial:
        mqtt.publish(b.serial, "name", {"name": b.name[:32]})
    return go(f"/buttons/{b.id}", "Saved. New presses are recorded against this person and location.")


@app.post("/buttons/{button_id}/{action}")
def button_action(button_id: int, action: str, user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    b = get_or_404(db, Button, button_id)
    msg = "Done."
    if action == "claim":
        msg = f"New claim code: {set_claim(b)}. It is shown only once and is valid for 7 days."
    elif action == "revoke":
        b.revoked, b.online, b.claim_hash = True, False, None
        msg = "Button revoked. Its presses are ignored from now on and its certificate will not be renewed."
    elif action == "restore":
        b.revoked = False
        msg = "Button restored."
    elif action in ("identify", "reboot") and b.serial:
        mqtt.publish(b.serial, action, {})
        msg = "Command sent." if b.online else "Command sent, but the button is offline and will not receive it."
    elif action == "delete":
        if db.query(Press).filter_by(button_id=b.id).count():
            raise HTTPException(400, "This button has recorded presses. Revoke it instead of deleting it.")
        db.delete(b)
        audit(db, user.email, f"deleted button {b.name}")
        db.commit()
        return go("/buttons", "Button deleted.")
    else:
        raise HTTPException(404, "Not found.")
    audit(db, user.email, f"button {b.name}: {action}")
    db.commit()
    return go(f"/buttons/{b.id}", msg)


# ---------- firmware and sound updates ----------

async def read_upload(file: UploadFile, limit: int) -> bytes:
    data = await file.read(limit + 1)
    if not data:
        raise HTTPException(400, "Choose a file.")
    if len(data) > limit:
        raise HTTPException(400, f"File is larger than the {limit // 1024} KB limit.")
    return data


@app.get("/updates")
def updates(request: Request, user: User = Depends(need()), db: Db = Depends(get_db)):
    return render(request, "updates.html",
                  firmware=db.query(Firmware).order_by(Firmware.uploaded_at.desc()).all(),
                  clips=db.query(AudioClip).order_by(AudioClip.uploaded_at.desc()).all(),
                  buttons=db.query(Button).filter(Button.serial.isnot(None), Button.revoked.is_(False))
                  .order_by(Button.name).all())


@app.post("/updates/firmware")
async def firmware_upload(file: UploadFile, version: str = Form(""), user: User = Depends(need("manager")),
                          db: Db = Depends(get_db)):
    version = version.strip()
    if not version or len(version) > 31 or db.query(Firmware).filter_by(version=version).first():
        raise HTTPException(400, "Enter a version that has not been uploaded before (31 characters at most).")
    data = await read_upload(file, MAX_FIRMWARE)
    if data[0] != 0xE9:
        raise HTTPException(400, "This is not an ESP32 application image (.bin).")
    fw = Firmware(version=version, token=secrets.token_urlsafe(24), sha256=hashlib.sha256(data).hexdigest(),
                  size=len(data))
    (FILES / "fw" / f"{fw.token}.bin").write_bytes(data)
    db.add(fw)
    audit(db, user.email, f"uploaded firmware {version}")
    db.commit()
    return go("/updates", f"Firmware {version} uploaded.")


@app.post("/updates/audio")
async def audio_upload(file: UploadFile, name: str = Form(""), user: User = Depends(need("manager")),
                       db: Db = Depends(get_db)):
    data = await read_upload(file, MAX_AUDIO)
    try:
        with wave.open(io.BytesIO(data)) as w:
            ok = w.getsampwidth() == 2 and w.getnchannels() in (1, 2) and 8000 <= w.getframerate() <= 48000
    except (wave.Error, EOFError):
        ok = False
    if not ok:
        raise HTTPException(400, "The sound must be a WAV file: 16-bit PCM, mono or stereo, 8 to 48 kHz.")
    clip = AudioClip(name=(name.strip() or file.filename or "Sound")[:100], token=secrets.token_urlsafe(24),
                     sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    (FILES / "audio" / f"{clip.token}.wav").write_bytes(data)
    db.add(clip)
    audit(db, user.email, f"uploaded sound {clip.name}")
    db.commit()
    return go("/updates", f"Sound {clip.name} uploaded.")


@app.post("/updates/push")
async def push(request: Request, user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    form = await request.form()
    kind, item_id = form.get("kind"), opt_int(str(form.get("item", "")))
    ids = [int(v) for v in form.getlist("targets") if str(v).isdigit()]
    targets = db.query(Button).filter(Button.id.in_(ids), Button.serial.isnot(None), Button.revoked.is_(False)).all()
    if not targets:
        raise HTTPException(400, "Select at least one button.")
    if kind == "firmware":
        fw = get_or_404(db, Firmware, item_id or 0)
        for b in targets:
            b.target_fw = fw.version
            mqtt.publish(b.serial, "ota", {"version": fw.version, "url": f"{settings.public_url}/files/fw/{fw.token}.bin"},
                         retain=True)
        what = f"firmware {fw.version}"
    elif kind == "audio":
        clip = get_or_404(db, AudioClip, item_id or 0)
        for b in targets:
            b.target_audio_sha = clip.sha256
            mqtt.publish(b.serial, "audio", {"sha256": clip.sha256,
                                             "url": f"{settings.public_url}/files/audio/{clip.token}.wav"}, retain=True)
        what = f"sound {clip.name}"
    else:
        raise HTTPException(400, "Unknown update type.")
    audit(db, user.email, f"sent {what} to {len(targets)} button(s)")
    db.commit()
    return go("/updates", f"Sent {what} to {len(targets)} button(s). Offline buttons update when they reconnect.")


@app.post("/updates/{kind}/{item_id}/delete")
def update_delete(kind: str, item_id: int, user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    if kind == "firmware":
        item = get_or_404(db, Firmware, item_id)
        path, label = FILES / "fw" / f"{item.token}.bin", f"firmware {item.version}"
    elif kind == "audio":
        item = get_or_404(db, AudioClip, item_id)
        path, label = FILES / "audio" / f"{item.token}.wav", f"sound {item.name}"
    else:
        raise HTTPException(404, "Not found.")
    path.unlink(missing_ok=True)
    db.delete(item)
    audit(db, user.email, f"deleted {label}")
    db.commit()
    return go("/updates", f"Deleted {label}.")


@app.get("/files/fw/{name}")
def firmware_file(name: str, db: Db = Depends(get_db)):
    fw = db.query(Firmware).filter_by(token=name.removesuffix(".bin")).first()
    if not fw:
        raise HTTPException(404, "Not found.")
    return FileResponse(FILES / "fw" / f"{fw.token}.bin", media_type="application/octet-stream")


@app.get("/files/audio/{name}")
def audio_file(name: str, db: Db = Depends(get_db)):
    clip = db.query(AudioClip).filter_by(token=name.removesuffix(".wav")).first()
    if not clip:
        raise HTTPException(404, "Not found.")
    return FileResponse(FILES / "audio" / f"{clip.token}.wav", media_type="audio/wav")


# ---------- users ----------

def check_new_password(password: str) -> None:
    if len(password) < security.MIN_PASSWORD:
        raise HTTPException(400, f"The password must be at least {security.MIN_PASSWORD} characters.")


@app.get("/users")
def users(request: Request, user: User = Depends(need("admin")), db: Db = Depends(get_db)):
    return render(request, "users.html", users=db.query(User).order_by(User.email).all(), roles=ROLES,
                  log=db.query(Audit).order_by(Audit.at.desc()).limit(100).all())


@app.post("/users")
def user_create(email: str = Form(""), name: str = Form(""), role: str = Form("viewer"),
                user: User = Depends(need("admin")), db: Db = Depends(get_db)):
    email = email.strip().lower()
    if "@" not in email or role not in ROLES or db.query(User).filter_by(email=email).first():
        raise HTTPException(400, "Enter a new, valid email address and a role.")
    password = security.temp_password()
    db.add(User(email=email, name=name.strip()[:100], role=role, password_hash=security.hash_password(password),
                must_change=True))
    audit(db, user.email, f"added user {email} as {role}")
    db.commit()
    return go("/users", f"User added. Temporary password for {email}: {password}. It is shown only once.")


@app.post("/users/{user_id}")
def user_update(user_id: int, role: str = Form("viewer"), active: str = Form(""), reset: str = Form(""),
                user: User = Depends(need("admin")), db: Db = Depends(get_db)):
    target = get_or_404(db, User, user_id)
    if role not in ROLES:
        raise HTTPException(400, "Unknown role.")
    if target.id == user.id and (role != "admin" or active != "1"):
        raise HTTPException(400, "You cannot remove your own administrator access.")
    msg = "Saved."
    target.role, target.active = role, active == "1"
    if reset == "1":
        password = security.temp_password()
        target.password_hash, target.must_change = security.hash_password(password), True
        target.totp_enabled, target.totp_secret, target.locked_until = False, None, None
        msg = f"Temporary password for {target.email}: {password}. It is shown only once."
    if reset == "1" or not target.active:
        db.query(Session).filter(Session.user_id == target.id, Session.user_id != user.id).delete()
    audit(db, user.email, f"updated user {target.email}: role {role}, active {target.active}, reset {reset == '1'}")
    db.commit()
    return go("/users", msg)


# ---------- own account ----------

@app.get("/account")
def account(request: Request, user: User = Depends(need()), db: Db = Depends(get_db)):
    qr = uri = None
    if user.totp_secret and not user.totp_enabled:
        secret = security.decrypt(user.totp_secret)
        if secret:
            uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Button Notifier")
            qr = segno.make(uri).svg_inline(scale=4, border=2)
    return render(request, "account.html", qr=qr, uri=uri, min_password=security.MIN_PASSWORD)


@app.post("/account/password")
def account_password(request: Request, current: str = Form(""), new: str = Form(""),
                     user: User = Depends(need()), db: Db = Depends(get_db)):
    if not security.verify_password(user.password_hash, current):
        raise HTTPException(400, "The current password is not correct.")
    check_new_password(new)
    me = db.get(User, user.id)
    me.password_hash, me.must_change = security.hash_password(new), False
    keep = security.sha256_hex(request.cookies.get(security.COOKIE, ""))
    db.query(Session).filter(Session.user_id == me.id, Session.token_hash != keep).delete()
    audit(db, me.email, "changed own password")
    db.commit()
    return go("/account", "Password changed.")


@app.post("/account/totp/{action}")
def account_totp(action: str, code: str = Form(""), password: str = Form(""), user: User = Depends(need()),
                 db: Db = Depends(get_db)):
    me = db.get(User, user.id)
    if action == "start" and not me.totp_enabled:
        me.totp_secret = security.encrypt(pyotp.random_base32())
        msg = "Scan the code with your authenticator app, then enter the 6-digit code to finish."
    elif action == "enable" and me.totp_secret and not me.totp_enabled:
        if not security.totp_ok(me, code):
            raise HTTPException(400, "That code was not accepted. Check the time on your phone and try again.")
        me.totp_enabled = True
        msg = "Two-step sign-in is on."
    elif action == "disable":
        if not security.verify_password(me.password_hash, password):
            raise HTTPException(400, "The password is not correct.")
        me.totp_enabled, me.totp_secret = False, None
        msg = "Two-step sign-in is off."
    else:
        raise HTTPException(400, "Not available.")
    audit(db, me.email, f"two-step sign-in: {action}")
    db.commit()
    return go("/account", msg)


# ---------- device API ----------

class EnrollRequest(BaseModel):
    serial: str
    claim: str
    csr: str
    name: str = ""


_attempts: dict[str, list[float]] = {}


def throttle(ip: str, limit: int = 10, window: int = 60) -> None:
    now = time.monotonic()
    if len(_attempts) > 10000:
        _attempts.clear()
    recent = [t for t in _attempts.get(ip, []) if now - t < window]
    recent.append(now)
    _attempts[ip] = recent
    if len(recent) > limit:
        raise HTTPException(429, "Too many attempts.")


@app.post("/api/device/enroll")
def enroll(body: EnrollRequest, request: Request, db: Db = Depends(get_db)):
    throttle(request.client.host if request.client else "?")
    now = utcnow()
    b = db.query(Button).filter_by(claim_hash=security.sha256_hex(security.normalise_claim(body.claim))).first()
    if not b or b.revoked or not b.claim_expires or b.claim_expires < now:
        raise HTTPException(403, "Claim code not valid.")
    if db.query(Button).filter(Button.serial == body.serial, Button.id != b.id).first():
        raise HTTPException(403, "This unit is already registered as another button.")
    try:
        cert, expires = ca.sign_csr(body.csr, body.serial)
    except ca.CsrError as exc:
        raise HTTPException(403, f"Certificate request refused: {exc}.")
    b.serial, b.claim_hash, b.claim_expires = body.serial, None, None
    b.enrolled_at, b.cert_not_after, b.epoch = now, expires, b.epoch + 1
    audit(db, body.serial, f"enrolled as button {b.name}")
    db.commit()
    return {"cert": cert}


# ---------- people and locations (same pages, different fields) ----------
# Keep these last: "/{kind}" would otherwise shadow the fixed paths above.

KINDS = {
    "people": dict(model=Person, title="People", one="person", press_col=Press.person_id, filter_key="person"),
    "locations": dict(model=Location, title="Locations", one="location", press_col=Press.location_id,
                      filter_key="location"),
}


def kind_or_404(kind: str) -> dict:
    if kind not in KINDS:
        raise HTTPException(404, "Not found.")
    return KINDS[kind]


def apply_owner_form(item, kind: str, name: str, notes: str, address: str, tz: str) -> None:
    name = name.strip()
    if not name:
        raise HTTPException(400, "A name is required.")
    item.name, item.notes = name[:100], notes.strip()
    if kind == "locations":
        if tz and tz not in TZ_NAMES:
            raise HTTPException(400, "Unknown time zone.")
        item.address, item.timezone = address.strip()[:300], tz


@app.get("/{kind}")
def owners(request: Request, kind: str, user: User = Depends(need()), db: Db = Depends(get_db)):
    k = kind_or_404(kind)
    items = db.query(k["model"]).order_by(k["model"].archived, k["model"].name).all()
    last = dict(db.query(k["press_col"], func.max(Press.pressed_at)).group_by(k["press_col"]).all())
    return render(request, "owners.html", kind=kind, k=k, items=items, last=last, tz_names=TZ_NAMES)


@app.post("/{kind}")
def owner_create(kind: str, name: str = Form(""), notes: str = Form(""), address: str = Form(""),
                 timezone_name: str = Form(""), user: User = Depends(need("manager")), db: Db = Depends(get_db)):
    k = kind_or_404(kind)
    item = k["model"]()
    apply_owner_form(item, kind, name, notes, address, timezone_name)
    db.add(item)
    audit(db, user.email, f"added {k['one']} {item.name}")
    db.commit()
    return go(f"/{kind}/{item.id}", f"Added {item.name}.")


@app.get("/{kind}/{item_id}")
def owner_detail(request: Request, kind: str, item_id: int, user: User = Depends(need()), db: Db = Depends(get_db)):
    k = kind_or_404(kind)
    item = get_or_404(db, k["model"], item_id)
    presses = db.query(Press).filter(k["press_col"] == item_id).order_by(Press.pressed_at.desc()).limit(50).all()
    return render(request, "owner.html", kind=kind, k=k, item=item, presses=presses, tz_names=TZ_NAMES)


@app.post("/{kind}/{item_id}")
def owner_update(kind: str, item_id: int, name: str = Form(""), notes: str = Form(""), address: str = Form(""),
                 timezone_name: str = Form(""), archived: str = Form(""), user: User = Depends(need("manager")),
                 db: Db = Depends(get_db)):
    k = kind_or_404(kind)
    item = get_or_404(db, k["model"], item_id)
    apply_owner_form(item, kind, name, notes, address, timezone_name)
    item.archived = archived == "1"
    audit(db, user.email, f"edited {k['one']} {item.name}")
    db.commit()
    return go(f"/{kind}/{item_id}", "Saved.")
