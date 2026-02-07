from fastapi import FastAPI, Depends, HTTPException, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from datetime import datetime, timedelta
from calendar import monthrange
from urllib.parse import urlparse
import secrets
import re
from typing import Optional, Any
from app.database import get_db, init_db_schema
from app.models import (
    User,
    Highlight,
    Device,
    Source,
    Tag,
    SourceType,
    HighlightStatus,
    Collection,
    CollectionItem,
    Reminder,
)
from app.auth import hash_password, verify_password
from app.config import settings

app = FastAPI(title="Personal Highlight Manager")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
templates = Jinja2Templates(directory="app/templates")
init_db_schema()


def highlight_matches(text: str, search_term: str) -> str:
    """Bold matching search terms in text."""
    if not search_term or not text:
        return text
    # Escape special regex characters but preserve the search term
    escaped_term = re.escape(search_term)
    # Case-insensitive replacement
    pattern = re.compile(f"({escaped_term})", re.IGNORECASE)
    return pattern.sub(r"<strong>\1</strong>", text)


# Add custom filter to Jinja2
templates.env.filters["highlight_matches"] = highlight_matches


def get_session_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def normalize_url(url: str) -> str:
    """Add http:// scheme if missing from URL."""
    if url and not url.startswith(("http://", "https://")):
        return f"http://{url}"
    return url


