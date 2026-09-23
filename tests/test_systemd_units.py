"""Static checks for the dispatch-sweep systemd templates.

`systemd-analyze verify` is only possible on the Linux host at install time;
these checks pin what the repository can prove: the units mirror the API
unit's identity and configuration, carry no secrets, and invoke a CLI
command that actually exists.
"""

import configparser
import pathlib
import shlex

from app.cli import parser

UNIT_DIR = pathlib.Path(__file__).resolve().parents[1] / "deployment" / "systemd"
SERVICE = UNIT_DIR / "fazilabs-messaging-dispatch-sweep.service"
TIMER = UNIT_DIR / "fazilabs-messaging-dispatch-sweep.timer"
APP_ROOT = "/home/dev/app/fazilabs-messaging"


def load(path: pathlib.Path) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None, strict=True)
    unit.optionxform = str
    unit.read_string(path.read_text())
    return unit


def test_service_is_a_oneshot_mirroring_the_api_unit_identity() -> None:
    service = load(SERVICE)["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "dev"
    assert service["Group"] == "dev"
    assert service["WorkingDirectory"] == APP_ROOT
    assert service["EnvironmentFile"] == f"{APP_ROOT}/.env"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert "Restart" not in service


def test_service_runs_the_real_dispatch_sweep_cli_command() -> None:
    command = shlex.split(load(SERVICE)["Service"]["ExecStart"])
    assert command == [f"{APP_ROOT}/.venv/bin/python", "-m", "app.cli", "dispatch-sweep"]
    args = parser().parse_args(command[3:])
    assert args.handler.__name__ == "dispatch_sweep"


def test_units_contain_no_inline_configuration_or_secrets() -> None:
    for path in (SERVICE, TIMER):
        directives = [line for line in path.read_text().splitlines() if not line.startswith("#")]
        text = "\n".join(directives)
        assert "Environment=" not in text.replace("EnvironmentFile=", "")
        assert "APP_" not in text
        for word in ("token", "secret", "password", "api_key", "apikey"):
            assert word not in text.lower()


def test_timer_runs_the_service_every_minute_and_catches_up_after_downtime() -> None:
    timer = load(TIMER)
    assert timer["Timer"]["OnCalendar"] == "minutely"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Timer"]["Unit"] == SERVICE.name
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert "Install" not in load(SERVICE)
