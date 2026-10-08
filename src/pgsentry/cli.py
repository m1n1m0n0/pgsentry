"""Command line entry point."""

from __future__ import annotations

import json
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console

from pgsentry import __version__
from pgsentry.config import Config, ConfigError, load_config, load_password
from pgsentry.exporter import serve
from pgsentry.logging_setup import configure_logging
from pgsentry.models import CheckResult, exit_code
from pgsentry.pg.runner import run_db_checks
from pgsentry.report import overall, print_results, results_to_json

EXIT_CONFIG_ERROR = 3

# Click exits with 2 for usage errors (an unknown flag, a missing argument). In this tool
# 2 means CRITICAL, and a CI job must be able to tell "the database is down" from "I typed
# the command wrong", so usage errors share the configuration-error code instead.
click.UsageError.exit_code = EXIT_CONFIG_ERROR


class PgsentryError(click.ClickException):
    """A problem the user can fix (bad config, missing file). Exits with code 3."""

    exit_code = EXIT_CONFIG_ERROR


@dataclass
class AppState:
    """Holds the CLI options. The config is loaded lazily, on first use.

    Group callbacks run even for `pgsentry db check --help`; loading lazily means help
    works without a valid config file.
    """

    config_path: Path | None
    log_level: str | None
    _config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = load_config(self.config_path)
            except ConfigError as exc:
                raise PgsentryError(str(exc)) from exc
            level = self.log_level or self._config.logging.level
            configure_logging(level, self._config.logging.json_format)
        return self._config


pass_state = click.make_pass_decorator(AppState)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pgsentry")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    envvar="PGSENTRY_CONFIG",
    help="YAML config file (or set PGSENTRY_CONFIG). Defaults are used if omitted.",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Override logging.level from the config.",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, log_level: str | None) -> None:
    """Health checks for PostgreSQL and security checks for Kubernetes pods.

    Exit codes: 0 ok, 1 warning (only with --fail-on warn), 2 critical, 3 bad configuration.
    """
    ctx.obj = AppState(config_path=config_path, log_level=log_level)


@main.command("config")
@pass_state
def show_config(state: AppState) -> None:
    """Validate the configuration and print the effective values.

    The password is never printed; only whether one was found.
    """
    try:
        has_password = load_password() is not None
    except ConfigError as exc:
        raise PgsentryError(str(exc)) from exc
    effective = json.loads(state.config.model_dump_json())
    effective["postgres"]["password"] = "(set)" if has_password else "(not set)"
    click.echo(json.dumps(effective, indent=2))


fail_on_option = click.option(
    "--fail-on",
    type=click.Choice(["warn", "crit", "never"]),
    default="crit",
    show_default=True,
    help="Lowest severity that makes the command exit non-zero.",
)
format_option = click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)


@main.group()
def db() -> None:
    """PostgreSQL health checks."""


@db.command("check")
@fail_on_option
@format_option
@pass_state
def db_check(state: AppState, fail_on: str, output_format: str) -> None:
    """Run all database checks once and print a report."""
    config = state.config
    try:
        password = load_password()
    except ConfigError as exc:
        raise PgsentryError(str(exc)) from exc
    results = run_db_checks(config, password)
    if output_format == "json":
        click.echo(results_to_json(results))
    else:
        target = f"{config.postgres.host}:{config.postgres.port}"
        print_results(f"PostgreSQL {target}", results, Console())
    sys.exit(exit_code(overall(results), fail_on))


@main.command("serve")
@pass_state
def serve_command(state: AppState) -> None:
    """Run the checks on a timer and expose them at /metrics (Prometheus format)."""
    config = state.config
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # `docker stop` / pod termination
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def collect() -> list[CheckResult]:
        # The password is re-read every cycle so a rotated mounted secret is picked up.
        return run_db_checks(config, load_password())

    try:
        serve(config.serve, collect, stop)
    except OSError as exc:
        raise PgsentryError(
            f"cannot listen on {config.serve.bind}:{config.serve.port}: {exc.strerror}"
        ) from exc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
