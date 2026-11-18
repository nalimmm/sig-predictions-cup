"""Post-deploy check: prove that the code on the server is the code that is running.

Lesson from the campaign (README, "What failed"): a patch validated in simulation was never actually
deployed, and nothing noticed. This script turns "I deployed it" into checks that fail loudly:

  1. FILES: every file in the manifest exists under --root with the expected SHA-256 (the manifest is
     written on the dev machine from the commit being deployed).
  2. UNITS: the installed systemd unit files are identical to the ones in deploy/.
  3. RESTART: each service is active and was (re)started AFTER its code file last changed; otherwise
     the old code is still in memory.
  4. BOT: since that restart the bot logged "Starting the basket bot in LIVE mode" and at least one
     "Round OK", with no "Error during the round" (waits up to --wait seconds for the first round).
  5. DASHBOARD: the page answers HTTP 200 on localhost.

Usage:
  python3 deploy/post_deploy_check.py --write-manifest manifest.sha256        (dev machine)
  python3 deploy/post_deploy_check.py --root /opt/predictions-cup \\
          --manifest /opt/predictions-cup/deploy/manifest.sha256 --wait 180   (server, as root)

Exit code 0 when every check passes, 1 otherwise. deploy/deploy.sh runs it after every deploy.
Standard library only, so it runs on a bare server.
"""
import argparse
import hashlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# systemd unit -> the code file it runs (relative to the deploy root)
SERVICES = {"basket-bot": "src/basket_bot.py", "dashboard": "src/dashboard.py"}
MANIFEST_FILES = sorted(SERVICES.values())
UNIT_DIR = Path("/etc/systemd/system")
BOT_LOG = "basket_bot.log"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_manifest(out, root=REPO, files=MANIFEST_FILES):
    Path(out).write_text("".join(f"{sha256(Path(root) / f)}  {f}\n" for f in files))


def read_manifest(path):
    entries = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            entries[name.strip()] = digest
    return entries


# ---------------------------------------------------------------- 1. files

def check_files(root, manifest):
    problems = []
    for name, digest in manifest.items():
        p = Path(root) / name
        if not p.exists():
            problems.append(f"{name}: missing on the server")
        elif sha256(p) != digest:
            problems.append(f"{name}: content differs from the commit being deployed")
    return problems


# ---------------------------------------------------------------- 2. unit files

def check_unit_files(root, unit_dir=UNIT_DIR, units=SERVICES):
    problems = []
    for unit in units:
        repo_unit, installed = Path(root) / "deploy" / f"{unit}.service", Path(unit_dir) / f"{unit}.service"
        if not installed.exists():
            problems.append(f"{unit}.service: not installed in {unit_dir}")
        elif repo_unit.exists() and sha256(repo_unit) != sha256(installed):
            problems.append(f"{unit}.service: installed unit differs from deploy/{unit}.service "
                            "(run systemctl daemon-reload after installing it)")
    return problems


# ---------------------------------------------------------------- 3. restart

