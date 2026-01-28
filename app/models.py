import enum
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Text,
    Boolean,
    Integer,
    ForeignKey,
    Date,
    Enum as SQLEnum,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
from app.database import Base


class SourceType(str, enum.Enum):
    BOOK = "book"
    ARTICLE = "article"
    WEB = "web"
    PDF = "pdf"
    VIDEO = "video"
    PODCAST = "podcast"
    TWEET = "tweet"
    NOTE = "note"
    OTHER = "other"


class LinkType(str, enum.Enum):
    RELATED = "related"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXAMPLE = "example"
    EXPANDS = "expands"


class HighlightStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    devices = relationship("Device", back_populates="user")
    sources = relationship("Source", back_populates="user")
    highlights = relationship("Highlight", back_populates="user")
    tags = relationship("Tag", back_populates="user")
    collections = relationship("Collection", back_populates="user")
    digest_config = relationship("DigestConfig", back_populates="user", uselist=False)


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(Text, nullable=False)
    api_key_hash = Column(String(255), unique=True, nullable=False, index=True)
    prefix = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="devices")
    highlights = relationship("Highlight", back_populates="device")

    __table_args__ = (Index("ix_devices_user_id", "user_id"),)


class Source(Base):
    __tablename__ = "sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type = Column(SQLEnum(SourceType), nullable=False)
    title = Column(Text, nullable=False)
    author = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    publisher = Column(Text, nullable=True)
    published_at = Column(Date, nullable=True)
    source_metadata = Column(JSONB, nullable=True, default={})
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", back_populates="sources")
    highlights = relationship("Highlight", back_populates="source")

    __table_args__ = (
        Index("ix_sources_user_type", "user_id", "type"),
        Index("ix_sources_user_title", "user_id", "title"),
        Index("ix_sources_metadata", "source_metadata", postgresql_using="gin"),
    )


class Highlight(Base):
    __tablename__ = "highlights"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_id = Column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    device_id = Column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    text = Column(Text, nullable=False)
    note = Column(Text, nullable=True)
    location = Column(JSONB, nullable=True)
    status = Column(
        SQLEnum(HighlightStatus), default=HighlightStatus.ACTIVE, nullable=False
    )
    is_favorite = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    highlighted_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="highlights")
    source = relationship("Source", back_populates="highlights")
    device = relationship("Device", back_populates="highlights")
    tags = relationship("Tag", secondary="highlight_tags", back_populates="highlights")
    collections = relationship(
        "Collection", secondary="collection_items", back_populates="highlights"
    )

    __table_args__ = (
        Index("ix_highlights_user_created", "user_id", "created_at"),
        Index("ix_highlights_user_source", "user_id", "source_id"),
        Index("ix_highlights_user_favorite", "user_id", "is_favorite"),
        Index("ix_highlights_user_device", "user_id", "device_id"),
    )


class Tag(Base):
    __tablename__ = "tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="tags")
    highlights = relationship(
        "Highlight", secondary="highlight_tags", back_populates="tags"
    )

    __table_args__ = (Index("ix_tags_user_name", "user_id", "name", unique=True),)


class HighlightTag(Base):
    __tablename__ = "highlight_tags"

    highlight_id = Column(
        UUID(as_uuid=True),
        ForeignKey("highlights.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id = Column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class Collection(Base):
    __tablename__ = "collections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="collections")
    highlights = relationship(
        "Highlight", secondary="collection_items", back_populates="collections"
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"

    collection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    highlight_id = Column(
        UUID(as_uuid=True),
        ForeignKey("highlights.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class HighlightLink(Base):
    __tablename__ = "highlight_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    from_highlight_id = Column(
        UUID(as_uuid=True),
        ForeignKey("highlights.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_highlight_id = Column(
        UUID(as_uuid=True),
        ForeignKey("highlights.id", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(SQLEnum(LinkType), nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index(
            "ix_highlight_links_unique",
            "from_highlight_id",
            "to_highlight_id",
            "type",
            unique=True,
        ),
    )


class DigestConfig(Base):
    __tablename__ = "digest_config"

    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    daily_count = Column(Integer, default=5, nullable=False)
    tag_focus = Column(ARRAY(Text), default=[], nullable=False)
    timezone = Column(String(50), default="America/Detroit", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", back_populates="digest_config")
