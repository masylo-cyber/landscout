# LandScout

Agent AI wyszukujący nieruchomości pod deweloperkę w okolicach Warszawy – osobno dla
**Galano** (bliźniaki / szeregi / domy, liczy się liczba segmentów) i **BAURO** (działki pod domy premium).

## Co robi

1. **Zbiera** oferty: OLX, Otodom (działki + domy „do rozbiórki"), licytacje komornicze; przez Apify – dowolne
   kolejne źródła (Gratka, Morizon, FB) wg `config/apify.yml`.
2. **Normalizuje i deduplikuje** (ta sama działka na kilku portalach = jeden alert).
3. **Wzbogaca** rejestrami publicznymi: geokodowanie, działka ewidencyjna (ULDK/GUGiK – id, obręb, kształt,
   front), MPZP (krajowa integracja planów – czy jest plan, symbol przeznaczenia jeśli dostępny).
4. **Analizuje** modelem Claude (JSON: typ, media, dojazd, podzielność, ryzyka, szacunek segmentów, podsumowanie).
5. **Ocenia** dwoma profilami (`config/profiles.yml`) i wysyła **alerty** na Telegram i e-mail; reszta ląduje
   w panelu jako „do przeglądu".

## Instalacja (Ubuntu 22.04/24.04, jako root)

```
curl -fsSL https://raw.githubusercontent.com/masylo-cyber/landscout/main/install.sh | bash
```

Instalator pyta o klucze (zapis tylko do `/opt/landscout/.env`), stawia Dockera, Postgresa, firewall
i uruchamia agenta. Potem:

```
landscout telegram-setup   # po wysłaniu do bota "/start galano" i "/start bauro"
landscout test-alert
landscout run              # pierwszy przebieg ręcznie (zwykle 5-15 min)
landscout logs             # podgląd
landscout panel            # jak otworzyć panel WWW (tunel SSH)
landscout update           # aktualizacja do najnowszej wersji z GitHuba
```

## Konfiguracja

- `config/areas.yml` – obszary (gminy / dzielnice), pierścień (`warszawa` / `pierscien` / `dalej`).
- `config/profiles.yml` – progi: m² na segment, minimalna liczba segmentów wg pierścienia, słowa kluczowe.
- `config/apify.yml` – dodatkowe źródła przez aktory Apify.
- `.env` – klucze, SMTP, interwał przebiegów, limit analiz modelem na przebieg.

Zmiany w `config/` wymagają `landscout restart`.

## Rozwój lokalny

```
pip install -e .[dev]
DATABASE_URL=postgresql+psycopg://... landscout run --no-llm --no-alerts
pytest
```
