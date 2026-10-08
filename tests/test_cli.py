from pathlib import Path

from click.testing import CliRunner

from pgsentry.cli import main


def test_help_works_without_any_config() -> None:
    result = CliRunner().invoke(main, ["--help"], prog_name="pgsentry")
    assert result.exit_code == 0
    assert "pgsentry" in result.output


def test_config_command_never_prints_the_password(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["config"], env={"PGSENTRY_DB_PASSWORD": "super-secret-value"}
    )
    assert result.exit_code == 0
    assert "super-secret-value" not in result.output
    assert '"password": "(set)"' in result.output


def test_bad_config_exits_with_code_3(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("postgres:\n  port: not-a-number\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["-c", str(bad), "config"])
    assert result.exit_code == 3
    assert "postgres.port" in result.output
