"""Komendy bota Telegram (long polling w scheduler loop):
/status – stan ostatniego przebiegu, /stats – liczby, /top galano|bauro|mw – 5 najlepszych,
/ok <id> | /nie <id> | /dzwon <id> – statusy ofert, /pomoc.
"""
from __future__ import annotations

import logging
import re
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


def _set_grade(arg: str, col: str) -> str:
    try:
        lid_s, g_s = arg.split()
        lid, g = int(lid_s.strip().lstrip("#")), int(g_s)
        assert 1 <= g <= 5
    except Exception:  # noqa: BLE001
        return "Użycie: /std 123 4  (1–5, 3 = typowo)"
    from .scoring import score
    with session() as db:
        row = db.get(Listing, lid)
        if not row:
            return f"Nie ma oferty #{lid}"
        setattr(row, col, g)
        score(row)
        db.commit()
        pf = row.proforma_galano or row.proforma_bauro or row.proforma_mw
        from .proforma import proforma_line
        return f"#{lid} {col.split('_')[0]} = {g}" + (f"\n💰 {proforma_line(pf)}" if pf else "")


def _analiza(arg: str) -> str:
    url = arg.strip()
    if not url.startswith("http"):
        return "Użycie: /analiza <link do oferty>"
    from .adhoc import analyze_url
    try:
        row = analyze_url(url, force=True)
    except Exception as e:  # noqa: BLE001
        log.exception("analiza %s", url)
        return f"Nie udało się: {escape(str(e))[:200]}"
    out = []
    for prof in ("galano", "bauro", "mw"):
        vd = getattr(row, f"verdict_{prof}")
        if vd != "reject" or prof == "galano":
            out.append(card_text(row, prof))
    if not row.lat:
        out.append("⚠️ nie udało się zlokalizować działki – brak obszaru, MPZP i radaru rynku")
    return "\n\n".join(out)


def _market(arg: str) -> str:
    try:
        lid = int(arg.strip().lstrip("#"))
    except ValueError:
        return "Użycie: /rynek 123"
    from .comps import market_line, market_stats
    with session() as db:
        row = db.get(Listing, lid)
        if not row or not row.lat:
            return f"Brak oferty #{lid} lub bez lokalizacji"
        row.market = market_stats(db, row.lat, row.lon)
        db.commit()
        inv = row.market.get("investments") or []
        extra = "\n".join(f"• {i['name']} – {i['kind']}, {i['dist_km']} km, {i['active']} akt./{i['gone']} zniknęło"
                           + (f", {i['pm2_med']:,} zł/m², {i['area_p25']}–{i['area_p75']} m²".replace(",", " ") if i.get("pm2_med") else "")
                           for i in inv[:8])
        return escape(market_line(row.market) or "Brak ofert porównawczych w 2 km") + ("\n" + escape(extra) if extra else "")


def _known_chat(chat: str) -> bool:
    from .notify import telegram_chat_id
    return chat in {telegram_chat_id("galano"), telegram_chat_id("bauro")}


def _profile_for_chat(chat: str) -> str | None:
    from .notify import telegram_chat_id
    return "bauro" if chat == telegram_chat_id("bauro") else "galano" if chat == telegram_chat_id("galano") else None


def _add_lesson(text: str, chat: str, lid: int | None) -> str:
    """Zapisuje uwagę jako zasadę dla analizy; jeśli dotyczy oferty – analizuje ją ponownie i odsyła nową kartę."""
    from .db import Lesson
    text = text.strip()
    if len(text) < 5:
        return "Napisz uwagę, np. odpowiedz na kartę: „dla Galano 2 domy wystarczą, jeśli marża jest dobra”."
    prof = _profile_for_chat(chat)
    with session() as db:
        les = Lesson(text=(f"[#{lid}] " if lid else "") + text[:500], listing_id=lid, profile=prof, active=True)
        db.add(les)
        db.commit()
        les_id = les.id
    out = f"📝 Zapamiętane jako zasada #{les_id} – uwzględnię w kolejnych analizach."
    if lid:
        from .analyze import analyze
        from .pipeline import _fill_description
        from .scoring import score
        with session() as db:
            row = db.get(Listing, lid)
            if row:
                row.analysis = {}
                _fill_description(row)
                analyze(row)
                score(row)
                db.commit()
                out += "\n\nPonowna ocena:\n" + card_text(row, prof or "galano")
    return out


def _set_price(arg: str) -> str:
    """/cena Złotokłos 8500 [galano|bauro|mw] – cena sprzedaży zł/m² brutto dla obszaru (nadpisuje tabelę)."""
    import json
    m = re.match(r"(.+?)\s+(\d[\d\s]{2,})\s*(galano|bauro|mw)?\s*$", arg.strip(), re.I)
    if not m:
        return "Użycie: /cena Złotokłos 8500 [galano|bauro|mw] (domyślnie galano)"
    area, val, prof = m.group(1).strip(), int(re.sub(r"\s", "", m.group(2))), (m.group(3) or "galano").lower()
    with session() as db:
        d = json.loads(kv_get(db, "price_overrides", "{}") or "{}")
        d.setdefault(prof, {})[area] = val
        kv_set(db, "price_overrides", json.dumps(d, ensure_ascii=False))
    from . import proforma
    proforma._OV["t"] = 0
    return f"💰 {prof.upper()} {area}: {val:,} zł/m² brutto – użyję przy kolejnych przeliczeniach.".replace(",", " ")


