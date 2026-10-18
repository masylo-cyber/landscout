"""Jeden przebieg: zbierz -> zapisz -> wzbogać -> analizuj -> oceń -> alertuj."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .analyze import analyze
from .collectors import all_collectors
from .config import settings
from .db import Listing, RunLog, init_db, session
from .enrich import enrich
from .normalize import upsert
from .notify import alert, discover_chat_ids, send_telegram
from .scoring import score

log = logging.getLogger("landscout.run")


def run(only_source: str | None = None, no_llm: bool = False, no_alerts: bool = False) -> dict:
    init_db()
    s = settings()
    stats = {"collected": 0, "new": 0, "enriched": 0, "analyzed": 0, "alerts_galano": 0, "alerts_bauro": 0}
    errors: list[str] = []
    t0 = time.time()

    with session() as db:
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
        db.commit()

        # 2) wzbogacanie + analiza + scoring dla nowych / niewzbogaconych
        q = select(Listing).where(Listing.active.is_(True), Listing.enriched_at.is_(None)).order_by(Listing.first_seen.desc()).limit(1200)
        llm_budget = 0 if no_llm else s.max_llm_per_run
        for row in db.scalars(q):
            try:
                enrich(row)
                stats["enriched"] += 1
                # wstępny scoring bez modelu – decyduje, czy warto wydawać tokeny
                score(row)
                worth_llm = (row.verdict_galano != "reject" or row.verdict_bauro != "reject") and row.kind != "dom"
                if worth_llm and llm_budget > 0 and not row.analysis:
                    if analyze(row):
                        stats["analyzed"] += 1
                        llm_budget -= 1
                        score(row)
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                errors.append(f"enrich #{row.id}: {e}")
                log.exception("enrich #%s", row.id)

        # 3) alerty (tylko raz na ofertę i profil; duplikaty klastra – raz)
        if not no_alerts:
            discover_chat_ids()
            alerted_clusters: set[str] = set()
            for prof, flag_col, verdict_col in (("galano", "alerted_galano", "verdict_galano"),
                                                ("bauro", "alerted_bauro", "verdict_bauro")):
                q = select(Listing).where(Listing.active.is_(True), getattr(Listing, flag_col).is_(False),
                                          getattr(Listing, verdict_col) == "alert")
                for row in db.scalars(q):
                    ck = f"{prof}:{row.cluster_key}" if row.cluster_key else None
                    if ck and ck in alerted_clusters:
                        setattr(row, flag_col, True)
                        continue
                    if ck and db.scalar(select(Listing.id).where(Listing.cluster_key == row.cluster_key,
                                                                 getattr(Listing, flag_col).is_(True))):
                        setattr(row, flag_col, True)   # ten sam grunt już alertowany z innego portalu
                        continue
                    if alert(row, prof):
                        setattr(row, flag_col, True)
                        stats[f"alerts_{prof}"] += 1
                        if ck:
                            alerted_clusters.add(ck)
                        time.sleep(1.2)
                db.commit()

        runlog.finished = datetime.now(timezone.utc)
        runlog.stats = stats | {"seconds": round(time.time() - t0)}
        runlog.errors = errors
        db.commit()

    log.info("run done: %s errors=%d", stats, len(errors))
    if errors and not no_alerts:
        send_telegram("⚙️ LandScout – błędy przebiegu:\n" + "\n".join(errors[:8])[:3500], "galano")
    return stats


def loop() -> None:
    s = settings()
    while True:
        try:
            run()
        except Exception:  # noqa: BLE001
            log.exception("run failed")
        time.sleep(max(s.run_interval_min, 10) * 60)
