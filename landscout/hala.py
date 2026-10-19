"""Grunt pod halę: działki ~800–3500 m² (najlepiej do 2500) z możliwością postawienia hali (P, U, P/U, AG, usługi/produkcja/
magazyn w planie lub ogłoszeniu), im bliżej Magdalenki tym wyżej. Osobny czat Telegram „hala”, paczka co ALERT_INTERVAL_DAYS."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import select

from .comps import _dist_km
from .config import settings
from .db import Listing, kv_get, kv_set, session

log = logging.getLogger("landscout.hala")

MAGDALENKA = (52.0597, 20.9572)
MAX_KM = 25
AREA_MIN, AREA_MAX, AREA_BEST = 800, 3500, (1500, 2800)
HALA_TEXT = re.compile(r"pod hal|hal[aęiy]\b|magazyn|produkcyj|przemysł|usługow|pod usługi|składow|warsztat|baz[aęy] transport|logistyc|"
                       r"aktywności gospodarczej|działalnoś\w* gospodarcz|pod firm|komercyjn|\bP/?U\b|\bU/?P\b|\b\d*PU\b|\b\d*AG\b", re.I)
HALA_PURPOSE = re.compile(r"usług|produkc|przemys|skład|magazyn|aktywno\w* gospodarcz|baz\w* ", re.I)
HALA_SYM = re.compile(r"^(?:\d+\.?)?(P|U|PU|P/U|U/P|UP|AG|P-U|U-P|PS|UC)(?:[/.-]|$)", re.I)
NOT_HALA = re.compile(r"usługi publiczne|oświat|sakraln|kultu religijnego|\bUO\b|\bUK\b|zabudowa jednorodzinna(?! z usług)", re.I)


def _evidence(r: Listing) -> tuple[int, str]:
    """Siła dowodu: 2 = plan (symbol/przeznaczenie), 1 = ogłoszenie / analiza, 0 = brak."""
    mp = r.mpzp or {}
    for z in mp.get("zones") or []:
        if HALA_SYM.match(z.get("symbol", "").replace(" ", "")) or HALA_PURPOSE.search(z.get("purpose", "")):
            if not NOT_HALA.search(z.get("purpose", "")):
                return 2, f"plan: {z.get('symbol')} – {z.get('purpose')}"
    for sym in mp.get("symbols") or []:
        if HALA_SYM.match(sym):
            return 2, f"plan: {sym}"
    a = r.analysis or {}
    zs = a.get("zoning_symbol") or ""
    if zs and HALA_SYM.match(zs.replace(" ", "")):
        return 1, f"ogłoszenie/analiza: {zs}"
    txt = f"{r.title} {r.description or ''}"
    m = HALA_TEXT.search(txt)
    if m and not NOT_HALA.search(r.title):
        return 1, f"ogłoszenie: „{m.group(0)}”"
    return 0, ""


def score_hala(r: Listing) -> tuple[float | None, str, float | None]:
    if not (r.lat and r.lon) or r.kind not in ("dzialka", "unknown", "inne", "dom_rozbiorka"):
        return None, "", None
    if not r.area_m2 or not (AREA_MIN <= r.area_m2 <= AREA_MAX):
        return None, "", None
    km = _dist_km(MAGDALENKA[0], MAGDALENKA[1], r.lat, r.lon)
    if km > MAX_KM:
        return None, "", None
    ev, why = _evidence(r)
    if ev == 0:
        return None, "", km
    sc = 100 - km * 3 + (15 if ev == 2 else 0)
    if AREA_BEST[0] <= r.area_m2 <= AREA_BEST[1]:
        sc += 10
    if re.search(r"pod hal|hala|magazyn", f"{r.title} {r.description or ''}", re.I):
        sc += 10
    if not r.price_pln or r.price_pln < 10000:
        sc -= 15
    return round(max(0.0, min(100.0, sc)), 1), why, km


def card(r: Listing, why: str, km: float) -> str:
    pm2 = f" · {r.price_pln / r.area_m2:,.0f} zł/m²".replace(",", " ") if r.price_pln and r.area_m2 else ""
    price = f"{r.price_pln / 1e6:.2f} mln" if r.price_pln and r.price_pln >= 1e6 else (f"{r.price_pln / 1e3:.0f} tys." if r.price_pln else "cena ?")
    return "\n".join([
        f"<b>🏭 HALA {r.score_hala:.0f}</b> · {escape((r.location_text or r.area_name or '?')[:60])} · <b>{km:.1f} km</b> od Magdalenki",
        f"<b>{price}</b>{pm2} · {r.area_m2:,.0f} m²".replace(",", " "),
        escape(why[:140]),
        escape(r.title[:110]),
        f'<a href="{escape(r.url)}">▶ oferta ({r.source})</a>'
        + (f' · <a href="https://maps.google.com/?q={r.lat},{r.lon}">mapa</a>' if r.lat and r.lon else "") + f" · #{r.id}",
    ])


def run_hala(send: bool = True, force: bool = False) -> dict:
    from .notify import send_telegram, telegram_chat_id
    st = {"hala_matches": 0, "hala_sent": 0}
    with session() as db:
        rows = []
        for r in db.scalars(select(Listing).where(Listing.active.is_(True), Listing.lat.isnot(None),
                                                  Listing.area_m2.between(AREA_MIN, AREA_MAX))):
            sc, why, km = score_hala(r)
            r.score_hala = sc
            if sc is not None and sc >= 40:
                rows.append((r, why, km))
        db.commit()
        st["hala_matches"] = len(rows)
        if not send or not telegram_chat_id("hala"):
            return st
        last = kv_get(db, "hala_batch_at", "")
        due = force or not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)) >= timedelta(days=settings().alert_interval_days)
        new = [(r, w, k) for r, w, k in rows if not r.alerted_hala]
        if not due or not new:
            return st
        seen, out = set(), []
        for r, why, km in sorted(new, key=lambda x: -x[0].score_hala):
            key = (round(r.area_m2 or 0, -1), round(r.price_pln or 0, -4))
            if key in seen:          # ta sama działka z innego portalu
                r.alerted_hala = True
                continue
            seen.add(key)
            out.append(card(r, why, km))
            r.alerted_hala = True
        chunk = f"<b>🏭 Grunty pod halę – {len(out)} nowych (najbliżej Magdalenki na górze)</b>"
        for c in out:
            if len(chunk) + len(c) > 3800:
                send_telegram(chunk, "hala")
                chunk = ""
            chunk += "\n\n" + c
        if chunk.strip():
            send_telegram(chunk, "hala")
        st["hala_sent"] = len(out)
        kv_set(db, "hala_batch_at", datetime.now(timezone.utc).isoformat())
        db.commit()
    log.info("hala: %s", st)
    return st
