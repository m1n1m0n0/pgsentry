"""Command line entry point."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from pgsentry import __version__
from pgsentry.config import Config, ConfigError, load_config, load_password
from pgsentry.logging_setup import configure_logging

EXIT_CONFIG_ERROR = 3


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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
