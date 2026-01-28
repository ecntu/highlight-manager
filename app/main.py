from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime
import secrets
from typing import Optional
from app.database import get_db
from app.models import User, Highlight, Device, Source, Tag, SourceType, HighlightStatus
from app.auth import hash_password, verify_password
from app.config import settings


class MethodOverrideMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Check for _method in query params for simplicity
        if request.method == "POST":
            method_override = request.query_params.get("_method")
            if method_override and method_override.upper() in [
                "PUT",
                "PATCH",
                "DELETE",
            ]:
                request.scope["method"] = method_override.upper()
        return await call_next(request)


app = FastAPI(title="Personal Highlight Manager")
app.add_middleware(MethodOverrideMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
templates = Jinja2Templates(directory="app/templates")


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
    return RedirectResponse(url="/login", status_code=303)


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
    highlights = (
        db.query(Highlight)
        .filter(Highlight.user_id == user.id)
        .order_by(Highlight.created_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "highlights": highlights, "current_user": user},
    )


@app.post("/highlights")
def create_highlight(
    request: Request,
    text: str = Form(),
    tags: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
    source_author: Optional[str] = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    source_id = None
    if source_title and source_type:
        source = (
            db.query(Source)
            .filter(
                Source.user_id == user.id,
                Source.title == source_title,
                Source.type == source_type,
            )
            .first()
        )
        if not source:
            source = Source(
                user_id=user.id,
                title=source_title,
                type=SourceType(source_type),
                author=source_author,
            )
            db.add(source)
            db.flush()
        source_id = source.id

    highlight = Highlight(user_id=user.id, text=text, note=note, source_id=source_id)
    db.add(highlight)
    db.flush()

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

    db.commit()
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    devices = (
        db.query(Device)
        .filter(Device.user_id == user.id, Device.revoked_at == None)
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
        .filter(Device.user_id == user.id, Device.revoked_at == None)
        .all()
    )
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "current_user": user,
            "devices": devices,
            "new_api_key": api_key,
        },
    )


@app.delete("/devices/{device_id}")
def delete_device(
    device_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    device = (
        db.query(Device)
        .filter(Device.id == device_id, Device.user_id == user.id)
        .first()
    )
    if device:
        device.revoked_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/ingest/highlight")
def ingest_highlight(
    text: str = Form(),
    note: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
    source_author: Optional[str] = Form(None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    auth_header = request.headers.get("Authorization") if request else None
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization")

    api_key = auth_header.replace("Bearer ", "")
    device = None
    for d in db.query(Device).filter(Device.revoked_at == None).all():
        if verify_password(api_key, d.api_key_hash):
            device = d
            break

    if not device:
        raise HTTPException(status_code=401, detail="Invalid API key")

    device.last_used_at = datetime.utcnow()

    source_id = None
    if source_title and source_type:
        source = (
            db.query(Source)
            .filter(
                Source.user_id == device.user_id,
                Source.title == source_title,
                Source.type == source_type,
            )
            .first()
        )

        if not source:
            source = Source(
                user_id=device.user_id,
                title=source_title,
                type=SourceType(source_type),
                author=source_author,
            )
            db.add(source)
            db.flush()
        source_id = source.id

    highlight = Highlight(
        user_id=device.user_id,
        device_id=device.id,
        source_id=source_id,
        text=text,
        note=note,
    )
    db.add(highlight)

    if tags:
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        for tag_name in tag_names:
            tag = (
                db.query(Tag)
                .filter(Tag.user_id == device.user_id, Tag.name == tag_name)
                .first()
            )
            if not tag:
                tag = Tag(user_id=device.user_id, name=tag_name)
                db.add(tag)
                db.flush()
            highlight.tags.append(tag)

    db.commit()
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
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    return templates.TemplateResponse(
        "detail.html",
        {"request": request, "highlight": highlight, "current_user": user},
    )


@app.patch("/highlights/{highlight_id}")
def update_highlight(
    highlight_id: str,
    request: Request,
    text: str = Form(),
    note: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_title: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
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

    highlight.text = text
    highlight.note = note
    highlight.updated_at = datetime.utcnow()

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

    if source_title and source_type:
        source = (
            db.query(Source)
            .filter(
                Source.user_id == user.id,
                Source.title == source_title,
                Source.type == source_type,
            )
            .first()
        )
        if not source:
            source = Source(
                user_id=user.id,
                title=source_title,
                type=SourceType(source_type),
                author=source_author,
            )
            db.add(source)
            db.flush()
        highlight.source_id = source.id
    else:
        highlight.source_id = None

    db.commit()
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.put("/highlights/{highlight_id}/favorite")
def toggle_favorite(
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if highlight:
        highlight.is_favorite = not highlight.is_favorite
        db.commit()
    return RedirectResponse(url=f"/highlights/{highlight_id}", status_code=303)


@app.delete("/highlights/{highlight_id}")
def delete_highlight(
    highlight_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = (
        db.query(Highlight)
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if highlight:
        highlight.status = (
            HighlightStatus.ARCHIVED
            if highlight.status == HighlightStatus.ACTIVE
            else HighlightStatus.ACTIVE
        )
        db.commit()
    return RedirectResponse(url="/highlights", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    sources = (
        db.query(Source).filter(Source.user_id == user.id).order_by(Source.title).all()
    )
    return templates.TemplateResponse(
        "sources.html",
        {"request": request, "sources": sources, "current_user": user},
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: Optional[str] = None,
    source_type: Optional[str] = None,
    status: Optional[str] = None,
    favorite: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    results = []
    if q or source_type or status or favorite:
        query = db.query(Highlight).filter(Highlight.user_id == user.id)

        if q:
            search_term = f"%{q}%"
            query = query.filter(
                or_(
                    Highlight.text.ilike(search_term), Highlight.note.ilike(search_term)
                )
            )

        if source_type:
            query = query.join(Source).filter(Source.type == source_type)

        if status:
            from app.models import HighlightStatus

            query = query.filter(Highlight.status == HighlightStatus(status))

        if favorite == "true":
            query = query.filter(Highlight.is_favorite == True)

        results = query.order_by(Highlight.created_at.desc()).all()

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "results": results,
            "query": q,
            "source_type": source_type,
            "status": status,
            "favorite": favorite,
            "current_user": user,
        },
    )
