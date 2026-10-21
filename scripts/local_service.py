#!/usr/bin/env python3
"""Local service control: python3 scripts/local_service.py start|status|stop."""

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PID_FILE = DATA / "server.pid"
LOG_FILE = DATA / "server.log"
COMMAND = [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app:app",
           "--host", "127.0.0.1", "--port", "5005", "--no-access-log"]


def saved_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
    except FileNotFoundError:
        return None
    except ValueError:
        raise RuntimeError("Invalid data/server.pid; inspect it before continuing.")
    if pid <= 1:
        raise RuntimeError("Unsafe PID in data/server.pid; refusing to continue.")
    return pid


def process_state(pid):
    """Only trust the exact server command and this repository's working directory."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return "stopped"
    if result.returncode != 0:
        raise RuntimeError("Could not inspect PID; refusing to control it.")
    if result.stdout.strip() != " ".join(COMMAND):
        return "unverified"
    cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True, check=False)
    if cwd.returncode != 0 or f"n{ROOT}" not in cwd.stdout.splitlines():
        return "unverified"
    return "running"


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", 5005), timeout=0.3):
            return True
    except OSError:
        return False


def main(action):
    os.umask(0o077)
    pid = saved_pid()
    state = process_state(pid) if pid else "stopped"
    if state == "unverified":
        raise RuntimeError(f"PID {pid} does not match this project's command and cwd; no action taken.")

    if action == "status":
        print(f"Running (PID {pid}) at http://127.0.0.1:5005" if state == "running" else "Stopped")
        return

    if action == "stop":
        if state == "stopped":
            print("Already stopped")
            return
        # Recheck ownership immediately before sending a signal.
        if process_state(pid) != "running":
            raise RuntimeError("Process changed; no signal sent.")
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            state = process_state(pid)
            if state == "stopped":
                if saved_pid() == pid:
                    PID_FILE.unlink()
                print("Stopped")
                return
            if state != "running":
                raise RuntimeError("PID changed after SIGTERM; no further action taken.")
            time.sleep(0.2)
        raise RuntimeError("Stop requested; process is still exiting. Check status before retrying.")

    if state == "running":
        print(f"Already running (PID {pid}) at http://127.0.0.1:5005")
        return
    if not Path(COMMAND[0]).is_file():
        raise RuntimeError("Missing .venv/bin/python; install project dependencies first.")
    if port_open():
        raise RuntimeError("Port 127.0.0.1:5005 is occupied; no process started.")
    DATA.mkdir(mode=0o700, exist_ok=True)
    DATA.chmod(0o700)
    with LOG_FILE.open("ab") as log:
        LOG_FILE.chmod(0o600)
        child = subprocess.Popen(COMMAND, cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.write_text(f"{child.pid}\n")
    PID_FILE.chmod(0o600)
    for _ in range(50):
        if child.poll() is not None:
            if saved_pid() == child.pid:
                PID_FILE.unlink()
            raise RuntimeError(f"Server exited ({child.returncode}); inspect {LOG_FILE}.")
        if port_open():
            print(f"Started (PID {child.pid}) at http://127.0.0.1:5005\nLog: {LOG_FILE}")
            return
        time.sleep(0.2)
    print(f"Starting (PID {child.pid}); port not ready yet. Check status and {LOG_FILE}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    args = parser.parse_args()
    try:
        main(args.action)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
