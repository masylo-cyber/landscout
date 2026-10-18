"""Komendy bota Telegram (long polling w scheduler loop):
/status – stan ostatniego przebiegu, /stats – liczby, /top galano|bauro|mw – 5 najlepszych,
/ok <id> | /nie <id> | /dzwon <id> – statusy ofert, /pomoc.
"""
from __future__ import annotations

import logging
from html import escape

import httpx
from sqlalchemy import desc, func, select

from .config import settings
from .db import Listing, RunLog, kv_get, kv_set, session
from .notify import TG, card_text, telegram_chat_id

log = logging.getLogger("landscout.bot")


def _send(chat: str, text: str) -> None:
    s = settings()
    with httpx.Client(timeout=30) as c:
        c.post(TG.format(token=s.telegram_bot_token, method="sendMessage"),
               data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})


def _status() -> str:
    with session() as db:
        r = db.scalars(select(RunLog).order_by(desc(RunLog.id)).limit(1)).first()
        n = db.scalar(select(func.count()).where(Listing.active.is_(True)))
        by_src = dict(db.execute(select(Listing.source, func.count()).where(Listing.active.is_(True)).group_by(Listing.source)).all())
        if not r:
            return "Brak przebiegów."
        st = r.stats or {}
        when = r.finished.strftime("%d.%m %H:%M") if r.finished else "w toku"
        return (f"<b>Ostatni przebieg:</b> {when}\nzebrano {st.get('collected', 0)}, nowych {st.get('new', 0)}, "
                f"analiz {st.get('analyzed', 0)}, alerty G/B/MW: {st.get('alerts_galano', 0)}/{st.get('alerts_bauro', 0)}/{st.get('alerts_mw', 0)}"
                f"\nbłędy: {len(r.errors or [])}\n<b>Aktywnych ofert:</b> {n}  {escape(str(by_src))}")


def _stats() -> str:
    with session() as db:
        out = []
        for p in ("galano", "bauro", "mw"):
            vc = getattr(Listing, f"verdict_{p}")
            rows = db.execute(select(vc, func.count()).where(Listing.active.is_(True)).group_by(vc)).all()
            d = {k or "-": v for k, v in rows}
            out.append(f"<b>{p.upper()}</b>: alert {d.get('alert', 0)} · przegląd {d.get('review', 0)} · odrzucone {d.get('reject', 0)}")
        return "\n".join(out)


def _top(profile: str) -> str:
    if profile not in ("galano", "bauro", "mw"):
        return "Użycie: /top galano | bauro | mw"
    with session() as db:
        sc = getattr(Listing, f"score_{profile}")
        vd = getattr(Listing, f"verdict_{profile}")
        rows = db.scalars(select(Listing).where(Listing.active.is_(True), vd.in_(["alert", "review"]), Listing.status != "rejected")
                          .order_by(desc(sc)).limit(5)).all()
        if not rows:
            return "Brak ofert."
        return "\n\n".join(card_text(r, profile) for r in rows)


def _set_status(arg: str, status: str) -> str:
    try:
        lid = int(arg.strip().lstrip("#"))
    except ValueError:
        return "Podaj numer oferty, np. /ok 123"
    with session() as db:
        row = db.get(Listing, lid)
        if not row:
            return f"Nie ma oferty #{lid}"
        row.status = status
        db.commit()
        return f"#{lid} → {status}"


def handle(text: str, chat: str) -> str | None:
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    cmd, _, arg = t.partition(" ")
    cmd = cmd.lower().split("@")[0]
    if cmd == "/status":
        return _status()
    if cmd == "/stats":
        return _stats()
    if cmd == "/top":
        return _top(arg.strip().lower() or "galano")
    if cmd in ("/ok", "/dzwon", "/dzwonie"):
        return _set_status(arg, "called")
    if cmd in ("/nie", "/odrzuc", "/odrzuć"):
        return _set_status(arg, "rejected")
    if cmd in ("/ogledziny", "/oględziny", "/visit"):
        return _set_status(arg, "visit")
    if cmd in ("/pomoc", "/help"):
        return ("/status – ostatni przebieg\n/stats – liczby alertów\n/top galano|bauro|mw – 5 najlepszych\n"
                "/ok 123 – dzwoniłem\n/nie 123 – odrzuć\n/ogledziny 123 – do oględzin")
    return None


def poll_once() -> int:
    """Obsługuje nowe wiadomości od ostatniego offsetu. Zwraca liczbę obsłużonych."""
    s = settings()
    if not s.telegram_bot_token:
        return 0
    with session() as db:
        offset = int(kv_get(db, "tg_offset", "0") or 0)
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(TG.format(token=s.telegram_bot_token, method="getUpdates"), params={"offset": offset, "timeout": 0})
            updates = r.json().get("result", [])
    except Exception as e:  # noqa: BLE001
        log.warning("poll: %s", e)
        return 0
    n = 0
    last = offset
    for u in updates:
        last = max(last, int(u["update_id"]) + 1)
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        txt = m.get("text") or ""
        if not chat or not txt:
            continue
        if txt.lower().startswith("/start"):
            continue  # obsługuje discover_chat_ids
        reply = handle(txt, chat)
        if reply:
            _send(chat, reply)
            n += 1
    if last != offset:
        with session() as db:
            kv_set(db, "tg_offset", str(last))
    return n
