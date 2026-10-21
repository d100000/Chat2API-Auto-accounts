"""Run console HTTP checks against a disposable, offline application instance.

No running service, deployment .env, account data, or model requests are needed.
Run: python -m unittest discover -s tests -p 'test_local_console_http.py'
"""

import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from curl_cffi import requests


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_HOST = "127.0.0.1:5005"
CONSOLE_ORIGIN = "http://" + CONSOLE_HOST

# Run copied application sources rather than importing anything from the active
# deployment. Even an accidental forwarding attempt must fail inside this child.
SERVER_SCRIPT = r'''
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))

def block_network(*args, **kwargs):
    (root / "network-attempted").write_text("An outbound request was blocked.")
    raise AssertionError("Outbound connections are disabled in console HTTP tests")

async def block_async_network(*args, **kwargs):
    block_network()

socket.socket.connect = block_network
socket.socket.connect_ex = block_network

from curl_cffi.requests import AsyncSession, Session
AsyncSession.request = block_async_network
Session.request = block_network

import uvicorn

# Keep the socket bound while starting uvicorn so another process cannot claim
# the chosen port between discovery and startup. Never listen on port 5005.
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(128)
(root / "port").write_text(str(listener.getsockname()[1]))
try:
    config = uvicorn.Config("app:app", loop="asyncio", log_level="warning", access_log=False)
    uvicorn.Server(config).run(sockets=[listener])
finally:
    listener.close()
'''


class LocalConsoleHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sandbox = tempfile.TemporaryDirectory(prefix="chat2api-console-test-")
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.workdir = Path(cls.sandbox.name)

        # Explicit allowlist: never copy .env, data/, credentials, caches, or a
        # virtual environment out of the checkout being tested.
        for filename in ("app.py", "version.txt"):
            shutil.copy2(ROOT / filename, cls.workdir / filename)
        for package in ("api", "chatgpt", "utils"):
            for source in (ROOT / package).rglob("*.py"):
                target = cls.workdir / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        (cls.workdir / "templates").mkdir()
        for filename in ("local_console.html", "local_console_login.html", "tokens.html"):
            shutil.copy2(ROOT / "templates" / filename, cls.workdir / "templates" / filename)
        assets = cls.workdir / "static/local-console"
        assets.mkdir(parents=True)
        for filename in ("console.css", "console.js", "login.js"):
            shutil.copy2(ROOT / "static/local-console" / filename, assets / filename)
        (cls.workdir / ".env").write_text("", encoding="ascii")
        (cls.workdir / "data").mkdir()
        (cls.workdir / "data/token.txt").write_text("", encoding="ascii")
        (cls.workdir / "data/error_token.txt").write_text("", encoding="ascii")
        (cls.workdir / "run_test_server.py").write_text(SERVER_SCRIPT, encoding="utf-8")

        cls.login = {"username": "console-test", "password": secrets.token_urlsafe(24)}
        cls.api_key = "test-only-" + secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        rounds = 10000
        digest = hashlib.pbkdf2_hmac("sha256", cls.login["password"].encode(), salt, rounds).hex()
        password_hash = f"pbkdf2_sha256${rounds}${salt.hex()}${digest}"

        # Do not inherit deployment settings or proxy credentials from the shell.
        env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
        env.update({
            "PATH": os.defpath,
            "TMPDIR": str(cls.workdir),
            "TMP": str(cls.workdir),
            "TEMP": str(cls.workdir),
            "PYTHON_DOTENV_DISABLED": "true",
            "LOCAL_CONSOLE_ENABLED": "true",
            "LOCAL_CONSOLE_USERNAME": cls.login["username"],
            "LOCAL_CONSOLE_PASSWORD_HASH": password_hash,
            "AUTHORIZATION": cls.api_key,
            "CHATGPT_BASE_URL": "http://127.0.0.1:9",
            "ENABLE_GATEWAY": "false",
            "SCHEDULED_REFRESH": "false",
            "AUTO_SEED": "false",
            "API_PREFIX": "",
        })
        cls.server_log = (cls.workdir / "server.log").open("w+", encoding="utf-8")
        cls.addClassCleanup(cls.server_log.close)
        cls.process = subprocess.Popen(
            [sys.executable, "-I", str(cls.workdir / "run_test_server.py")],
            cwd=cls.workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=cls.server_log,
            stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls.stop_server)

        deadline = time.monotonic() + 20
        with cls.new_client() as client:
            while time.monotonic() < deadline:
                if cls.process.poll() is not None:
                    break
                port_file = cls.workdir / "port"
                if port_file.exists():
                    port = port_file.read_text().strip()
                    if port:
                        cls.base = f"http://127.0.0.1:{int(port)}"
                        try:
                            if client.get(cls.base + "/login", timeout=0.5).status_code == 200:
                                return
                        except requests.RequestsError:
                            pass
                time.sleep(0.05)
        cls.server_log.flush()
        log = (cls.workdir / "server.log").read_text(encoding="utf-8")
        raise RuntimeError("Isolated console failed to become ready:\n" + log[-6000:])

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    @classmethod
    def new_client(cls):
        return requests.Session(
            trust_env=False,
            headers={"Host": CONSOLE_HOST, "Origin": CONSOLE_ORIGIN},
            timeout=5,
            allow_redirects=False,
        )

    def setUp(self):
        self.client = self.new_client()
        self.addCleanup(self.client.close)

    def tearDown(self):
        self.assertFalse((self.workdir / "network-attempted").exists(), "Server attempted an outbound request")
        self.assertEqual((self.workdir / "data/token.txt").read_text(), "")
        self.assertEqual((self.workdir / "data/error_token.txt").read_text(), "")

    def log_in(self):
        response = self.client.post(self.base + "/local-console/login", json=self.login)
        self.assertEqual(response.status_code, 200)
        return response

    def test_unauthenticated_routes(self):
        for path in ("/playground", "/tokens"):
            with self.subTest(path=path):
                response = self.client.get(self.base + path)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 401)
        self.assertEqual(self.client.post(self.base + "/tokens/error").status_code, 401)
        body = {"messages": [{"role": "user", "content": "Never send this to a model."}]}
        self.assertEqual(self.client.post(self.base + "/local-console/chat", json=body).status_code, 401)

    def test_login_cookie_and_logout_revocation(self):
        response = self.log_in()
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=strict", response.headers["Set-Cookie"])
        old_cookie = self.client.cookies.get("chat2api_console")
        self.assertTrue(old_cookie)

        response = self.client.get(self.base + "/local-console/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], self.login["username"])
        self.assertEqual(response.json()["token_count"], 0)
        self.assertEqual(response.json()["error_token_count"], 0)
        page = self.client.get(self.base + "/playground")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")
        self.assertNotIn(self.login["password"], page.text + response.text)
        self.assertNotIn(self.api_key, page.text + response.text)
        self.assertEqual(self.client.get(self.base + "/tokens").status_code, 200)

        self.assertEqual(self.client.post(self.base + "/local-console/logout").status_code, 200)
        self.client.cookies.clear()
        self.client.headers["Cookie"] = "chat2api_console=" + old_cookie
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 401)

    def test_invalid_password_does_not_create_session(self):
        response = self.client.post(
            self.base + "/local-console/login",
            json={**self.login, "password": "incorrect-test-password"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("chat2api_console", self.client.cookies)
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 401)

    def test_origin_is_required_for_mutations(self):
        self.log_in()
        body = {"messages": [{"role": "user", "content": "Never send this to a model."}]}
        for origin in (None, "null", "https://example.invalid"):
            with self.subTest(origin=origin):
                self.client.headers.pop("Origin", None)
                if origin is not None:
                    self.client.headers["Origin"] = origin
                self.assertEqual(self.client.post(self.base + "/local-console/chat", json=body).status_code, 403)
                self.assertEqual(self.client.post(self.base + "/local-console/logout").status_code, 403)
                self.assertEqual(self.client.post(self.base + "/local-console/login", json=self.login).status_code, 403)
        self.client.headers["Origin"] = CONSOLE_ORIGIN
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 200)

    def test_foreign_host_is_rejected(self):
        self.client.headers["Host"] = "example.invalid"
        self.assertEqual(self.client.get(self.base + "/login").status_code, 400)
        self.assertEqual(self.client.post(self.base + "/local-console/login", json=self.login).status_code, 400)

    def test_empty_account_pool_and_input_validation(self):
        self.log_in()
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = {"messages": [{"role": "user", "content": "Never send this to a model."}], "stream": stream}
                response = self.client.post(self.base + "/local-console/chat", json=body)
                self.assertEqual(response.status_code, 503)
                self.assertIn("尚未配置", response.json()["detail"])
        invalid = {"messages": [], "max_tokens": 99999}
        self.assertEqual(self.client.post(self.base + "/local-console/chat", json=invalid).status_code, 422)


if __name__ == "__main__":
    unittest.main()