def systemctl_show(unit, prop, run=subprocess.run):
    r = run(["systemctl", "show", unit, "-p", prop, "--value"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def parse_systemd_time(s):
    """'Wed 2026-11-18 22:00:00 UTC' -> naive local datetime (systemd prints local time)."""
    parts = s.split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def service_start(unit, run=subprocess.run):
    """(active?, start time) of a systemd unit."""
    active = systemctl_show(unit, "ActiveState", run) == "active"
    return active, parse_systemd_time(systemctl_show(unit, "ActiveEnterTimestamp", run))


def check_restart(root, starts, services=SERVICES):
    """starts: unit -> (active, start). The service must have started after its code last changed."""
    problems = []
    for unit, code in services.items():
        active, start = starts[unit]
        if not active:
            problems.append(f"{unit}: service is not active")
            continue
        if start is None:
            problems.append(f"{unit}: start time unreadable")
            continue
        p = Path(root) / code
        if p.exists():
            changed = datetime.fromtimestamp(p.stat().st_mtime).replace(microsecond=0)
            if changed > start:
                problems.append(f"{unit}: running since {start:%d/%m %H:%M:%S} but {code} changed at "
                                f"{changed:%d/%m %H:%M:%S}: the old code is still running, restart it")
    return problems


# ---------------------------------------------------------------- 4. bot log

def log_lines_since(log_path, since, now=None):
    """Lines of the bot log written at or after `since` ('dd/mm HH:MM:SS  message', no year)."""
    now = now or datetime.now()
    out = []
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            t = datetime.strptime(f"{now.year}/{line[:14]}", "%Y/%d/%m %H:%M:%S")
        except ValueError:
            continue
        if t > now + timedelta(days=1):   # a December line read in January
            t = t.replace(year=now.year - 1)
        if t >= since:
            out.append(line[14:].strip())
    return out


def check_bot_log(lines):
    problems = []
    if not any("Starting the basket bot in LIVE mode" in m for m in lines):
        problems.append("bot: no 'Starting the basket bot in LIVE mode' since the restart")
    if not any(m.startswith("Round OK") for m in lines):
        problems.append("bot: no 'Round OK' since the restart")
    errors = [m for m in lines if m.startswith("Error during the round")]
    if errors:
        problems.append(f"bot: {len(errors)} round error(s) since the restart, last: {errors[-1][:200]}")
    return problems


# ---------------------------------------------------------------- 5. dashboard

def dash_port(env_file):
    try:
        for line in Path(env_file).read_text().splitlines():
            if line.strip().startswith("DASH_PORT="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 80


def check_dashboard(root, port, urlopen=urllib.request.urlopen):
    token_file = Path(root) / "dashboard_token.txt"
    if not token_file.exists():
        return ["dashboard: dashboard_token.txt missing"]
    url = f"http://127.0.0.1:{port}/{token_file.read_text().strip()}"
    try:
        with urlopen(url, timeout=30) as r:
            code = r.status
    except Exception as e:  # HTTPError, URLError, timeout
        return [f"dashboard: no answer on port {port} ({getattr(e, 'code', e)})"]
    return [] if code == 200 else [f"dashboard: HTTP {code}"]


# ---------------------------------------------------------------- main

def run_checks(args, run=subprocess.run, urlopen=urllib.request.urlopen, sleep=time.sleep, clock=datetime.now):
    root = Path(args.root)
    problems = check_files(root, read_manifest(args.manifest))
    problems += check_unit_files(root, Path(args.unit_dir))
    starts = {u: service_start(u, run) for u in SERVICES}
    problems += check_restart(root, starts)

    bot_active, bot_start = starts["basket-bot"]
    if bot_active and bot_start:
        deadline = clock() + timedelta(seconds=args.wait)
        while True:   # the first round can take a minute or two: wait for it, but fail on an error
            log_problems = check_bot_log(log_lines_since(root / BOT_LOG, bot_start, clock()))
            done = not log_problems or any("round error" in p for p in log_problems)
            if done or clock() >= deadline:
                break
            sleep(10)
        problems += log_problems

    if not args.no_dashboard:
        problems += check_dashboard(root, dash_port(args.env_file), urlopen)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-manifest", metavar="FILE", help="write the manifest of this checkout and exit")
    ap.add_argument("--root", default="/opt/predictions-cup")
    ap.add_argument("--manifest")
    ap.add_argument("--unit-dir", default=str(UNIT_DIR))
    ap.add_argument("--env-file", default="/etc/predictions-cup.env")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the bot's first round")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args(argv)

    if args.write_manifest:
        write_manifest(args.write_manifest)
        print(f"Manifest written: {args.write_manifest}")
        return 0
    args.manifest = args.manifest or str(Path(args.root) / "deploy" / "manifest.sha256")

    problems = run_checks(args)
    if problems:
        print("POST-DEPLOY CHECK FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("POST-DEPLOY CHECK OK: deployed files match the commit, services restarted on them, "
          "bot rounds OK, dashboard up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