def normalize_text_for_fingerprint(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def build_import_fingerprint(
    source_id: Optional[str], original_text: str
) -> Optional[str]:
    if not source_id:
        return None
    normalized_text = normalize_text_for_fingerprint(original_text)
    if not normalized_text:
        return None
    return f"{source_id}::{normalized_text}"


WEB_DEVICE_NAME = "Web"
WEB_DEVICE_PREFIX = "web"
SOURCE_HIGHLIGHTS_PREVIEW_LIMIT = 25
HOME_DUE_REMINDERS_LIMIT = 10


def get_device_from_auth_header(auth_header: Optional[str], db: Session) -> Device:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Invalid authorization")

    api_key = None
    if auth_header.startswith("Bearer "):
        api_key = auth_header.replace("Bearer ", "", 1).strip()
    elif auth_header.startswith("Token "):
        api_key = auth_header.replace("Token ", "", 1).strip()

    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid authorization")

    device = None
    for d in db.query(Device).filter(Device.revoked_at.is_(None)).all():
        if verify_password(api_key, d.api_key_hash):
            device = d
            break

    if not device:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return device


def get_or_create_web_device(
    user_id: str, db: Session, backfill: bool = False
) -> Device:
    device = (
        db.query(Device)
        .filter(
            Device.user_id == user_id,
            Device.revoked_at.is_(None),
            Device.name == WEB_DEVICE_NAME,
        )
        .first()
    )
    changed = False
    if not device:
        api_key = f"phm_web_{secrets.token_urlsafe(24)}"
        device = Device(
            user_id=user_id,
            name=WEB_DEVICE_NAME,
            api_key_hash=hash_password(api_key),
            prefix=WEB_DEVICE_PREFIX,
        )
        db.add(device)
        db.flush()
        changed = True

    if backfill:
        updated = (
            db.query(Highlight)
            .filter(Highlight.user_id == user_id, Highlight.device_id.is_(None))
            .update({Highlight.device_id: device.id})
        )
        if updated:
            changed = True

    if changed:
        db.commit()
        db.refresh(device)

    return device


def cleanup_orphaned_sources(user_id: str, db: Session):
    """Delete sources that have no active highlights."""
    orphaned = (
        db.query(Source)
        .outerjoin(Highlight, Source.id == Highlight.source_id)
        .filter(
            Source.user_id == user_id,
            Highlight.id.is_(None),
        )
        .all()
    )
    for source in orphaned:
        db.delete(source)
    if orphaned:
        db.commit()


def add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def add_years(dt: datetime, years: int) -> datetime:
    year = dt.year + years
    day = min(dt.day, monthrange(year, dt.month)[1])
    return dt.replace(year=year, day=day)


def build_remind_at_from_preset(preset: str, now: datetime) -> datetime:
    start_today = datetime(now.year, now.month, now.day)
    if preset == "tomorrow":
        return start_today + timedelta(days=1)
    if preset == "next_week":
        return start_today + timedelta(days=7)
    if preset == "next_month":
        return add_months(start_today, 1)
    if preset == "next_year":
        return add_years(start_today, 1)
    raise HTTPException(status_code=400, detail="Invalid reminder preset")


def get_or_create_source(
    user_id: str,
    source_url: Optional[str],
    source_title: Optional[str],
    source_author: Optional[str],
    db: Session,
) -> tuple[Optional[Source], Optional[str], Optional[str], Optional[str]]:
    source = None
    url = None
    page_title = None
    page_author = None

    if source_url:
        source_url = normalize_url(source_url)
        parsed = urlparse(source_url)
        domain = parsed.netloc or None

        if domain:
            source = (
                db.query(Source)
                .filter(
                    Source.user_id == user_id,
                    Source.type == SourceType.WEB,
                    Source.domain == domain,
                )
                .first()
            )
            if not source:
                source = Source(
                    user_id=user_id,
                    domain=domain,
                    type=SourceType.WEB,
                    original_name=domain,
                    display_name=domain,
                )
                db.add(source)
                db.flush()

            url = source_url
            page_title = source_title
            page_author = source_author
    elif source_title:
        source = (
            db.query(Source)
            .filter(
                Source.user_id == user_id,
                Source.type == SourceType.BOOK,
                Source.title.ilike(source_title),
            )
            .first()
        )
        if not source:
            source = Source(
                user_id=user_id,
                title=source_title,
                author=source_author,
                type=SourceType.BOOK,
                original_name=source_title,
                display_name=source_title,
            )
            db.add(source)
            db.flush()
        elif source_author and not source.author:
            source.author = source_author

    return source, url, page_title, page_author


def create_highlight_with_metadata(
    user_id: str,
    text: str,
    note: Optional[str],
    tags: Optional[str],
    source_url: Optional[str],
    source_title: Optional[str],
    source_author: Optional[str],
    device_id: Optional[str],
    db: Session,
    dedupe_on_import: bool = False,
    location: Optional[dict[str, Any]] = None,
) -> tuple[Highlight, bool]:
    source, url, page_title, page_author = get_or_create_source(
        user_id=user_id,
        source_url=source_url,
        source_title=source_title,
        source_author=source_author,
        db=db,
    )
    source_id = source.id if source else None
    fingerprint = build_import_fingerprint(source_id, text)

    if dedupe_on_import and fingerprint:
        existing = (
            db.query(Highlight)
            .filter(
                Highlight.user_id == user_id,
                Highlight.source_id == source_id,
                or_(
                    Highlight.import_fingerprint == fingerprint,
                    Highlight.original_text == text,
                    Highlight.text == text,
                ),
            )
            .first()
        )
        if existing:
            return existing, False

    highlight = Highlight(
        user_id=user_id,
        text=text,
        note=note,
        source_id=source_id,
        device_id=device_id,
        url=url,
        page_title=page_title,
        page_author=page_author,
        location=location,
        original_text=text,
        original_note=note,
        import_fingerprint=fingerprint,
    )
    db.add(highlight)
    db.flush()

    if tags:
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        for tag_name in tag_names:
            tag = (
                db.query(Tag)
                .filter(Tag.user_id == user_id, Tag.name == tag_name)
                .first()
            )
            if not tag:
                tag = Tag(user_id=user_id, name=tag_name)
                db.add(tag)
                db.flush()
            highlight.tags.append(tag)

    db.commit()
    db.refresh(highlight)
    return highlight, True


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
def register(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Username already taken"}
        )

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    get_or_create_web_device(user.id, db)
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid credentials"}
        )

    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/highlights", response_class=HTMLResponse)