def _lessons(arg: str) -> str:
    from .db import Lesson
    with session() as db:
        rows = db.scalars(select(Lesson).where(Lesson.active.is_(True)).order_by(Lesson.id)).all()
        if not rows:
            return "Brak zasad. Dodaj: odpowiedz na kartę oferty albo /uwaga <tekst>."
        return "<b>Zasady dla analizy:</b>\n" + "\n".join(f"{r.id}. {escape(r.text)}" for r in rows) + "\n\nUsuń: /usun 3"


def _del_lesson(arg: str) -> str:
    from .db import Lesson
    try:
        les_id = int(arg.strip())
    except ValueError:
        return "Użycie: /usun 3"
    with session() as db:
        r = db.get(Lesson, les_id)
        if not r:
            return f"Nie ma zasady {les_id}"
        r.active = False
        db.commit()
    return f"Zasada {les_id} usunięta."


def handle(text: str, chat: str, reply_to: str = "") -> str | None:
    t = (text or "").strip()
    if not t.startswith("/"):
        # odpowiedź na kartę oferty = uwaga do tej oferty (nauka agenta)
        ids = list(dict.fromkeys(re.findall(r"#(\d+)", reply_to or "")))
        if not ids or not _known_chat(chat):
            return None
        own = re.search(r"#(\d+)", t)
        if own:
            return _add_lesson(re.sub(r"#\d+", "", t, count=1), chat, int(own.group(1)))
        if len(ids) > 1:
            return "W tej wiadomości jest kilka ofert – dopisz numer, np. „#" + ids[0] + " dla Galano 2 domy wystarczą”."
        return _add_lesson(t, chat, int(ids[0]))
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
    if cmd in ("/std", "/standard", "/lok", "/otoczenie", "/otocz"):
        return _set_grade(arg, "standard_grade" if cmd in ("/std", "/standard") else "location_grade")
    if cmd in ("/uwaga", "/zasada", "/lekcja"):
        if not _known_chat(chat):
            return None
        m = re.match(r"#?(\d+)\s+(.*)", arg.strip(), re.S)
        return _add_lesson(m.group(2), chat, int(m.group(1))) if m else _add_lesson(arg, chat, None)
    if cmd in ("/cena",):
        return _set_price(arg) if _known_chat(chat) else None
    if cmd in ("/lekcje", "/zasady"):
        return _lessons(arg)
    if cmd in ("/usun", "/usuń"):
        return _del_lesson(arg) if _known_chat(chat) else None
    if cmd in ("/hala", "/hale"):
        from .hala import card as hcard, score_hala
        with session() as db:
            rows = db.scalars(select(Listing).where(Listing.active.is_(True), Listing.score_hala >= 40)
                              .order_by(desc(Listing.score_hala)).limit(10)).all()
            out = []
            for r in rows:
                _, why, km = score_hala(r)
                out.append(hcard(r, why, km or 0))
            return "\n\n".join(out) if out else "Brak gruntów pod halę w promieniu 25 km od Magdalenki."
    if cmd in ("/wynajem",):
        from .db import Rental
        with session() as db:
            from .rentals import _km, card as rcard
            rows = sorted(db.scalars(select(Rental).where(Rental.active.is_(True), Rental.matches.is_(True))).all(), key=lambda x: _km(x) or 99)[:15]
            return "\n\n".join(rcard(r) for r in rows) if rows else "Brak domów na wynajem spełniających kryteria (≥220 m², rok ≥2020, bez szeregów, do 15 km od Magdalenki, bez Warszawy)."
    if cmd in ("/rynek", "/market"):
        return _market(arg)
    if cmd in ("/analiza", "/analizuj", "/a"):
        return _analiza(arg)
    if cmd in ("/pomoc", "/help"):
        return ("/status – ostatni przebieg\n/stats – liczby alertów\n/top galano|bauro|mw – 5 najlepszych\n"
                "/ok 123 – dzwoniłem\n/nie 123 – odrzuć\n/ogledziny 123 – do oględzin\n"
                "/std 123 4 – standard 1–5 (3 = typowy)\n/lok 123 2 – otoczenie 1–5\n/rynek 123 – radar rynku 2 km\n"
                "/analiza <link> – pełna analiza dowolnej oferty z linku\n"
                "Odpowiedz na kartę oferty swoją uwagą – agent zapamięta zasadę i oceni ofertę ponownie\n"
                "/uwaga <tekst> – ogólna zasada · /zasady – lista · /usun 3 – usuń zasadę\n"
                "/cena Złotokłos 8500 [galano|bauro|mw] – Twoja cena sprzedaży dla obszaru\n"
                "/wynajem – domy na wynajem · /hala – grunty pod halę")
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
            low = txt.lower()
            names = [p for p in ("galano", "bauro", "wynajem", "hala") if p in low]
            if names:
                with session() as db:
                    for p in names:
                        kv_set(db, f"tg_chat_{p}", chat)
                _send(chat, "✅ Ten czat odbiera: " + ", ".join(names))
            continue
        rt = (m.get("reply_to_message") or {})
        reply = handle(txt, chat, (rt.get("text") or rt.get("caption") or ""))
        if reply:
            _send(chat, reply)
            n += 1
    if last != offset:
        with session() as db:
            kv_set(db, "tg_offset", str(last))
    return n
