from __future__ import annotations

import argparse
import json
import os
import sys

from .intent import parse_investment_intent
from .runtime import build_runtime_from_env
from .doctor import run_preflight
from .data_lock import RuntimeDataLock
from .backup import backup_databases, restore_databases
from .job_admin import job_status, reset_job
from .lifecycle import run_polling
from .env_file import EnvFileError, load_environment_file
from .connectivity import run_connectivity_diagnostic, run_market_connectivity
from .local_strategy_report import read_local_strategy_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock guide agent")
    parser.add_argument("message", nargs="?")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run-bot", action="store_true")
    parser.add_argument("--once", action="store_true")
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--check-telegram", action="store_true")
    modes.add_argument("--check-market", action="store_true")
    modes.add_argument("--jobs-status", action="store_true")
    modes.add_argument("--reset-job", metavar="JOB_ID")
    modes.add_argument("--backup-to", metavar="NEW_DIRECTORY")
    modes.add_argument("--restore-from", metavar="BACKUP_DIRECTORY")
    modes.add_argument(
        "--strategy-report",
        nargs=2,
        metavar=("STRATEGY_ID", "VERSION"),
    )
    parser.add_argument("--restore-to", metavar="NEW_DATA_DIRECTORY")
    parser.add_argument("--env-file", metavar="LOCAL_FILE")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    if args.once and not args.run_bot:
        parser.error("--once requires --run-bot")
    if bool(args.restore_from) != bool(args.restore_to):
        parser.error("--restore-from and --restore-to must be supplied together")
    if args.env_file:
        try:
            load_environment_file(args.env_file)
        except EnvFileError:
            print("Unable to load environment file. Check its local path and syntax.", file=sys.stderr)
            raise SystemExit(1) from None
    if args.data_dir is None:
        args.data_dir = os.environ.get("AGENT_DATA_DIR", ".")
    if args.strategy_report:
        if args.message:
            parser.error("--strategy-report cannot be combined with a message")
        try:
            rendered = read_local_strategy_report(
                args.data_dir,
                args.strategy_report[0],
                args.strategy_report[1],
                calendar_overrides_path=os.environ.get("KRX_CALENDAR_OVERRIDES") or None,
            )
        except Exception:
            print(
                "전략 보고서를 읽을 수 없습니다. 데이터베이스와 전략 ID·버전을 확인하세요.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        print(rendered)
        return
    if args.check_telegram or args.check_market:
        try:
            if args.check_telegram:
                report = run_connectivity_diagnostic(os.environ)
            else:
                with RuntimeDataLock(args.data_dir):
                    report = run_market_connectivity(os.environ)
        except Exception:
            print("Connection check failed. Check configuration, data access and whether the bot is stopped.", file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if not report.ready:
            raise SystemExit(1)
        return
    if args.jobs_status or args.reset_job or args.backup_to or args.restore_from:
        try:
            if args.jobs_status:
                result = job_status(args.data_dir)
            elif args.reset_job:
                reset_job(args.data_dir, args.reset_job)
                result = {"reset": args.reset_job}
            elif args.backup_to:
                manifest = backup_databases(args.data_dir, args.backup_to)
                result = {"backed_up_databases": len(manifest["files"])}
            else:
                manifest = restore_databases(args.restore_from, args.restore_to)
                result = {"restored_databases": len(manifest["files"])}
        except Exception:
            print("Maintenance failed. Check paths, database integrity and whether the bot is stopped.", file=sys.stderr)
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.doctor:
        report = run_preflight(os.environ)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if not report.ready:
            raise SystemExit(1)
        return
    if args.run_bot:
        os.environ["AGENT_DATA_DIR"] = args.data_dir
        try:
            lock = RuntimeDataLock(args.data_dir)
            lock.__enter__()
        except Exception:
            print("Bot data directory is unavailable or already in use.", file=sys.stderr)
            raise SystemExit(1) from None
        try:
            try:
                runtime = build_runtime_from_env()
            except Exception:
                print("Bot startup failed. Run --doctor and check local configuration and data access.", file=sys.stderr)
                raise SystemExit(1) from None
            try:
                run_polling(runtime, once=args.once, on_once=lambda result: print(
                    json.dumps({"events": len(result.events), "failures": result.failures})
                ))
            except Exception:
                print("Bot stopped after an internal error. Check local configuration and job status.", file=sys.stderr)
                raise SystemExit(1) from None
            return
        finally:
            lock.__exit__(None, None, None)
    if not args.message:
        parser.error("message is required unless --run-bot or --doctor is used")
    print(
        json.dumps(
            parse_investment_intent(args.message).to_dict(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
