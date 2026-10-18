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


def card_text(row: Listing, profile: str) -> str:
    a = row.analysis or {}
    price_m2 = f" ({row.price_pln / row.area_m2:,.0f} zł/m²)".replace(",", " ") if row.price_pln and row.area_m2 else ""
    head = "🏘 GALANO" if profile == "galano" else "🌲 BAURO"
    sc = row.score_galano if profile == "galano" else row.score_bauro
    lines = [f"<b>{head} · {sc:.0f}/100</b>",
             f"<b>{escape(row.title[:120])}</b>",
             f"{_fmt_price(row.price_pln)}{price_m2} · {_fmt_area(row.area_m2)}",
             f"📍 {escape(row.area_name or row.location_text or '')}"]
    if profile == "galano" and row.segments_est:
        lines.append(f"🏗 szac. segmentów: <b>{row.segments_est}</b> (próg dla {row.ring or '?'})")
    mp = row.mpzp or {}
    z = a.get("zoning_symbol") or ", ".join(mp.get("symbols") or []) or ("plan jest" if mp.get("has_plan") else "brak danych")
    lines.append(f"📜 MPZP: {escape(str(z))}")
    if row.parcel_id:
        lines.append(f"🧭 działka: {escape(row.parcel_id)}")
    if a.get("summary_pl"):
        lines.append(f"\n{escape(a['summary_pl'])}")
    if a.get("risks"):
        lines.append("⚠️ " + "; ".join(escape(r) for r in a["risks"][:4]))
    lines.append(f"\n🔗 {escape(row.url)}")
    if row.lat and row.lon:
        lines.append(f"🗺 https://maps.google.com/?q={row.lat},{row.lon}")
    lines.append(f"źródło: {row.source} · #{row.id}")
    return "\n".join(lines)


def telegram_chat_id(profile: str) -> str:
    s = settings()
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
            if photo:
                r = c.post(TG.format(token=s.telegram_bot_token, method="sendPhoto"),
                           data={"chat_id": chat, "photo": photo, "caption": text[:1000], "parse_mode": "HTML"})
                if r.status_code == 200:
                    return True
            r = c.post(TG.format(token=s.telegram_bot_token, method="sendMessage"),
                       data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": False})
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
            for upd in r.json().get("result", []):
                m = upd.get("message") or upd.get("channel_post") or {}
                txt = (m.get("text") or "").strip().lower()
                chat = str((m.get("chat") or {}).get("id", ""))
                if not chat:
                    continue
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
