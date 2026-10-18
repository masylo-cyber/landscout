"""Kolektory oparte na aktorach Apify (Gratka, Morizon, Domiporta, FB Marketplace, ...).

Konfiguracja w config/apify.yml:

  - name: gratka
    actor: "nazwa/aktora-z-apify-store"
    input: { startUrls: [{url: "https://gratka.pl/nieruchomosci/dzialki/mazowieckie/..."}] }
    map:                      # jak przełożyć pola rekordu aktora na nasze
      external_id: id
      url: url
      title: title
      description: description
      price: price
      area: area
      location: location
      images: images

Bez pliku lub bez APIFY_TOKEN – te kolektory są pomijane.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from ..config import CONFIG_DIR, settings
from .base import Collector, RawOffer, area_from_text, log, to_float


def _dig(d: dict, path: str):
    cur = d
    for p in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and p.isdigit():
            cur = cur[int(p)] if int(p) < len(cur) else None
        else:
            return None
    return cur


class ApifyCollector(Collector):
    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.actor = spec["actor"]
        self.input = spec.get("input", {})
        self.map = spec.get("map", {})
        self.max_items = int(spec.get("max_items", 300))

    def collect(self) -> Iterable[RawOffer]:
        from apify_client import ApifyClient
        cl = ApifyClient(settings().apify_token)
        try:
            run = cl.actor(self.actor).call(run_input=self.input, timeout_secs=900)
        except Exception as e:  # noqa: BLE001
            log.warning("apify %s: %s", self.name, e)
            return
        if not run or not run.get("defaultDatasetId"):
            return
        n = 0
        for item in cl.dataset(run["defaultDatasetId"]).iterate_items():
            n += 1
            if n > self.max_items:
                break
            g = lambda k, default=None: _dig(item, self.map.get(k, k)) if self.map.get(k, k) else default  # noqa: E731
            title = str(g("title") or "")
            desc = str(g("description") or "")
            area = to_float(g("area")) or area_from_text(f"{title} {desc}")
            imgs = g("images") or []
            if isinstance(imgs, str):
                imgs = [imgs]
            ext = g("external_id") or g("url")
            if not ext:
                continue
            yield RawOffer(
                source=self.name,
                external_id=str(ext)[:120],
                url=str(g("url") or ""),
                title=title,
                description=desc,
                price_pln=to_float(g("price")),
                area_m2=area,
                location_text=str(g("location") or ""),
                lat=to_float(g("lat")), lon=to_float(g("lon")),
                images=[str(i) for i in imgs if i][:8],
                raw={"apify_actor": self.actor},
            )


def apify_collectors() -> list[Collector]:
    if not settings().apify_token:
        return []
    f: Path = CONFIG_DIR / "apify.yml"
    if not f.exists():
        return []
    specs = yaml.safe_load(f.read_text(encoding="utf-8")) or []
    return [ApifyCollector(s) for s in specs if s.get("enabled", True)]
