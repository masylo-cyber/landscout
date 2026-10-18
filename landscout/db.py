from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint,
                        create_engine, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Listing(Base):
    """Jedna oferta z jednego źródła (po normalizacji)."""
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    price_pln: Mapped[float | None] = mapped_column(Float)
    area_m2: Mapped[float | None] = mapped_column(Float)
    location_text: Mapped[str] = mapped_column(Text, default="")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    images: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(30), default="unknown")  # dzialka | dom_rozbiorka | dom | inne
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    price_history: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # klucz grupujący duplikaty między portalami
    cluster_key: Mapped[str | None] = mapped_column(String(80), index=True)

    # wzbogacenie
    parcel_id: Mapped[str | None] = mapped_column(String(60))
    parcel: Mapped[dict] = mapped_column(JSON, default=dict)
    mpzp: Mapped[dict] = mapped_column(JSON, default=dict)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # analiza modelu
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # scoring per profil
    score_galano: Mapped[float | None] = mapped_column(Float, index=True)
    score_bauro: Mapped[float | None] = mapped_column(Float, index=True)
    segments_est: Mapped[int | None] = mapped_column(Integer)
    ring: Mapped[str | None] = mapped_column(String(20))
    area_name: Mapped[str | None] = mapped_column(String(80))
    proforma_galano: Mapped[dict] = mapped_column(JSON, default=dict)
    proforma_bauro: Mapped[dict] = mapped_column(JSON, default=dict)
    proforma_mw: Mapped[dict] = mapped_column(JSON, default=dict)
    score_mw: Mapped[float | None] = mapped_column(Float, index=True)
    verdict_mw: Mapped[str | None] = mapped_column(String(20))
    alerted_mw: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict_galano: Mapped[str | None] = mapped_column(String(20))  # alert | review | reject
    verdict_bauro: Mapped[str | None] = mapped_column(String(20))
    alerted_galano: Mapped[bool] = mapped_column(Boolean, default=False)
    alerted_bauro: Mapped[bool] = mapped_column(Boolean, default=False)

    # praca użytkownika
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new|seen|called|visit|rejected|offer
    note: Mapped[str] = mapped_column(Text, default="")


class RunLog(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class KV(Base):
    """Drobne ustawienia runtime (np. chat_id Telegrama wykryte po /start)."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


_engine = None
_Session = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings().database_url, pool_pre_ping=True)
    return _engine


def init_db():
    Base.metadata.create_all(engine())
    # proste migracje dodające kolumny (bez Alembic)
    from sqlalchemy import inspect, text
    insp = inspect(engine())
    cols = {c["name"] for c in insp.get_columns("listings")}
    with engine().begin() as conn:
        for name in ("proforma_galano", "proforma_bauro", "proforma_mw"):
            if name not in cols:
                conn.execute(text(f"ALTER TABLE listings ADD COLUMN {name} JSON DEFAULT '{{}}'"))
        if "score_mw" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN score_mw FLOAT"))
            conn.execute(text("ALTER TABLE listings ADD COLUMN verdict_mw VARCHAR(20)"))
            conn.execute(text("ALTER TABLE listings ADD COLUMN alerted_mw BOOLEAN DEFAULT FALSE"))


def session() -> Session:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False)
    return _Session()


def kv_get(s: Session, key: str, default: str = "") -> str:
    row = s.get(KV, key)
    return row.value if row else default


def kv_set(s: Session, key: str, value: str) -> None:
    row = s.get(KV, key)
    if row:
        row.value = value
    else:
        s.add(KV(key=key, value=value))
    s.commit()
