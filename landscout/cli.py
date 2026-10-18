from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="landscout")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="jeden przebieg")
    r.add_argument("--source", help="tylko jeden kolektor (olx|otodom|komornik|<apify>)")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-alerts", action="store_true")

    sub.add_parser("loop", help="przebiegi w pętli co RUN_INTERVAL_MIN")
    sub.add_parser("serve", help="panel WWW")
    sub.add_parser("initdb", help="utwórz tabele")
    sub.add_parser("telegram-setup", help="wykryj chat_id po /start")
    t = sub.add_parser("test-alert", help="wyślij testowy alert")
    t.add_argument("profile", nargs="?", default="galano")

    a = p.parse_args(argv)
    if a.cmd == "run":
        from .pipeline import run
        stats = run(only_source=a.source, no_llm=a.no_llm, no_alerts=a.no_alerts)
        print(stats)
    elif a.cmd == "loop":
        from .pipeline import loop
        loop()
    elif a.cmd == "serve":
        import uvicorn
        from .db import init_db
        init_db()
        uvicorn.run("landscout.web.app:app", host="0.0.0.0", port=8080, log_level="info")
    elif a.cmd == "initdb":
        from .db import init_db
        init_db()
        print("ok")
    elif a.cmd == "telegram-setup":
        from .db import init_db
        from .notify import discover_chat_ids
        init_db()
        found = discover_chat_ids()
        if found:
            print("Zapisano chat_id:", found)
        else:
            print("Nie znaleziono. Napisz do bota '/start galano' lub '/start bauro' i uruchom ponownie.")
    elif a.cmd == "test-alert":
        from .notify import send_telegram, send_mail
        ok = send_telegram(f"✅ LandScout – test alertu ({a.profile})", a.profile)
        okm = send_mail("[LandScout] test", "<b>LandScout działa.</b>")
        print(f"telegram={ok} mail={okm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