def list_highlights(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    get_or_create_web_device(user.id, db, backfill=True)
    now = datetime.utcnow()
    highlights = (
        db.query(Highlight)
        .filter(Highlight.user_id == user.id)
        .order_by(Highlight.created_at.desc())
        .limit(20)
        .all()
    )
    due_reminders = (
        db.query(Reminder)
        .options(joinedload(Reminder.highlight).joinedload(Highlight.source))
        .filter(Reminder.user_id == user.id, Reminder.remind_at <= now)
        .order_by(Reminder.remind_at.asc())
        .limit(HOME_DUE_REMINDERS_LIMIT)
        .all()
    )
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "highlights": highlights,
            "due_reminders": due_reminders,
            "current_user": user,
        },
    )


@app.post("/highlights")
def create_highlight(
    request: Request,
    text: str = Form(),
    tags: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_author: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Convert empty strings to None
    source_url = source_url.strip() if source_url else None
    source_title = source_title.strip() if source_title else None
    source_author = source_author.strip() if source_author else None
    tags = tags.strip() if tags else None
    note = note.strip() if note else None

    web_device = get_or_create_web_device(user.id, db)
    highlight, _ = create_highlight_with_metadata(
        user_id=user.id,
        text=text,
        note=note,
        tags=tags,
        source_url=source_url or None,
        source_title=source_title or None,
        source_author=source_author or None,
        device_id=web_device.id,
        db=db,
    )

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_item.html",
            {"request": request, "highlight": highlight},
        )
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    get_or_create_web_device(user.id, db, backfill=True)
    devices = (
        db.query(Device)
        .filter(Device.user_id == user.id, Device.revoked_at.is_(None))
        .all()
    )
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "current_user": user, "devices": devices},
    )


@app.post("/devices")
def create_device(
    request: Request,
    name: str = Form(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    api_key = f"phm_live_{secrets.token_urlsafe(32)}"
    device = Device(
        user_id=user.id,
        name=name,
        api_key_hash=hash_password(api_key),
        prefix="phm_live",
    )
    db.add(device)
    db.commit()

    devices = (
        db.query(Device)
        .filter(Device.user_id == user.id, Device.revoked_at.is_(None))
        .all()
    )

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/devices_table.html",
            {
                "request": request,
                "devices": devices,
                "new_api_key": api_key,
                "new_api_key_device_name": device.name,
            },
        )

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "current_user": user,
            "devices": devices,
            "new_api_key": api_key,
            "new_api_key_device_name": device.name,
        },
    )


@app.post("/devices/{device_id}/roll")
def roll_device_key(
    request: Request,
    device_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    device = (
        db.query(Device)
        .filter(
            Device.id == device_id,
            Device.user_id == user.id,
            Device.revoked_at.is_(None),
        )
        .first()
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.name == WEB_DEVICE_NAME:
        raise HTTPException(status_code=400, detail="Web device key cannot be rolled")

    prefix = device.prefix or "phm_live"
    api_key = f"{prefix}_{secrets.token_urlsafe(32)}"
    device.api_key_hash = hash_password(api_key)
    device.last_used_at = None
    db.commit()

    devices = (
        db.query(Device)
        .filter(Device.user_id == user.id, Device.revoked_at.is_(None))
        .all()
    )

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/devices_table.html",
            {
                "request": request,
                "devices": devices,
                "new_api_key": api_key,
                "new_api_key_device_name": device.name,
            },
        )
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "current_user": user,
            "devices": devices,
            "new_api_key": api_key,
            "new_api_key_device_name": device.name,
        },
    )


