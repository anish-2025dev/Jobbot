"""JobBot command line: one entry point for every process.

    python -m app.cli api                     # dashboard + REST API (uvicorn)
    python -m app.cli scheduler               # all timed jobs (section 12)
    python -m app.cli apply-worker [-n 5]     # browser workers (section 8.5)
    python -m app.cli telegram                # Telegram bot (long polling)
    python -m app.cli run <job>               # run one scheduler job now (ingest, mail, nightly, ...)
    python -m app.cli try-apply <app_id>      # fill one application in a visible browser, do NOT submit
    python -m app.cli try-apply <app_id> --submit   # same, then submit through the normal gate
    python -m app.cli seed-companies file.csv # name,careers_url[,ats,slug]; seeded companies are pinned
    python -m app.cli create-admin email      # sets a password interactively; first admin
    python -m app.cli resume ...              # offline resume build (see app/resume/cli.py)
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("api")
    a.add_argument("--host", default="0.0.0.0")
    a.add_argument("--port", type=int, default=8000)
    sub.add_parser("scheduler")
    w = sub.add_parser("apply-worker")
    w.add_argument("-n", "--workers", type=int, default=None)
    sub.add_parser("telegram")
    r = sub.add_parser("run")
    r.add_argument("job")
    ta = sub.add_parser("try-apply")
    ta.add_argument("application_id", type=int)
    ta.add_argument("--submit", action="store_true", help="actually submit (default: fill only)")
    ta.add_argument("--headless", action="store_true", help="don't open a visible browser window")
    sc = sub.add_parser("seed-companies")
    sc.add_argument("csv")
    ca = sub.add_parser("create-admin")
    ca.add_argument("email")
    sub.add_parser("resume", add_help=False)
    args, rest = ap.parse_known_args(argv)

    if args.cmd == "resume":
        from app.resume.cli import main as resume_main
        return resume_main(rest)
    _logging()
    if args.cmd == "api":
        import uvicorn
        uvicorn.run("app.main:app", host=args.host, port=args.port, proxy_headers=True)
    elif args.cmd == "scheduler":
        from app.scheduler import run_forever
        run_forever()
    elif args.cmd == "apply-worker":
        _apply_workers(args.workers)
    elif args.cmd == "telegram":
        from app.config import get_settings
        from app.notify.bot_loop import run_forever
        from app.notify.telegram import TelegramBot
        token = get_settings().telegram_bot_token
        if not token:
            print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
            return 2
        run_forever(TelegramBot(token))
    elif args.cmd == "run":
        from app.scheduler import run_named
        print(json.dumps(run_named(args.job), default=str, indent=2))
    elif args.cmd == "try-apply":
        return _try_apply(args.application_id, submit=args.submit, headless=args.headless)
    elif args.cmd == "seed-companies":
        from pathlib import Path

        from app import db
        from app.ingest.registry import seed_from_csv
        with db.worker_session() as s:
            print(f"seeded {seed_from_csv(s, Path(args.csv))} companies")
    elif args.cmd == "create-admin":
        return _create_admin(args.email)
    return 0


def _apply_workers(n: int | None) -> None:
    import multiprocessing as mp

    from app.apply.worker import run_forever
    from app.config import get_settings
    n = n or get_settings().apply_workers
    if n <= 1:
        run_forever()
        return
    procs = [mp.Process(target=run_forever, name=f"apply-{i}", daemon=False) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


def _try_apply(application_id: int, *, submit: bool, headless: bool) -> int:
    """Supervised live test of the application engine on one real posting.
    Without --submit the form is filled and left open for you to inspect;
    nothing is sent and no ledger/cooldown row is written."""
    from app.apply.browser import browser_session
    from app.apply.runner import run_application
    from app.llm.client import get_llm
    with browser_session(headed=not headless) as context:
        out = run_application(application_id, context, llm=get_llm(), submit=submit, keep_open=not headless)
        print(json.dumps({"status": out.status, "step": out.step, "reason": out.reason}, indent=2))
        if not submit and not headless:
            input("Form filled (not submitted). Inspect the browser, then press Enter to close... ")
    return 0


def _create_admin(email: str) -> int:
    from sqlalchemy import text

    from app import db
    from app.auth import hash_password, password_problems
    pw = getpass.getpass("Password: ")
    problems = password_problems(pw, email)
    if problems:
        print("Password needs: " + ", ".join(problems), file=sys.stderr)
        return 2
    with db.worker_session() as s:
        s.execute(text("""INSERT INTO users (email, password_hash, is_admin) VALUES (lower(:e), :p, true)
                          ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, is_admin = true"""),
                  {"e": email, "p": hash_password(pw)})
    print(f"admin {email} ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
