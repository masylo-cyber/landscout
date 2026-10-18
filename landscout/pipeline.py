"""Jeden przebieg: zbierz -> zapisz -> wzbogać -> analizuj -> oceń -> alertuj."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from .analyze import analyze
from .collectors import all_collectors
from .config import settings
from .db import Listing, RunLog, init_db, session
from .enrich import enrich, in_profile_area_parcel, match_area, mpzp_by_xy
from .normalize import upsert
from .notify import alert, digest, discover_chat_ids, send_telegram
from .scoring import score

log = logging.getLogger("landscout.run")


def run(only_source: str | None = None, no_llm: bool = False, no_alerts: bool = False) -> dict:
    init_db()
    s = settings()
    stats = {"collected": 0, "new": 0, "enriched": 0, "analyzed": 0, "alerts_galano": 0, "alerts_bauro": 0, "alerts_mw": 0}
    errors: list[str] = []
    t0 = time.time()

    with session() as db:
        # blokada: tylko jeden przebieg naraz (scheduler + ręczny `landscout run`)
        if not db.execute(text("select pg_try_advisory_lock(815001)")).scalar():
            log.warning("inny przebieg trwa – pomijam")
            return stats | {"skipped": True}
        try:
            runlog = RunLog()
            db.add(runlog)
            db.commit()

            # 1) zbieranie
            seen_ids: set[int] = set()
            for col in all_collectors(only_source):
                n_src = 0
                try:
                    for offer in col.collect():
                        row, is_new = upsert(db, offer)
                        seen_ids.add(row.id)
                        n_src += 1
                        stats["collected"] += 1
                        stats["new"] += int(is_new)
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    errors.append(f"{col.name}: {e}")
                    log.exception("collector %s", col.name)
                log.info("%s: %d ofert", col.name, n_src)

            # oferty niewidziane od 14 dni -> nieaktywne
            cutoff = datetime.now(timezone.utc) - timedelta(days=14)
            for row in db.scalars(select(Listing).where(Listing.active.is_(True), Listing.last_seen < cutoff)):
                row.active = False
                row.deactivated_at = row.last_seen
            db.commit()

            # 1b) oferty porównawcze (domy/mieszkania) do radaru rynku
            if not only_source:
                try:
                    from .comps import collect_comps
                    stats.update(collect_comps(db))
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    errors.append(f"comps: {e}")
                    log.exception("comps")

            # 2) wzbogacanie + wstępny scoring (sekwencyjnie – limity geokodera), potem analiza LLM równolegle
            q = select(Listing).where(Listing.active.is_(True), Listing.enriched_at.is_(None)).order_by(Listing.first_seen.desc()).limit(1200)
            llm_budget = 0 if no_llm else s.max_llm_per_run
            to_analyze: list[int] = []
            for row in db.scalars(q):
                try:
                    enrich(row)
                    stats["enriched"] += 1
                    score(row)
                    in_bauro = bool(row.lat and row.lon and in_profile_area_parcel(row.lat, row.lon, "bauro", row.parcel, row.location_text))
                    worth_llm = (row.verdict_galano != "reject" or row.verdict_bauro != "reject" or row.verdict_mw != "reject" or in_bauro) and row.kind != "dom"
                    if worth_llm and not row.analysis and len(to_analyze) < llm_budget:
                        to_analyze.append(row.id)
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    errors.append(f"enrich #{row.id}: {e}")
                    log.exception("enrich #%s", row.id)

            if to_analyze:
                n_ok = analyze_many(to_analyze, workers=4)
                stats["analyzed"] += n_ok
                llm_budget -= n_ok

            # 2b) radar rynku 2 km dla ofert, które nie są odrzucone we wszystkich profilach (odświeżany co 7 dni)
            try:
                refresh_market(db)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                errors.append(f"market: {e}")
                log.exception("market")

            # 3) powiadomienia zbiorcze co ALERT_INTERVAL_DAYS (domyślnie 4): oferty z werdyktem "alert" po analizie LLM,
            #    raz na ofertę i profil; duplikaty klastra – raz. Między wysyłkami kandydaci czekają (widać je w panelu).
            if not no_alerts:
                discover_chat_ids()
                from .db import kv_get, kv_set
                from .enrich import in_profile_area_parcel as _in_area
                from .notify import send_batch
                last = kv_get(db, "alert_batch_at", "")
                due = (not last) or (datetime.now(timezone.utc) - datetime.fromisoformat(last)) >= timedelta(days=s.alert_interval_days)
                for prof, flag_col, verdict_col in (("galano", "alerted_galano", "verdict_galano"),
                                                    ("bauro", "alerted_bauro", "verdict_bauro"),
                                                    ("mw", "alerted_mw", "verdict_mw")):
                    q = select(Listing).where(Listing.active.is_(True), getattr(Listing, flag_col).is_(False),
                                              getattr(Listing, verdict_col) == "alert", Listing.status != "rejected")
                    cands: list[Listing] = []
                    for row in db.scalars(q):
                        if not row.analysis:
                            if llm_budget > 0 and analyze(row):
                                llm_budget -= 1
                                score(row)
                                db.commit()
                                if getattr(row, verdict_col) != "alert":
                                    continue
                            else:
                                continue
                        cands.append(row)
                    stats[f"pending_{prof}"] = len(cands)
                    if not due or not cands:
                        continue
                    seen_clusters: set[str] = set()
                    inside, outside = [], []
                    for row in sorted(cands, key=lambda r: -(getattr(r, f"score_{prof}") or 0)):
                        ck = row.cluster_key
                        if ck and (ck in seen_clusters or db.scalar(select(Listing.id).where(Listing.cluster_key == ck,
                                                                                            getattr(Listing, flag_col).is_(True)))):
                            setattr(row, flag_col, True)   # ten sam grunt już wysłany / z innego portalu
                            continue
                        if ck:
                            seen_clusters.add(ck)
                        (inside if _in_area(row.lat, row.lon, prof, row.parcel, row.location_text) else outside).append(row)
                    if send_batch(prof, inside, outside):
                        for row in inside + outside:
                            setattr(row, flag_col, True)
                        stats[f"alerts_{prof}"] += len(inside) + len(outside)
                    db.commit()
                if due:
                    kv_set(db, "alert_batch_at", datetime.now(timezone.utc).isoformat())
                    db.commit()
                for prof in ("galano", "bauro"):
                    try:
                        stats[f"digest_{prof}"] = digest(prof)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"digest {prof}: {e}")

            runlog.finished = datetime.now(timezone.utc)
            runlog.stats = stats | {"seconds": round(time.time() - t0)}
            runlog.errors = errors
            db.commit()

        finally:
            db.rollback()
            db.execute(text("select pg_advisory_unlock(815001)"))
            db.commit()

    log.info("run done: %s errors=%d", stats, len(errors))
    if errors and not no_alerts:
        send_telegram("⚙️ LandScout – błędy przebiegu:\n" + "\n".join(errors[:8])[:3500], "galano")
    return stats


def refresh_market(db, max_age_days: int = 7, limit: int = 400) -> int:
    """Liczy statystyki rynku w promieniu dla ofert z werdyktem alert/review i przelicza pro formę."""
    from .comps import market_stats
    q = select(Listing).where(Listing.active.is_(True), Listing.lat.isnot(None),
                              (Listing.verdict_galano != "reject") | (Listing.verdict_bauro != "reject") | (Listing.verdict_mw != "reject"))
    n = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()[:16]
    for row in db.scalars(q.order_by(Listing.first_seen.desc()).limit(1500)):
        if (row.market or {}).get("at", "") >= cutoff:
            continue
        row.market = market_stats(db, row.lat, row.lon)
        score(row)
        n += 1
        if n % 50 == 0:
            db.commit()
        if n >= limit:
            break
    db.commit()
    log.info("market: odświeżono %d", n)
    return n


def analyze_many(ids: list[int], workers: int = 4) -> int:
    """Analiza LLM równolegle; każdy wątek ma własną sesję DB. Zwraca liczbę udanych analiz."""
    from concurrent.futures import ThreadPoolExecutor

    def one(lid: int) -> bool:
        with session() as db:
            row = db.get(Listing, lid)
            if not row or row.analysis:
                return False
            ok = bool(analyze(row))
            if ok:
                score(row)
                db.commit()
            return ok

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, ids):
            ok += int(bool(res))
    log.info("analyze_many: %d/%d", ok, len(ids))
    return ok


def rescore(refresh_mpzp: bool = False, llm_missing: int = 0, regeocode: bool = False) -> dict:
    """Przelicza scoring wszystkich wzbogaconych ofert (po zmianie reguł) bez ponownego pobierania."""
    init_db()
    n = n_mpzp = n_llm = n_geo = 0
    pending_llm: list[int] = []
    with session() as db:
        if regeocode:
            # oferty bez trafienia w obszar i z lokalizacją bez miejscowości (sama ulica) -> do ponownego wzbogacenia
            for row in db.scalars(select(Listing).where(Listing.active.is_(True), (Listing.area_name.is_(None)) | (Listing.lat.is_(None)))):
                if "," not in (row.location_text or "") or not row.lat:
                    row.lat = row.lon = None
                    row.parcel, row.mpzp, row.parcel_id = {}, {}, None
                    row.enriched_at = None
                    n_geo += 1
            db.commit()
        for row in db.scalars(select(Listing).where(Listing.active.is_(True), Listing.enriched_at.isnot(None))):
            if refresh_mpzp and row.lat and row.lon and not (row.mpzp or {}).get("has_plan"):
                mp = mpzp_by_xy(row.lat, row.lon)
                if mp:
                    row.mpzp = mp
                    n_mpzp += 1
            if row.lat and row.lon:
                _, row.area_name, row.ring = match_area(row.lat, row.lon, row.parcel, row.location_text)
            score(row)
            in_bauro = bool(row.lat and row.lon and in_profile_area_parcel(row.lat, row.lon, "bauro", row.parcel, row.location_text))
            if llm_missing > n_llm and not row.analysis and (row.verdict_galano != "reject" or row.verdict_bauro != "reject" or row.verdict_mw != "reject" or in_bauro):
                pending_llm.append(row.id)
                n_llm += 1
            n += 1
            if n % 50 == 0:
                db.commit()
        db.commit()
    if pending_llm:
        n_llm = analyze_many(pending_llm, workers=4)
    log.info("rescore: %d ofert, mpzp odświeżone: %d, analiz LLM: %d, do ponownego geokodowania: %d", n, n_mpzp, n_llm, n_geo)
    return {"rescored": n, "mpzp_refreshed": n_mpzp, "analyzed": n_llm, "regeocode_queued": n_geo}


def loop() -> None:
    s = settings()
    from .telegram_bot import poll_once
    while True:
        try:
            run()
        except Exception:  # noqa: BLE001
            log.exception("run failed")
        deadline = time.time() + max(s.run_interval_min, 10) * 60
        while time.time() < deadline:
            try:
                poll_once()
            except Exception:  # noqa: BLE001
                log.exception("bot poll failed")
            time.sleep(20)
