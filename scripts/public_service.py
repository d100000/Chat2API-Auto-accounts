#!/usr/bin/env python3
"""Temporary API-only HTTPS access: public_service.py start|status|stop."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_FILE = DATA / "public-api.json"
GATEWAY_LOG = DATA / "public-api-gateway.log"
TUNNEL_LOG = DATA / "public-api-tunnel.log"
PUBLIC_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b")


def commands():
    binary = "/opt/homebrew/bin/cloudflared"
    if not Path(binary).is_file():
        binary = shutil.which("cloudflared") or binary
    gateway = [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.public_gateway:app",
               "--host", "127.0.0.1", "--port", "5006", "--no-access-log"]
    tunnel = [binary, "tunnel", "--url", "http://127.0.0.1:5006", "--no-autoupdate", "--protocol", "http2"]
    return gateway, tunnel


def saved_state():
    try:
        value = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("Invalid data/public-api.json; inspect it before continuing.") from error
    if not isinstance(value, dict):
        raise RuntimeError("Invalid public API state.")
    for name in ("gateway_pid", "tunnel_pid"):
        pid = value.get(name)
        if pid is not None and (type(pid) is not int or pid <= 1):
            raise RuntimeError("Unsafe PID in public API state; no action taken.")
    return value


def process_state(pid, command):
    if pid is None:
        return "stopped"
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("Unsafe PID; refusing to control it.")
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return "stopped"
    if result.returncode != 0:
        raise RuntimeError("Could not inspect process ownership.")
    if result.stdout.strip() != " ".join(command):
        return "unverified"
    cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, check=False)
    if cwd.returncode != 0 or f"n{ROOT}" not in cwd.stdout.splitlines():
        return "unverified"
    return "running"


def public_connection_info():
    """Read verified runtime status only; never load credentials or start processes."""
    stopped = {"public_status": "stopped", "public_base_url": None, "public_streaming": False}
    try:
        state = saved_state()
        url = state.get("url")
        if not isinstance(url, str) or PUBLIC_URL.fullmatch(url) is None:
            return stopped
        gateway, tunnel = commands()
        if process_state(state.get("gateway_pid"), gateway) != "running" or process_state(state.get("tunnel_pid"), tunnel) != "running":
            return stopped
        return {"public_status": "running", "public_base_url": f"{url}/v1", "public_streaming": False}
    except (OSError, RuntimeError):
        return stopped


def write_state(value):
    with tempfile.NamedTemporaryFile(mode="w", dir=DATA, prefix=".public-api-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, STATE_FILE)
        finally:
            temporary.unlink(missing_ok=True)


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", 5006), timeout=0.3):
            return True
    except OSError:
        return False


def gateway_ready(pid):
    if not port_open():
        return False
    listener = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP:5006", "-sTCP:LISTEN", "-Fn"], capture_output=True, text=True, check=False)
    return listener.returncode == 0 and "n127.0.0.1:5006" in listener.stdout.splitlines()


def validate_authorization():
    # Import and read dotenv only on an explicit start, never when imported by the UI.
    from dotenv import dotenv_values
    settings = dotenv_values(ROOT / ".env")
    key = os.environ.get("AUTHORIZATION", settings.get("AUTHORIZATION") or "")
    if not isinstance(key, str) or not any(item.strip() for item in key.split(",")):
        raise RuntimeError("AUTHORIZATION must contain an API key before public access can start.")


def spawn(command, logfile, environment):
    descriptor = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "ab") as log:
        os.fchmod(log.fileno(), 0o600)
        return subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def stop_owned(pid, command, child=None):
    state = process_state(pid, command)
    if state == "stopped":
        return
    if state != "running":
        raise RuntimeError("Process ownership changed; no signal sent.")
    os.kill(pid, signal.SIGTERM)
    for _ in range(25):
        if child is not None and child.poll() is not None:
            return
        state = process_state(pid, command)
        if state == "stopped":
            return
        if state != "running":
            raise RuntimeError("PID changed after SIGTERM; no further signal sent.")
        time.sleep(0.2)
    raise RuntimeError("Process is still exiting; inspect status before retrying.")


@contextmanager
def operation_lock():
    try:
        import fcntl
    except ImportError:
        raise RuntimeError("The temporary tunnel service script requires macOS or Linux.") from None
    DATA.mkdir(mode=0o700, exist_ok=True)
    DATA.chmod(0o700)
    descriptor = os.open(DATA / "public-api.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another public API operation is already running.") from error
        yield


def start():
    validate_authorization()
    gateway, tunnel = commands()
    state = saved_state()
    states = [process_state(state.get(name), command) for name, command in (("gateway_pid", gateway), ("tunnel_pid", tunnel))]
    if "unverified" in states:
        raise RuntimeError("Saved PID does not match this project's exact command and cwd; no action taken.")
    if all(item == "running" for item in states):
        info = public_connection_info()
        if info["public_status"] == "running":
            print(f"Already running: {info['public_base_url']} (streaming disabled)")
            return
    if "running" in states:
        raise RuntimeError("An incomplete public service is still running; use stop before start.")
    if not Path(gateway[0]).is_file() or not Path(tunnel[0]).is_file():
        raise RuntimeError("Missing .venv/bin/python or cloudflared; install dependencies first.")
    if port_open():
        raise RuntimeError("Port 127.0.0.1:5006 is occupied; no process started.")
    environment = {**os.environ, "PUBLIC_API_STREAMING": "false"}
    offset = TUNNEL_LOG.stat().st_size if TUNNEL_LOG.exists() else 0
    children = []
    state = {"url": None, "base_url": None, "started_at": datetime.now(timezone.utc).isoformat(), "gateway_pid": None, "tunnel_pid": None, "streaming": False}
    deadline = time.monotonic() + 45
    try:
        child = spawn(gateway, GATEWAY_LOG, environment)
        children.append((child, gateway))
        state = {**state, "gateway_pid": child.pid}
        write_state(state)
        while not gateway_ready(child.pid):
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Gateway did not become ready; inspect data/public-api-gateway.log.")
            time.sleep(0.2)
        child = spawn(tunnel, TUNNEL_LOG, environment)
        children.append((child, tunnel))
        state = {**state, "tunnel_pid": child.pid}
        write_state(state)
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process, _ in children):
                raise RuntimeError("Public service exited during startup; inspect public API logs.")
            with TUNNEL_LOG.open("rb") as log:
                log.seek(offset)
                output = log.read().decode("utf-8", errors="replace")
            match = PUBLIC_URL.search(output)
            if match and "Registered tunnel connection" in output:
                url = match.group(0)
                write_state({**state, "url": url, "base_url": f"{url}/v1"})
                print(f"Started: {url}/v1 (streaming disabled)\nTemporary URL changes after restart.\nLogs: {GATEWAY_LOG}, {TUNNEL_LOG}")
                return
            time.sleep(0.25)
        raise RuntimeError("No connected HTTPS tunnel within 45 seconds; inspect data/public-api-tunnel.log.")
    except BaseException:
        cleanup_failed = False
        for child, command in reversed(children):
            if child.poll() is None:
                try:
                    stop_owned(child.pid, command, child)
                    child.wait(timeout=1)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    cleanup_failed = True
        if not cleanup_failed:
            STATE_FILE.unlink(missing_ok=True)
        else:
            print("Some child processes could not be safely stopped; state and logs retained.", file=sys.stderr)
        raise


def main(action):
    os.umask(0o077)
    if action == "status":
        info = public_connection_info()
        print(f"Running: {info['public_base_url']} (streaming disabled)" if info["public_status"] == "running" else "Stopped")
        return
    with operation_lock():
        if action == "start":
            start()
            return
        state = saved_state()
        gateway, tunnel = commands()
        pairs = [(state.get("tunnel_pid"), tunnel), (state.get("gateway_pid"), gateway)]
        if any(process_state(pid, command) == "unverified" for pid, command in pairs):
            raise RuntimeError("Saved PID does not match this project's exact command and cwd; no action taken.")
        for pid, command in pairs:
            stop_owned(pid, command)
        STATE_FILE.unlink(missing_ok=True)
        print("Stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    try:
        main(parser.parse_args().action)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
