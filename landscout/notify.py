from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import httpx

from .config import settings
from .db import Listing, kv_get, session

log = logging.getLogger("landscout.notify")
TG = "https://api.telegram.org/bot{token}/{method}"


def _fmt_price(p: float | None) -> str:
    if not p:
        return "cena: brak"
    return f"{p:,.0f} zł".replace(",", " ")


def _fmt_area(a: float | None) -> str:
    return f"{a:,.0f} m²".replace(",", " ") if a else "pow.: brak"


def _zoning(row: Listing) -> str:
    a = row.analysis or {}
    mp = row.mpzp or {}
    if a.get("zoning_symbol"):
        return str(a["zoning_symbol"])
    if mp.get("symbols"):
        return ", ".join(mp["symbols"])
    if mp.get("has_plan"):
        return "plan jest (symbol nieznany)"
    if mp.get("has_plan") is False:
        return "brak planu → WZ"
    return "?"


def _short_summary(a: dict) -> str:
    """1–2 zdania z analizy modelu – pierwsze zdania summary_pl."""
    txt = (a.get("summary_pl") or "").strip()
    if not txt:
        return ""
    parts = [p.strip() for p in txt.replace("\n", " ").split(". ") if p.strip()]
    out = ". ".join(parts[:2])
    return (out + ("." if not out.endswith(".") else ""))[:260]


def card_text(row: Listing, profile: str) -> str:
    a = row.analysis or {}
    head = {"galano": "🏘 GALANO", "bauro": "🌲 BAURO", "mw": "🏢 BAURO MW"}[profile]
    sc = getattr(row, f"score_{profile}")
    price = _fmt_price(row.price_pln)
    pm2 = f" · {row.price_pln / row.area_m2:,.0f} zł/m²".replace(",", " ") if row.price_pln and row.area_m2 else ""
    where = row.area_name or row.location_text or "?"
    if row.area_name and row.location_text and row.location_text.split(",")[0] not in row.area_name:
        where = f"{row.location_text.split(',')[0]}, {row.area_name}"
    lines = [f"<b>{head} {sc:.0f}</b> · {escape(where)}",
             f"<b>{price}</b>{pm2} · {_fmt_area(row.area_m2)}"]
    if profile == "galano":
        lines.append(f"🏗 <b>{row.segments_est or '?'} segm.</b> · MPZP: {escape(_zoning(row))}")
    elif profile == "mw":
        pfm = row.proforma_mw or {}
        lines.append(f"🏢 PUM ~{pfm.get('pum_m2', '?')} m² (~{pfm.get('units', '?')} mieszkań) · MPZP: {escape(_zoning(row))}")
    else:
        lines.append(f"MPZP: {escape(_zoning(row))}")
    extras = []
    if a.get("road_access"):
        extras.append(f"dojazd: {a['road_access']}")
    ut = a.get("utilities") or {}
    med = [k for k, v in (("woda", ut.get("water")), ("kanal.", ut.get("sewage")), ("gaz", ut.get("gas")), ("prąd", ut.get("power"))) if v]
    if med:
        extras.append("media: " + ", ".join(med))
    if a.get("seller"):
        extras.append(a["seller"])
    if extras:
        lines.append(escape(" · ".join(extras)))
    if a.get("risks"):
        lines.append("⚠️ " + escape("; ".join(a["risks"][:3]))[:160])
    pf = getattr(row, f"proforma_{profile}")
    if pf:
        from .proforma import proforma_line, fmt_mln
        lines.append("💰 " + proforma_line(pf))
    if profile == "bauro" and row.segments_est and row.segments_est >= 3 and row.proforma_galano:
        from .proforma import fmt_mln
        lines.append(f"↔ wariant Galano: {row.segments_est} segm., marża {fmt_mln(row.proforma_galano.get('margin'))}")
    summ = _short_summary(a)
    if summ:
        lines.append(escape(summ))
    lines.append(f'<a href="{escape(row.url)}">▶ oferta ({row.source})</a>'
                 + (f' · <a href="https://maps.google.com/?q={row.lat},{row.lon}">mapa</a>' if row.lat and row.lon else "")
                 + f" · #{row.id}")
    return "\n".join(lines)


def telegram_chat_id(profile: str) -> str:
    s = settings()
    if profile == "mw":
        profile = "bauro"   # wielorodzinna idzie na czat BAURO
    env = s.telegram_chat_galano if profile == "galano" else s.telegram_chat_bauro
    if env:
        return env
    with session() as db:
        return kv_get(db, f"tg_chat_{profile}", "")


def send_telegram(text: str, profile: str, photo: str | None = None) -> bool:
    s = settings()
    chat = telegram_chat_id(profile)
    if not s.telegram_bot_token or not chat:
        return False
    try:
        with httpx.Client(timeout=30) as c:
            if photo and len(text) <= 1000:
                r = c.post(TG.format(token=s.telegram_bot_token, method="sendPhoto"),
                           data={"chat_id": chat, "photo": photo, "caption": text, "parse_mode": "HTML"})
                if r.status_code == 200:
                    return True
            # tekst zawsze w całości; podgląd zdjęcia z oferty daje link
            r = c.post(TG.format(token=s.telegram_bot_token, method="sendMessage"),
                       data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": False})
            if r.status_code != 200:
                log.warning("telegram sendMessage %s: %s", r.status_code, r.text[:200])
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("telegram: %s", e)
        return False


def send_mail(subject: str, html: str) -> bool:
    s = settings()
    if not (s.smtp_host and s.mail_to):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, s.mail_from or s.smtp_user, s.mail_to
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as sm:
            sm.starttls()
            if s.smtp_user:
                sm.login(s.smtp_user, s.smtp_password)
            sm.sendmail(msg["From"], [x.strip() for x in s.mail_to.split(",")], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("mail: %s", e)
        return False


def alert(row: Listing, profile: str) -> bool:
    text = card_text(row, profile)
    photo = (row.images or [None])[0]
    ok = send_telegram(text, profile, photo)
    html = "<div style='font-family:sans-serif;max-width:640px'>" + text.replace("\n", "<br>")
    if photo:
        html += f"<br><img src='{escape(photo)}' style='max-width:600px'>"
    html += "</div>"
    ok_mail = send_mail(f"[LandScout {profile.upper()}] {row.title[:80]}", html)
    return ok or ok_mail


def discover_chat_ids() -> dict[str, str]:
    """Czyta /start galano | /start bauro z getUpdates i zapisuje chat_id."""
    s = settings()
    found: dict[str, str] = {}
    if not s.telegram_bot_token:
        return found
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(TG.format(token=s.telegram_bot_token, method="getUpdates"))
            js = r.json()
            if not js.get("ok"):
                log.warning("getUpdates: %s", js.get("description"))
            updates = js.get("result", [])
            log.info("getUpdates: %d wiadomości", len(updates))
            for upd in updates:
                m = upd.get("message") or upd.get("channel_post") or {}
                txt = (m.get("text") or m.get("caption") or "").strip().lower()
                chat = str((m.get("chat") or {}).get("id", ""))
                if not chat:
                    continue
                log.info("  od chat %s: %r", chat, txt[:40])
                if "/start" in txt:
                    for p in ("galano", "bauro"):
                        if p in txt:
                            found[p] = chat
                    if "galano" not in txt and "bauro" not in txt:
                        found.setdefault("galano", chat)
                        found.setdefault("bauro", chat)
    except Exception as e:  # noqa: BLE001
        log.warning("getUpdates: %s", e)
    if found:
        from .db import kv_set
        with session() as db:
            for p, chat in found.items():
                kv_set(db, f"tg_chat_{p}", chat)
    return found