@app.delete("/devices/{device_id}")
def delete_device(
    request: Request,
    device_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    device = (
        db.query(Device)
        .filter(Device.id == device_id, Device.user_id == user.id)
        .first()
    )
    if device and device.name == WEB_DEVICE_NAME:
        raise HTTPException(status_code=400, detail="Web device cannot be revoked")
    if device:
        device.revoked_at = datetime.utcnow()
        db.commit()

    if is_htmx(request):
        return HTMLResponse("", status_code=200)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/highlights")
def api_create_highlight(
    text: str = Form(),
    note: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_author: Optional[str] = Form(None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    auth_header = request.headers.get("Authorization") if request else None
    device = get_device_from_auth_header(auth_header, db)

    device.last_used_at = datetime.utcnow()
    db.commit()

    # Convert empty strings to None
    source_url = source_url.strip() if source_url else None
    source_title = source_title.strip() if source_title else None
    source_author = source_author.strip() if source_author else None
    tags = tags.strip() if tags else None
    note = note.strip() if note else None

    highlight, created = create_highlight_with_metadata(
        user_id=device.user_id,
        text=text,
        note=note,
        tags=tags,
        source_url=source_url or None,
        source_title=source_title or None,
        source_author=source_author or None,
        device_id=device.id,
        db=db,
        dedupe_on_import=True,
    )

    if not created:
        raise HTTPException(
            status_code=409,
            detail="Duplicate highlight for this source and original text",
        )

    return {"id": str(highlight.id), "created_at": highlight.created_at.isoformat()}


@app.post("/api/highlights/moon-reader")
async def api_create_highlight_moon_reader(
    request: Request,
    db: Session = Depends(get_db),
):
    auth_header = request.headers.get("Authorization")
    device = get_device_from_auth_header(auth_header, db)
    device.last_used_at = datetime.utcnow()
    db.commit()

    payload = await request.json()
    highlights = payload.get("highlights") if isinstance(payload, dict) else None
    if not isinstance(highlights, list) or not highlights:
        raise HTTPException(status_code=422, detail="Missing highlights payload")

    data = highlights[0] if isinstance(highlights[0], dict) else None
    if not data:
        raise HTTPException(status_code=422, detail="Invalid highlight payload")

    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Missing highlight text")

    note = (data.get("note") or "").strip() or None
    source_title = (data.get("title") or "").strip() or None
    source_author = (data.get("author") or "").strip() or None
    chapter = (data.get("chapter") or "").strip() or None

    highlight, created = create_highlight_with_metadata(
        user_id=device.user_id,
        text=text,
        note=note,
        tags=None,
        source_url=None,
        source_title=source_title,
        source_author=source_author,
        device_id=device.id,
        db=db,
        dedupe_on_import=True,
        location={"chapter": chapter} if chapter else None,
    )

    if not created:
        raise HTTPException(
            status_code=409,
            detail="Duplicate highlight for this source and original text",
        )

    return {"id": str(highlight.id), "created_at": highlight.created_at.isoformat()}


@app.get("/highlights/{highlight_id}", response_class=HTMLResponse)
def get_highlight(
    highlight_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
            joinedload(Highlight.device),
            joinedload(Highlight.reminders),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_card.html",
            {
                "request": request,
                "highlight": highlight,
                "current_user": user,
            },
        )

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "highlight": highlight,
            "current_user": user,
        },
    )


@app.post("/highlights/{highlight_id}/reminders")
def create_or_update_reminder(
    highlight_id: str,
    request: Request,
    preset: Optional[str] = Form(None),
    remind_on: Optional[str] = Form(None),
    keep_open: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
            joinedload(Highlight.device),
            joinedload(Highlight.reminders),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    now = datetime.utcnow()
    if remind_on:
        try:
            custom_date = datetime.strptime(remind_on, "%Y-%m-%d")
            resolved_remind_at = datetime(
                custom_date.year, custom_date.month, custom_date.day
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid custom reminder date")
    else:
        if not preset:
            raise HTTPException(status_code=400, detail="Missing reminder preset")
        resolved_remind_at = build_remind_at_from_preset(preset, now)

    reminder = Reminder(
        user_id=user.id,
        highlight_id=highlight_id,
        remind_at=resolved_remind_at,
    )
    db.add(reminder)
    db.commit()
    db.refresh(highlight)

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/reminders_panel.html",
            {
                "request": request,
                "highlight": highlight,
                "keep_open": bool(keep_open),
                "current_user": user,
            },
        )
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.delete("/highlights/{highlight_id}/reminders/{reminder_id}")
def dismiss_highlight_reminder(
    highlight_id: str,
    reminder_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
            joinedload(Highlight.device),
            joinedload(Highlight.reminders),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    reminder = (
        db.query(Reminder)
        .filter(
            Reminder.id == reminder_id,
            Reminder.user_id == user.id,
            Reminder.highlight_id == highlight_id,
        )
        .first()
    )
    if reminder:
        db.delete(reminder)
        db.commit()
    db.refresh(highlight)

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/reminders_panel.html",
            {
                "request": request,
                "highlight": highlight,
                "keep_open": True,
                "current_user": user,
            },
        )
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.patch("/highlights/{highlight_id}")
def update_highlight(
    highlight_id: str,
    request: Request,
    text: str = Form(),
    note: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_author: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    if highlight.original_text is None:
        highlight.original_text = highlight.text
    if highlight.original_note is None:
        highlight.original_note = highlight.note

    note = note.strip() if note else None
    highlight.text = text
    highlight.note = note
    highlight.updated_at = datetime.utcnow()

    # Convert empty strings to None
    source_url = source_url.strip() if source_url else None
    source_title = source_title.strip() if source_title else None
    source_author = source_author.strip() if source_author else None
    tags = tags.strip() if tags else None

    source_url = source_url or None
    source_title = source_title or None
    source_author = source_author or None
    tags = tags or None

    highlight.tags.clear()
    if tags:
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        for tag_name in tag_names:
            tag = (
                db.query(Tag)
                .filter(Tag.user_id == user.id, Tag.name == tag_name)
                .first()
            )
            if not tag:
                tag = Tag(user_id=user.id, name=tag_name)
                db.add(tag)
                db.flush()
            highlight.tags.append(tag)

    if source_url or source_title:
        source, resolved_url, page_title, page_author = get_or_create_source(
            user_id=user.id,
            source_url=source_url,
            source_title=source_title,
            source_author=source_author,
            db=db,
        )
        highlight.source_id = source.id if source else None
        highlight.url = resolved_url
        highlight.page_title = page_title
        highlight.page_author = page_author
        fingerprint_text = highlight.original_text or text
        highlight.import_fingerprint = build_import_fingerprint(
            highlight.source_id, fingerprint_text
        )
    else:
        highlight.source_id = None
        highlight.url = None
        highlight.page_title = None
        highlight.page_author = None
        highlight.import_fingerprint = None

    db.commit()
    db.refresh(highlight)

    # Reload with relationships for response
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
            joinedload(Highlight.device),
        )
        .filter(Highlight.id == highlight_id)
        .first()
    )

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_card.html",
            {
                "request": request,
                "highlight": highlight,
                "current_user": user,
            },
        )
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.get("/highlights/{highlight_id}/edit", response_class=HTMLResponse)
def get_highlight_edit(
    highlight_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
            joinedload(Highlight.device),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        "partials/highlight_edit.html",
        {"request": request, "highlight": highlight, "current_user": user},
    )


