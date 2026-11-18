"""Tests for deploy/post_deploy_check.py, on a fake server tree (no systemd, no network)."""
import os
from argparse import Namespace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import post_deploy_check as pdc

START = datetime(2026, 11, 3, 18, 0, 0)


@pytest.fixture
def server(tmp_path):
    """A deployed tree identical to this checkout, with unit files installed, code older than START."""
    root, units = tmp_path / "opt", tmp_path / "units"
    for rel in pdc.MANIFEST_FILES + [f"deploy/{u}.service" for u in pdc.SERVICES]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((pdc.REPO / rel).read_bytes())
    units.mkdir()
    for u in pdc.SERVICES:
        (units / f"{u}.service").write_bytes((pdc.REPO / "deploy" / f"{u}.service").read_bytes())
    old = (START - timedelta(minutes=5)).timestamp()
    for rel in pdc.MANIFEST_FILES:
        os.utime(root / rel, (old, old))
    (root / "dashboard_token.txt").write_text("tok\n")
    pdc.write_manifest(root / "manifest.sha256")
    return root, units


def log(root, *lines):
    with open(root / pdc.BOT_LOG, "a") as f:
        for t, msg in lines:
            f.write(f"{t:%d/%m %H:%M:%S}  {msg}\n")


def fake_systemctl(state="active", start=START):
    def run(cmd, **kw):
        prop = cmd[cmd.index("-p") + 1]
        value = state if prop == "ActiveState" else f"Tue {start:%Y-%m-%d %H:%M:%S} UTC"
        return SimpleNamespace(returncode=0, stdout=value + "\n")
    return run


class Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def args(root, units, wait=0):
    return Namespace(root=str(root), manifest=str(root / "manifest.sha256"), unit_dir=str(units),
                     env_file=str(root / "missing.env"), wait=wait, no_dashboard=False)


def check(server, run=None, urlopen=None, clock=None, wait=0):
    root, units = server
    urls = []

    def ok(url, timeout):
        urls.append(url)
        return Resp(200)
    problems = pdc.run_checks(args(root, units, wait), run=run or fake_systemctl(), urlopen=urlopen or ok,
                              sleep=lambda s: None, clock=clock or (lambda: START + timedelta(minutes=3)))
    return problems, urls


def healthy_log(root):
    log(root, (START - timedelta(hours=1), "Round OK: old run"),
        (START - timedelta(minutes=30), "Error during the round: from before the restart"),
        (START + timedelta(seconds=1), "Starting the basket bot in LIVE mode"),
        (START + timedelta(seconds=70), "Round OK: account 264,752"))


def test_all_green(server):
    healthy_log(server[0])
    problems, urls = check(server)
    assert problems == []
    assert urls == ["http://127.0.0.1:80/tok"]


def test_manifest_matches_this_checkout(server):
    entries = pdc.read_manifest(server[0] / "manifest.sha256")
    assert set(entries) == {"src/basket_bot.py", "src/dashboard.py"}
    assert entries["src/basket_bot.py"] == pdc.sha256(pdc.REPO / "src" / "basket_bot.py")


def test_patch_not_copied_is_caught(server):
    # The README's failure: the server still has the old file.
    root, _ = server
    healthy_log(root)
    (root / "src" / "basket_bot.py").write_text("# the version before the patch\n")
    os.utime(root / "src" / "basket_bot.py", ((START - timedelta(hours=1)).timestamp(),) * 2)
    problems, _ = check(server)
    assert problems == ["src/basket_bot.py: content differs from the commit being deployed"]


def test_missing_file_is_caught(server):
    root, _ = server
    healthy_log(root)
    (root / "src" / "dashboard.py").unlink()
    problems, _ = check(server)
    assert "src/dashboard.py: missing on the server" in problems


def test_copied_but_not_restarted_is_caught(server):
    root, _ = server
    healthy_log(root)
    later = (START + timedelta(minutes=2)).timestamp()
    os.utime(root / "src" / "basket_bot.py", (later, later))
    problems, _ = check(server)
    assert len(problems) == 1 and problems[0].startswith("basket-bot: running since")
    assert "the old code is still running" in problems[0]


