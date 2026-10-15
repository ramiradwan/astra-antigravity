"""Headless per-user agent. ChatGPT owns setup and management; no local GUI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from companion import integration, maintenance, headless
from companion.controller import Controller, worker, DEFAULT_RELAY


def output(value, path=None):
    if path:
        worker.private_json(path, value)
    elif sys.stdout:
        print(json.dumps(value), flush=True)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    # A protocol link is one validated URI, never a way to inject local options.
    if "--connect-uri" in arguments and (len(arguments) != 2 or arguments[0] != "--connect-uri"):
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/astra-antigravity-companion")
    parser.add_argument("--relay", default=DEFAULT_RELAY)
    parser.add_argument("--request-id")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--portable", action="store_true", help="Do not install a source or review build")
    parser.add_argument("--install", action="store_true", help="Install for this Windows user and return setup JSON")
    parser.add_argument("--new-connection", action="store_true", help="With --install, explicitly archive old connection history for future tasks")
    parser.add_argument("--stop", action="store_true", help="Quit after current work and delivery finish")
    parser.add_argument("--uninstall", action="store_true", help="Remove this user's companion after current work")
    parser.add_argument("--retry-removal", action="store_true", help="Resend only a pending removal receipt with its narrow capability")
    parser.add_argument("--connect-uri")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--maintenance", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if sum((args.status, args.stop, args.uninstall, args.retry_removal)) > 1:
        return 1
    if args.new_connection and not args.install:
        return 1
    if args.connect_uri:
        try:
            args.request_id = integration.parse_connection_uri(args.connect_uri)
        except ValueError:
            return 1
    state = worker.private_directory(args.state)
    if args.retry_removal:
        try:
            output(maintenance.retry_pending(state))
            return 0
        except (ValueError, OSError):
            output({"status": "unconfirmed", "message": "Removal receipt needs local review; no task was started."})
            return 1
    if args.maintenance:
        try:
            maintenance.run(state, args.maintenance)
            return 0
        except Exception:
            return 1
    if args.status:
        output(headless.status(state), args.status_file)
        return 0
    live = headless.status(state)
    if live["status"] not in ("not_running", "unreachable"):
        if args.new_connection:
            output({"status": "rejected", "message": "Quit the current companion before creating a new connection."})
            return 1
        if args.stop or args.uninstall:
            result = headless.submit(state, "uninstall" if args.uninstall else "quit")
        else:
            if args.request_id:
                headless.submit(state, "attach_request", {"request_id": args.request_id})
            result = live
        if args.install or args.stop or args.uninstall:
            output(result)
        return 0
    if args.stop:
        output({"status": "not_running", "worker_running": False})
        return 0
    frozen = getattr(sys, "frozen", False)
    executable = Path(sys.executable).resolve()
    if frozen and executable == integration.launcher_executable().resolve():
        try:
            integration.launch(integration.selected_executable(), arguments if args.connect_uri else ["--managed", *arguments])
            if args.install:
                output(headless.wait_status(state))
            return 0
        except (ValueError, OSError):
            return 1
    installed_source = frozen and integration.owned_executable(executable)
    if not args.portable and not args.managed and not installed_source:
        installed = integration.install_current_app()
        if installed and executable != installed.resolve():
            integration.launch(installed, arguments if args.connect_uri else ["--managed", *arguments])
            if args.install:
                output(headless.wait_status(state))
            return 0
    if frozen and args.install and not args.managed:
        integration.launch(executable, ["--managed", "--background", *arguments])
        output(headless.wait_status(state))
        return 0
    control_state = worker.private_directory(state / "application")
    try:
        with worker.single_worker(control_state):
            missing_proof = not (state / "worker/auth.json").exists() and any((state / "worker" / name).exists() for name in ("worker.json", "worker.sqlite3"))
            retained_first = args.install and (missing_proof or args.new_connection)
            controller = Controller(state, relay=args.relay, request_id=None if retained_first else args.request_id,
                                    portable=args.portable)
            if args.install and (controller.snapshot()["status"] == "connection_invalid" or args.new_connection):
                controller.prepare_reinstall(new_request_id=args.request_id, explicit_recovery=args.new_connection)
            if args.install and controller.value["startup_available"]:
                controller.automatic_updates(True)
            return headless.run(controller, validation=args.validate_update, uninstall=args.uninstall)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        worker.private_json(state / "setup-diagnostic.json", {"stage": "startup", "error": type(error).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
