"""Database models for catalog objects, users, and throttling."""

import json
from datetime import datetime, timezone
from typing import Any, List

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ObjectRecord(Base):
    __tablename__ = "objects"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simbad_main_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    query_text: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ra_deg: Mapped[float | None] = mapped_column(nullable=True)
    dec_deg: Mapped[float | None] = mapped_column(nullable=True)
    otype: Mapped[str | None] = mapped_column(String, nullable=True)
    spectral_type: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_state: Mapped[str] = mapped_column(String, nullable=False)
    # Tracks whether a partial result came from a lookup failure.
    planets_lookup_failed: Mapped[bool] = mapped_column(default=False, nullable=False)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Timestamp for the most recent AI summary.
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Candidate list for ambiguous lookups.
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Serialized resolution trail for the UI.
    resolved_via_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Child rows associated with this object.
    identifiers: Mapped[List["IdentifierRecord"]] = relationship(back_populates="object", cascade="all, delete-orphan")
    planets: Mapped[List["PlanetRecord"]] = relationship(back_populates="object", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "resolution_state IN ('RESOLVED','AMBIGUOUS','PARTIAL','UNRESOLVED','LOOKUP_FAILED')",
            name="ck_resolution_state",
        ),
    )

    @property
    def candidates(self) -> list[dict[str, Any]]:
        """Deserialize stored candidates."""
        if not self.candidates_json:
            return []
        try:
            return json.loads(self.candidates_json)
        except (TypeError, ValueError):
            return []

    @property
    def resolved_via(self) -> list[str]:
        """Deserialize the stored resolution trail."""
        if not self.resolved_via_json:
            return []
        try:
            return json.loads(self.resolved_via_json)
        except (TypeError, ValueError):
            return []

    def to_dict(self) -> dict:
        """Create a JSON-friendly dictionary."""
        return {
            "id": self.id,
            "simbad_main_id": self.simbad_main_id,
            "query_text": self.query_text,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "otype": self.otype,
            "spectral_type": self.spectral_type,
            "resolution_state": self.resolution_state,
            "planets_lookup_failed": self.planets_lookup_failed,
            "ai_summary": self.ai_summary,
            "ai_summary_generated_at": (
                self.ai_summary_generated_at.isoformat() if self.ai_summary_generated_at else None
            ),
            "candidates": self.candidates,
            "resolved_via": self.resolved_via,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "identifiers": [identifier.to_dict() for identifier in self.identifiers],
            "planets": [planet.to_dict() for planet in self.planets],
        }


class IdentifierRecord(Base):
    """SIMBAD alias or identifier associated with a resolved object."""
    __tablename__ = "identifiers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    catalog: Mapped[str] = mapped_column(String, nullable=False)
    identifier: Mapped[str] = mapped_column(String, nullable=False)
    matched_exoplanet_archive: Mapped[bool] = mapped_column(default=False)

    object: Mapped[ObjectRecord] = relationship(back_populates="identifiers")

    __table_args__ = (UniqueConstraint("object_id", "catalog", "identifier", name="uq_identifier"),)

    def to_dict(self) -> dict:
        """Serialize the identifier row for API and template consumers."""
        return {
            "id": self.id,
            "catalog": self.catalog,
            "identifier": self.identifier,
            "matched_exoplanet_archive": self.matched_exoplanet_archive,
        }


class PlanetRecord(Base):
    """Planet metadata linked to a resolved object."""
    __tablename__ = "planets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    pl_name: Mapped[str] = mapped_column(String, nullable=False)
    pl_letter: Mapped[str | None] = mapped_column(String, nullable=True)
    orbital_period_days: Mapped[float | None] = mapped_column(nullable=True)
    planet_radius_earth: Mapped[float | None] = mapped_column(nullable=True)
    discovery_year: Mapped[int | None] = mapped_column(nullable=True)
    discovery_method: Mapped[str | None] = mapped_column(String, nullable=True)

    object: Mapped[ObjectRecord] = relationship(back_populates="planets")

    def to_dict(self) -> dict:
        """Serialize the planet row for API and template consumers."""
        return {
            "id": self.id,
            "pl_name": self.pl_name,
            "pl_letter": self.pl_letter,
            "orbital_period_days": self.orbital_period_days,
            "planet_radius_earth": self.planet_radius_earth,
            "discovery_year": self.discovery_year,
            "discovery_method": self.discovery_method,
        }


class User(Base):
    """Registered account with saved searches and AI summary snapshots."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Personal Gemini API key fallback (see README section III.A). Stored as
    # Fernet ciphertext, never as plaintext -- see app/crypto_utils.py. Never
    # decrypted for display; the settings UI only ever shows whether a key is
    # currently saved, not the key itself.
    gemini_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_preferred_model: Mapped[str | None] = mapped_column(String, nullable=True)

    saved_searches: Mapped[List["SavedSearch"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    summary_snapshots: Mapped[List["UserSummarySnapshot"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class SavedSearch(Base):
    """User bookmark for a resolved object."""
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped[User] = relationship(back_populates="saved_searches")
    object: Mapped[ObjectRecord] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "object_id", name="uq_saved_search"),)


class UserSummarySnapshot(Base):
    """Persisted copy of an AI summary for a user."""
    __tablename__ = "user_summary_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped[User] = relationship(back_populates="summary_snapshots")
    object: Mapped[ObjectRecord] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "object_id", name="uq_user_summary_snapshot"),)


class QueryAlias(Base):
    """Historical query string mapped to its resolved object."""
    __tablename__ = "query_aliases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query_text: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RateLimitEvent(Base):
    """Single throttling event used for per-user and per-session limits."""
    __tablename__ = "rate_limit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('user','session','resolve_user','resolve_session','auth')",
            name="ck_rate_limit_subject_type",
        ),
        # check_limit() filters by exactly (subject_type, subject_id, created_at) on
        # every AI-summary request -- see EVALUATION.md suggestion #5. Without this,
        # that query does a full table scan as rate_limit_events grows.
        Index("ix_rate_limit_events_subject_created", "subject_type", "subject_id", "created_at"),
    )