def test_inactive_service_is_caught(server):
    problems, _ = check(server, run=fake_systemctl(state="failed"))
    assert "basket-bot: service is not active" in problems
    assert "dashboard: service is not active" in problems


def test_stale_unit_file_is_caught(server):
    root, units = server
    healthy_log(root)
    (units / "basket-bot.service").write_text("[Service]\nExecStart=/usr/bin/python3 basket_bot.py\n")
    problems, _ = check(server)
    assert problems == ["basket-bot.service: installed unit differs from deploy/basket-bot.service "
                        "(run systemctl daemon-reload after installing it)"]


def test_bot_in_simulation_or_silent_is_caught(server):
    root, _ = server
    log(root, (START + timedelta(seconds=1), "Starting the basket bot in SIMULATION mode"))
    problems, _ = check(server)
    assert "bot: no 'Starting the basket bot in LIVE mode' since the restart" in problems
    assert "bot: no 'Round OK' since the restart" in problems


def test_round_error_after_restart_is_caught(server):
    root, _ = server
    healthy_log(root)
    log(root, (START + timedelta(seconds=130), "Error during the round: GET /orders: 401 unauthorized"))
    problems, _ = check(server)
    assert problems == ["bot: 1 round error(s) since the restart, last: "
                        "Error during the round: GET /orders: 401 unauthorized"]


def test_waits_for_the_first_round(server):
    root, _ = server
    log(root, (START + timedelta(seconds=1), "Starting the basket bot in LIVE mode"))
    t = [START + timedelta(seconds=5)]

    def clock():
        t[0] += timedelta(seconds=10)
        if t[0] >= START + timedelta(seconds=60) and not (root / "round_written").exists():
            (root / "round_written").touch()
            log(root, (START + timedelta(seconds=60), "Round OK: first round"))
        return t[0]
    problems, _ = check(server, clock=clock, wait=180)
    assert problems == []


def test_gives_up_after_wait(server):
    root, _ = server
    log(root, (START + timedelta(seconds=1), "Starting the basket bot in LIVE mode"))
    t = [START]

    def clock():
        t[0] += timedelta(seconds=10)
        return t[0]
    problems, _ = check(server, clock=clock, wait=60)
    assert problems == ["bot: no 'Round OK' since the restart"]
    assert t[0] <= START + timedelta(seconds=90)


def test_dashboard_down_is_caught(server):
    healthy_log(server[0])

    def down(url, timeout):
        raise OSError("connection refused")
    problems, _ = check(server, urlopen=down)
    assert problems == ["dashboard: no answer on port 80 (connection refused)"]


def test_dashboard_port_from_env_file(tmp_path):
    env = tmp_path / "predictions-cup.env"
    env.write_text("SUPERMARKET_API_KEY=secret\n# comment\nDASH_PORT=8080\n")
    assert pdc.dash_port(env) == 8080
    assert pdc.dash_port(tmp_path / "missing") == 80


def test_parse_systemd_time():
    assert pdc.parse_systemd_time("Tue 2026-11-03 18:00:00 UTC") == START
    assert pdc.parse_systemd_time("") is None
    assert pdc.parse_systemd_time("n/a") is None


def test_log_year_rollover(tmp_path):
    p = tmp_path / "bot.log"
    p.write_text("31/12 23:59:00  Round OK: last of the year\n01/01 00:01:00  Round OK: first\n")
    lines = pdc.log_lines_since(p, datetime(2026, 12, 31, 23, 0), now=datetime(2027, 1, 1, 0, 5))
    assert lines == ["Round OK: last of the year", "Round OK: first"]


def test_main_exit_codes(server, capsys, monkeypatch):
    root, units = server
    monkeypatch.setattr(pdc, "run_checks", lambda a: [])
    assert pdc.main(["--root", str(root)]) == 0
    monkeypatch.setattr(pdc, "run_checks", lambda a: ["boom"])
    assert pdc.main(["--root", str(root)]) == 1
    assert "POST-DEPLOY CHECK FAILED" in capsys.readouterr().out
