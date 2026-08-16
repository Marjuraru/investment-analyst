#!/usr/bin/env python3
"""CLI utility to deploy, stage, update, rollback, and monitor independent local releases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from investment_analyst.application.local_release import (
    DEFAULT_ORIGIN_URL,
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_SERVICE_ENV_PATH,
    DEFAULT_SYSTEMD_UNIT_PATH,
    LocalReleaseError,
    LocalReleasePaths,
    LocalReleaseService,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy_local_release",
        description="Deploy and manage independent local releases for investment-analyst.",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Root directory for local release storage and mirrors",
    )
    parser.add_argument(
        "--repo-url",
        default=DEFAULT_ORIGIN_URL,
        help="Remote Git repository URL",
    )
    parser.add_argument(
        "--unit-file",
        type=Path,
        default=DEFAULT_SYSTEMD_UNIT_PATH,
        help="Path to the systemd user service unit file",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_SERVICE_ENV_PATH,
        help="Path to the private service environment file",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # stage
    stage_parser = subparsers.add_parser(
        "stage", help="Stage an immutable release for an exact SHA"
    )
    stage_parser.add_argument("--sha", required=True, help="Full 40-character commit SHA")

    # adopt-env
    adopt_parser = subparsers.add_parser(
        "adopt-env", help="Adopt private environment file with 0600 permissions"
    )
    adopt_parser.add_argument(
        "--source", required=True, type=Path, help="Path to existing .env source"
    )
    adopt_parser.add_argument(
        "--destination", type=Path, default=None, help="Target path for service.env"
    )

    # retarget-unit
    retarget_parser = subparsers.add_parser(
        "retarget-unit", help="Retarget existing systemd unit to a staged release"
    )
    retarget_parser.add_argument("--sha", required=True, help="Full 40-character commit SHA")

    # activate
    activate_parser = subparsers.add_parser(
        "activate", help="Activate a staged release with restart and health verification"
    )
    activate_parser.add_argument("--sha", required=True, help="Full 40-character commit SHA")
    activate_parser.add_argument("--port", type=int, default=8765, help="HTTP server port")
    activate_parser.add_argument(
        "--skip-systemd", action="store_true", help="Skip systemctl reload and restart"
    )
    activate_parser.add_argument(
        "--skip-health-check", action="store_true", help="Skip HTTP health check"
    )

    # update
    update_parser = subparsers.add_parser(
        "update", help="Stage and activate a release for an exact SHA"
    )
    update_parser.add_argument("--sha", required=True, help="Full 40-character commit SHA")
    update_parser.add_argument("--port", type=int, default=8765, help="HTTP server port")
    update_parser.add_argument(
        "--skip-systemd", action="store_true", help="Skip systemctl reload and restart"
    )
    update_parser.add_argument(
        "--skip-health-check", action="store_true", help="Skip HTTP health check"
    )

    # rollback
    rollback_parser = subparsers.add_parser(
        "rollback", help="Rollback to the verified previous release deployment"
    )
    rollback_parser.add_argument("--port", type=int, default=8765, help="HTTP server port")
    rollback_parser.add_argument(
        "--skip-systemd", action="store_true", help="Skip systemctl reload and restart"
    )
    rollback_parser.add_argument(
        "--skip-health-check", action="store_true", help="Skip HTTP health check"
    )

    # status
    status_parser = subparsers.add_parser(
        "status", help="Display local release runtime and unit status"
    )
    status_parser.add_argument("--port", type=int, default=8765, help="HTTP server port")
    status_parser.add_argument(
        "--no-systemd", action="store_true", help="Do not query systemd status"
    )
    status_parser.add_argument(
        "--no-http", action="store_true", help="Do not query HTTP health endpoint"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Output status report as structured JSON"
    )

    # bootstrap
    boot_parser = subparsers.add_parser(
        "bootstrap", help="Adopt environment, stage release, retarget unit, and activate"
    )
    boot_parser.add_argument("--sha", required=True, help="Full 40-character commit SHA")
    boot_parser.add_argument(
        "--env-source", type=Path, default=None, help="Path to source .env to adopt"
    )
    boot_parser.add_argument("--port", type=int, default=8765, help="HTTP server port")
    boot_parser.add_argument(
        "--skip-systemd", action="store_true", help="Skip systemctl reload and restart"
    )
    boot_parser.add_argument(
        "--skip-health-check", action="store_true", help="Skip HTTP health check"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    service = LocalReleaseService(
        paths=LocalReleasePaths.from_root(args.runtime_root),
        repo_url=args.repo_url,
        systemd_unit_path=args.unit_file,
        service_env_path=args.env_file,
    )

    try:
        if args.command == "stage":
            manifest = service.stage(args.sha)
            print(f"Staged release {manifest.commit_sha} at {manifest.release_path}")
            print(f"  Tree SHA: {manifest.tree_sha}")
            print(f"  uv.lock SHA256: {manifest.uv_lock_sha256}")
            print(f"  Python version: {manifest.python_version}")
            return 0

        elif args.command == "adopt-env":
            adopted = service.adopt_env(args.source, destination=args.destination)
            print(f"Adopted environment file at {adopted}")
            return 0

        elif args.command == "retarget-unit":
            service.retarget_unit(args.sha, unit_file=args.unit_file, env_file=args.env_file)
            print(f"Retargeted unit {args.unit_file} to release {args.sha}")
            return 0

        elif args.command == "activate":
            state = service.activate(
                sha=args.sha,
                unit_file=args.unit_file,
                env_file=args.env_file,
                port=args.port,
                skip_systemd=args.skip_systemd,
                skip_health_check=args.skip_health_check,
            )
            print(f"Activated release {state.current}")
            if state.previous:
                print(f"  Previous release: {state.previous}")
            return 0

        elif args.command == "update":
            state = service.update(
                sha=args.sha,
                unit_file=args.unit_file,
                env_file=args.env_file,
                port=args.port,
                skip_systemd=args.skip_systemd,
                skip_health_check=args.skip_health_check,
            )
            print(f"Updated and activated release {state.current}")
            if state.previous:
                print(f"  Previous release: {state.previous}")
            return 0

        elif args.command == "rollback":
            state = service.rollback(
                unit_file=args.unit_file,
                env_file=args.env_file,
                port=args.port,
                skip_systemd=args.skip_systemd,
                skip_health_check=args.skip_health_check,
            )
            print(f"Rolled back to release {state.current}")
            return 0

        elif args.command == "status":
            report = service.status(
                unit_file=args.unit_file,
                port=args.port,
                check_systemd=not args.no_systemd,
                check_http=not args.no_http,
            )
            if args.json:
                print(json.dumps(report.model_dump(mode="json"), indent=2))
            else:
                print("Local Release Runtime Status:")
                print(f"  Current Release SHA: {report.current_commit or 'none'}")
                print(f"  Previous Release SHA: {report.previous_commit or 'none'}")
                print(f"  Unit File: {report.unit_path}")
                print(f"  Unit Working Directory: {report.unit_working_directory or 'unknown'}")
                print(f"  Unit Environment File: {report.unit_environment_file or 'unknown'}")
                print(f"  Unit Matches Current: {report.unit_matches_current}")
                if report.service_active is not None:
                    print(f"  Service Active: {report.service_active}")
                if report.service_enabled is not None:
                    print(f"  Service Enabled: {report.service_enabled}")
                if report.overview_status is not None:
                    print(f"  HTTP Health Status (:8765): {report.overview_status}")
            return 0

        elif args.command == "bootstrap":
            if args.env_source is not None:
                service.adopt_env(args.env_source, destination=args.env_file)
            manifest = service.stage(args.sha)
            state = service.activate(
                sha=manifest.commit_sha,
                unit_file=args.unit_file,
                env_file=args.env_file,
                port=args.port,
                skip_systemd=args.skip_systemd,
                skip_health_check=args.skip_health_check,
            )
            print(f"Bootstrap complete for release {state.current} at {manifest.release_path}")
            return 0

        else:
            parser.print_help(sys.stderr)
            return 2

    except LocalReleaseError as error:
        print(f"deploy_local_release error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Unexpected error during release deployment: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