@app.get("/highlights/{highlight_id}/add-tag", response_class=HTMLResponse)
def get_add_tag_modal(
    highlight_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        "partials/tag_modal.html",
        {"request": request, "highlight": highlight, "current_user": user},
    )


@app.post("/highlights/{highlight_id}/tags")
def add_tag_to_highlight(
    highlight_id: str,
    request: Request,
    tag_name: str = Form(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404)

    tag_name = tag_name.strip()
    if tag_name:
        tag = db.query(Tag).filter(Tag.user_id == user.id, Tag.name == tag_name).first()
        if not tag:
            tag = Tag(user_id=user.id, name=tag_name)
            db.add(tag)
            db.flush()

        if tag not in highlight.tags:
            highlight.tags.append(tag)
            db.commit()
            db.refresh(highlight)

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_card.html",
            {
                "request": request,
                "highlight": highlight,
                "current_user": user,
                "show_metadata": True,
            },
        )
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.put("/highlights/{highlight_id}/favorite")
def toggle_favorite(
    request: Request,
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404)

    highlight.is_favorite = not highlight.is_favorite
    db.commit()
    db.refresh(highlight)

    if is_htmx(request):
        icon = (
            '<svg class="icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" data-fav="true">'
            '<path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>'
            if highlight.is_favorite
            else '<svg class="icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" data-fav="false">'
            '<path stroke-linecap="round" stroke-linejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z"/></svg>'
        )
        return HTMLResponse(icon)
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.delete("/highlights/{highlight_id}")
def delete_highlight(
    request: Request,
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404)

    source_id = highlight.source_id
    highlight.status = (
        HighlightStatus.ARCHIVED
        if highlight.status == HighlightStatus.ACTIVE
        else HighlightStatus.ACTIVE
    )
    if highlight.status == HighlightStatus.ARCHIVED:
        db.query(Reminder).filter(
            Reminder.user_id == user.id, Reminder.highlight_id == highlight.id
        ).delete()
    db.commit()
    db.refresh(highlight)

    # Cleanup orphaned sources if this was the last highlight
    if highlight.status == HighlightStatus.ARCHIVED and source_id:
        cleanup_orphaned_sources(user.id, db)

    if is_htmx(request):
        # For detail page, redirect to highlights list (avoids nested templates)
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = "/highlights"
        return response
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    sources = (
        db.query(Source)
        .filter(Source.user_id == user.id)
        .order_by(
            func.coalesce(
                Source.display_name, Source.original_name, Source.title, Source.domain
            )
        )
        .all()
    )
    return templates.TemplateResponse(
        "sources.html",
        {"request": request, "sources": sources, "current_user": user},
    )


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(
    source_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    source = (
        db.query(Source)
        .filter(Source.id == source_id, Source.user_id == user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    highlight_count = (
        db.query(Highlight)
        .filter(Highlight.source_id == source_id, Highlight.user_id == user.id)
        .count()
    )
    highlights = (
        db.query(Highlight)
        .options(joinedload(Highlight.tags))
        .filter(Highlight.source_id == source_id, Highlight.user_id == user.id)
        .order_by(Highlight.created_at.desc())
        .limit(SOURCE_HIGHLIGHTS_PREVIEW_LIMIT)
        .all()
    )

    return templates.TemplateResponse(
        "source_detail.html",
        {
            "request": request,
            "source": source,
            "highlights": highlights,
            "highlight_count": highlight_count,
            "preview_limit": SOURCE_HIGHLIGHTS_PREVIEW_LIMIT,
            "current_user": user,
        },
    )


@app.patch("/sources/{source_id}")
def update_source_name(
    source_id: str,
    request: Request,
    display_name: str = Form(),
    source_author: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    source = (
        db.query(Source)
        .filter(Source.id == source_id, Source.user_id == user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name cannot be empty")

    source_author = source_author.strip() if source_author else None
    if source_type:
        source_type = source_type.strip().lower()
        if source_type not in {SourceType.BOOK.value, SourceType.WEB.value}:
            raise HTTPException(status_code=400, detail="Invalid source type")
        source.type = SourceType(source_type)
    source.display_name = display_name
    source.author = source_author
    source.updated_at = datetime.utcnow()
    db.commit()

    if is_htmx(request):
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = f"/sources/{source_id}"
        return response
    return RedirectResponse(url=f"/sources/{source_id}", status_code=303)


# Quick search for nav dropdown
@app.get("/api/search-quick", response_class=HTMLResponse)
def search_quick(
    request: Request,
    q: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    results = []
    if q and len(q.strip()) >= 2:
        search_term = f"%{q}%"
        results = (
            db.query(Highlight)
            .filter(
                Highlight.user_id == user.id,
                or_(
                    Highlight.text.ilike(search_term),
                    Highlight.note.ilike(search_term),
                    Highlight.page_title.ilike(search_term),
                ),
            )
            .order_by(Highlight.created_at.desc())
            .limit(5)
            .all()
        )

    if not results:
        return ""

    # Return dropdown HTML with bold matches
    html = ""
    for h in results:
        preview = h.text[:100] + "..." if len(h.text) > 100 else h.text
        # Apply highlighting
        highlighted_preview = highlight_matches(preview, q)
        source_text = h.source.name if h.source else "No source"

        html += f"""
        <a href="/highlights/{h.id}" class="search-dropdown-item" style="text-decoration: none; color: inherit; display: block;">
            <div style="font-size: 13px; color: #666; margin-bottom: 4px;">
                {source_text}
            </div>
            <div style="font-size: 14px; color: #1a1a1a;">{highlighted_preview}</div>
        </a>
        """

    html += f"""
    <div class="search-dropdown-footer">
        <a href="/search?q={q}">See all results →</a>
    </div>
    """

    return html


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    collection_id: Optional[str] = None,
    tag: Optional[str] = None,
    device_id: Optional[str] = None,
    status: Optional[str] = None,
    favorite: Optional[str] = None,
    sort: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    get_or_create_web_device(user.id, db, backfill=True)
    status_provided = status is not None
    status_filter = status if status not in (None, "", "all") else None
    has_search = bool(
        q
        or source_type
        or source_id
        or collection_id
        or tag
        or device_id
        or favorite
        or status_provided
    )
    results = []
    if has_search:
        query = (
            db.query(Highlight)
            .options(
                joinedload(Highlight.source),
                joinedload(Highlight.tags),
                joinedload(Highlight.collections),
            )
            .filter(Highlight.user_id == user.id)
        )

        if q:
            search_term = f"%{q}%"
            query = query.filter(
                or_(
                    Highlight.text.ilike(search_term), Highlight.note.ilike(search_term)
                )
            )

        if source_type:
            query = query.join(Source).filter(Source.type == source_type)

        if source_id:
            query = query.filter(Highlight.source_id == source_id)

        if collection_id:
            query = query.filter(
                Highlight.collections.any(Collection.id == collection_id)
            )

        if tag:
            query = query.filter(Highlight.tags.any(Tag.name == tag))

        if device_id:
            query = query.filter(Highlight.device_id == device_id)

        if status_provided:
            if status_filter:
                query = query.filter(Highlight.status == HighlightStatus(status_filter))
        else:
            query = query.filter(Highlight.status == HighlightStatus("active"))

        if favorite == "true":
            query = query.filter(Highlight.is_favorite.is_(True))

        # Apply sorting
        if sort == "recent-asc":
            query = query.order_by(Highlight.created_at.asc())
        elif sort == "relevance" and q:
            # For relevance, prioritize exact matches in text over note
            # This is a simple implementation; could be enhanced with full-text search
            query = query.order_by(
                Highlight.text.ilike(search_term).desc(), Highlight.created_at.desc()
            )
        else:  # Default to recent-desc
            query = query.order_by(Highlight.created_at.desc())

        results = query.all()

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/search_results.html",
            {
                "request": request,
                "results": results,
                "query": q,
                "source_type": source_type,
                "source_id": source_id,
                "collection_id": collection_id,
                "tag": tag,
                "device_id": device_id,
                "status": status,
                "favorite": favorite,
                "sort": sort,
            },
        )

    devices = (
        db.query(Device)
        .filter(Device.user_id == user.id, Device.revoked_at.is_(None))
        .order_by(Device.created_at.desc())
        .all()
    )

    status_ui = status if status is not None else "active"
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "results": results,
            "query": q,
            "source_type": source_type,
            "source_id": source_id,
            "collection_id": collection_id,
            "tag": tag,
            "device_id": device_id,
            "status": status,
            "status_ui": status_ui,
            "favorite": favorite,
            "sort": sort,
            "devices": devices,
            "current_user": user,
        },
    )


@app.get("/reminders", response_class=HTMLResponse)
def reminders_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    now = datetime.utcnow()
    reminders = (
        db.query(Reminder)
        .options(joinedload(Reminder.highlight).joinedload(Highlight.source))
        .filter(Reminder.user_id == user.id)
        .order_by(Reminder.remind_at.asc())
        .all()
    )
    return templates.TemplateResponse(
        "reminders.html",
        {
            "request": request,
            "reminders": reminders,
            "now": now,
            "current_user": user,
        },
    )


@app.delete("/reminders/{reminder_id}")
def dismiss_reminder_by_id(
    reminder_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    reminder = (
        db.query(Reminder)
        .filter(Reminder.id == reminder_id, Reminder.user_id == user.id)
        .first()
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")

    db.delete(reminder)
    db.commit()

    if is_htmx(request):
        return HTMLResponse("", status_code=200)
    return RedirectResponse(url="/reminders", status_code=303)


# Collections endpoints
@app.get("/collections", response_class=HTMLResponse)
def list_collections(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    collections = (
        db.query(Collection)
        .filter(Collection.user_id == user.id)
        .order_by(Collection.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "collections.html",
        {"request": request, "collections": collections, "current_user": user},
    )


@app.post("/collections")
def create_collection(
    request: Request,
    name: str = Form(),
    description: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = Collection(
        user_id=user.id,
        name=name,
        description=description,
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/collection_item.html",
            {"request": request, "collection": collection},
        )
    return RedirectResponse(url="/collections", status_code=303)


@app.get("/collections/{collection_id}", response_class=HTMLResponse)
def get_collection(
    collection_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = (
        db.query(Collection)
        .filter(Collection.id == collection_id, Collection.user_id == user.id)
        .first()
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Redirect to search with collection filter
    return RedirectResponse(
        url=f"/search?collection_id={collection_id}", status_code=303
    )


@app.patch("/collections/{collection_id}")
def update_collection(
    collection_id: str,
    request: Request,
    name: str = Form(),
    description: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = (
        db.query(Collection)
        .filter(Collection.id == collection_id, Collection.user_id == user.id)
        .first()
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    collection.name = name
    collection.description = description
    db.commit()

    if is_htmx(request):
        return HTMLResponse(
            '<div style="padding: 15px; background: #d4edda; border-radius: 4px; margin-bottom: 20px;">'
            "Collection updated successfully!</div>"
        )
    return RedirectResponse(url=f"/collections/{collection_id}", status_code=303)


@app.delete("/collections/{collection_id}")
def delete_collection(
    collection_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = (
        db.query(Collection)
        .filter(Collection.id == collection_id, Collection.user_id == user.id)
        .first()
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    db.delete(collection)
    db.commit()
    return {"status": "deleted"}


@app.post("/collections/{collection_id}/highlights/{highlight_id}")
def add_highlight_to_collection(
    collection_id: str,
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = (
        db.query(Collection)
        .filter(Collection.id == collection_id, Collection.user_id == user.id)
        .first()
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    # Check if already in collection
    existing = (
        db.query(CollectionItem)
        .filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.highlight_id == highlight_id,
        )
        .first()
    )
    if existing:
        return {"status": "already_exists"}

    # Add to collection
    collection.highlights.append(highlight)
    db.commit()
    return {"status": "added"}


@app.delete("/collections/{collection_id}/highlights/{highlight_id}")
def remove_highlight_from_collection(
    collection_id: str,
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    collection = (
        db.query(Collection)
        .filter(Collection.id == collection_id, Collection.user_id == user.id)
        .first()
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    # Remove from collection
    if highlight in collection.highlights:
        collection.highlights.remove(highlight)
        db.commit()
        return {"status": "removed"}

    return {"status": "not_found"}
