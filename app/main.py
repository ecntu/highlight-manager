from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from datetime import datetime
from urllib.parse import urlparse
import secrets
import re
from typing import Optional
from app.database import get_db
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
)
from app.auth import hash_password, verify_password
from app.config import settings

app = FastAPI(title="Personal Highlight Manager")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
templates = Jinja2Templates(directory="app/templates")


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


def cleanup_orphaned_sources(user_id: str, db: Session):
    """Delete sources that have no active highlights."""
    orphaned = (
        db.query(Source)
        .outerjoin(Highlight, Source.id == Highlight.source_id)
        .filter(
            Source.user_id == user_id,
            Highlight.id == None,
        )
        .all()
    )
    for source in orphaned:
        db.delete(source)
    if orphaned:
        db.commit()


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
) -> Highlight:
    source_id = None
    url = None
    page_title = None
    page_author = None

    if source_url or source_title:
        # Web source: extract domain and create/find domain-level source
        if source_url:
            source_url = normalize_url(source_url)
            parsed = urlparse(source_url)
            domain = parsed.netloc or None

            if domain:
                # Find or create domain-level source
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
                    )
                    db.add(source)
                    db.flush()

                source_id = source.id
                url = source_url
                page_title = source_title
                page_author = source_author

        # Book source: match by title
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
                )
                db.add(source)
                db.flush()

            source_id = source.id

    highlight = Highlight(
        user_id=user_id,
        text=text,
        note=note,
        source_id=source_id,
        device_id=device_id,
        url=url,
        page_title=page_title,
        page_author=page_author,
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
    return highlight


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

    highlight = create_highlight_with_metadata(
        user_id=user.id,
        text=text,
        note=note,
        tags=tags,
        source_url=source_url or None,
        source_title=source_title or None,
        source_author=source_author or None,
        device_id=None,
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

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/devices_table.html",
            {"request": request, "devices": devices, "new_api_key": api_key},
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
    db.commit()

    # Convert empty strings to None
    source_url = source_url.strip() if source_url else None
    source_title = source_title.strip() if source_title else None
    source_author = source_author.strip() if source_author else None
    tags = tags.strip() if tags else None
    note = note.strip() if note else None

    highlight = create_highlight_with_metadata(
        user_id=device.user_id,
        text=text,
        note=note,
        tags=tags,
        source_url=source_url or None,
        source_title=source_title or None,
        source_author=source_author or None,
        device_id=device.id,
        db=db,
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
        )
        .filter(Highlight.id == highlight_id, Highlight.user_id == user.id)
        .first()
    )
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_card.html",
            {"request": request, "highlight": highlight, "current_user": user},
        )

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
        # Normalize URL (add scheme if missing) and extract domain
        domain = None
        if source_url:
            source_url = normalize_url(source_url)
            parsed = urlparse(source_url)
            domain = parsed.netloc or None

        # If only URL provided, use domain as title
        if source_url and not source_title:
            source_title = domain or source_url

        # Match by URL if provided, otherwise by title
        if source_url:
            source = (
                db.query(Source)
                .filter(
                    Source.user_id == user.id,
                    Source.url == source_url,
                )
                .first()
            )
        else:
            source = (
                db.query(Source)
                .filter(
                    Source.user_id == user.id,
                    Source.title.ilike(source_title),
                )
                .first()
            )

        if not source:
            # Infer type: if url provided it's web, otherwise book
            source_type = SourceType.WEB if source_url else SourceType.BOOK
            source = Source(
                user_id=user.id,
                url=source_url,
                domain=domain,
                title=source_title,
                author=source_author,
                type=source_type,
            )
            db.add(source)
            db.flush()
        highlight.source_id = source.id
    else:
        highlight.source_id = None

    db.commit()
    db.refresh(highlight)

    # Reload with relationships for response
    highlight = (
        db.query(Highlight)
        .options(
            joinedload(Highlight.source),
            joinedload(Highlight.tags),
            joinedload(Highlight.collections),
        )
        .filter(Highlight.id == highlight_id)
        .first()
    )

    if is_htmx(request):
        return templates.TemplateResponse(
            "partials/highlight_card.html",
            {"request": request, "highlight": highlight, "current_user": user},
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
            {"request": request, "highlight": highlight, "current_user": user},
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
        # Return just the icon (heart variant)
        icon = "♥" if highlight.is_favorite else "♡"
        return PlainTextResponse(icon)
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
        db.query(Source).filter(Source.user_id == user.id).order_by(Source.title).all()
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

    # Redirect to search with source filter
    return RedirectResponse(url=f"/search?source_id={source_id}", status_code=303)

    return templates.TemplateResponse(
        "source_detail.html",
        {
            "request": request,
            "source": source,
            "highlights": highlights,
            "current_user": user,
        },
    )


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
        source_text = (
            h.source.domain
            if h.source and h.source.type.value == "web"
            else (h.source.title if h.source else "No source")
        )

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
    status: Optional[str] = None,
    favorite: Optional[str] = None,
    sort: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    results = []
    if q or source_type or source_id or collection_id or tag or status or favorite:
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

        if status:
            query = query.filter(Highlight.status == HighlightStatus(status))

        if favorite == "true":
            query = query.filter(Highlight.is_favorite == True)

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
                "status": status,
                "favorite": favorite,
                "sort": sort,
            },
        )

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
            "status": status,
            "favorite": favorite,
            "sort": sort,
            "current_user": user,
        },
    )


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
