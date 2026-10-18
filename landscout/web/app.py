from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from ..config import settings
from ..db import Listing, RunLog, session

app = FastAPI(title="LandScout")
tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()

STATUSES = ["new", "seen", "called", "visit", "offer", "rejected"]
STATUS_PL = {"new": "nowa", "seen": "obejrzana", "called": "dzwoniłem", "visit": "do oględzin", "offer": "oferta", "rejected": "odrzucona"}


def auth(c: HTTPBasicCredentials = Depends(security)) -> str:
    s = settings()
    if not (secrets.compare_digest(c.username, s.panel_user) and secrets.compare_digest(c.password, s.panel_password)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    return c.username


@app.get("/", response_class=HTMLResponse)
def index(request: Request, profile: str = "galano", verdict: str = "alert,review", status_f: str = "",
          q: str = "", page: int = 1, _: str = Depends(auth)):
    with session() as db:
        score_col = getattr(Listing, f"score_{profile}")
        verdict_col = getattr(Listing, f"verdict_{profile}")
        stmt = select(Listing).where(Listing.active.is_(True))
        if verdict:
            stmt = stmt.where(verdict_col.in_(verdict.split(",")))
        if status_f:
            stmt = stmt.where(Listing.status == status_f)
        if q:
            like = f"%{q}%"
            stmt = stmt.where((Listing.title.ilike(like)) | (Listing.location_text.ilike(like)) | (Listing.area_name.ilike(like)))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(stmt.order_by(desc(score_col), desc(Listing.first_seen)).offset((page - 1) * 50).limit(50)).all()
        last_runs = db.scalars(select(RunLog).order_by(desc(RunLog.id)).limit(5)).all()
        counts = {p: db.scalar(select(func.count()).where(Listing.active.is_(True),
                  getattr(Listing, f"verdict_{p}") == "alert")) for p in ("galano", "bauro", "mw")}
    return tpl.TemplateResponse(request, "index.html", dict(rows=rows, profile=profile, verdict=verdict,
                                status_f=status_f, q=q, page=page, total=total, runs=last_runs, counts=counts,
                                STATUSES=STATUSES, STATUS_PL=STATUS_PL))


@app.get("/l/{lid}", response_class=HTMLResponse)
def detail(request: Request, lid: int, _: str = Depends(auth)):
    with session() as db:
        row = db.get(Listing, lid)
        if not row:
            raise HTTPException(404)
        dups = []
        if row.cluster_key:
            dups = db.scalars(select(Listing).where(Listing.cluster_key == row.cluster_key, Listing.id != lid)).all()
    return tpl.TemplateResponse(request, "detail.html", dict(r=row, dups=dups, profile="galano", counts=None,
                                                    STATUSES=STATUSES, STATUS_PL=STATUS_PL))


@app.post("/l/{lid}/status")
def set_status(lid: int, status_v: str = Form(...), note: str = Form(""), back: str = Form("/"),
               standard_grade: int | None = Form(None), location_grade: int | None = Form(None), _: str = Depends(auth)):
    with session() as db:
        row = db.get(Listing, lid)
        if row and status_v in STATUSES:
            row.status = status_v
            if note:
                row.note = note
            if standard_grade or location_grade:
                row.standard_grade = standard_grade or row.standard_grade
                row.location_grade = location_grade or row.location_grade
                from ..scoring import score
                score(row)
            db.commit()
    return RedirectResponse(back, status_code=303)


@app.get("/health")
def health():
    return {"ok": True}
