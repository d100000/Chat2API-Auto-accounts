"""Run console HTTP checks against a disposable, offline application instance.

No running service, deployment .env, account data, or model requests are needed.
Run: python -m unittest discover -s tests -p 'test_local_console_http.py'
"""

import base64
import hashlib
import json
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
        for package in ("api", "chatgpt", "utils", "scripts", "gateway"):
            for source in (ROOT / package).rglob("*.py"):
                target = cls.workdir / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        (cls.workdir / "templates").mkdir()
        for filename in ("local_console.html", "local_console_login.html", "local_accounts.html", "tokens.html", "_workspace_header.html", "local_connection.html", "local_features.html", "local_dashboard.html", "login.html", "chatgpt.html", "chatgpt_context_1.json", "chatgpt_context_2.json", "gpts_context.json", "initialize.json"):
            shutil.copy2(ROOT / "templates" / filename, cls.workdir / "templates" / filename)
        assets = cls.workdir / "static/local-console"
        assets.mkdir(parents=True)
        for filename in ("console.css", "console.js", "login.js", "accounts.css", "accounts.js", "ui.js", "playground.css", "tokens.css", "tokens.js", "connection.css", "connection.js", "features.css", "features.js", "theme.js", "dashboard.css", "dashboard.js"):
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
            "LOCAL_QUOTA_AUTO_REFRESH_SECONDS": "0",
            "AUTHORIZATION": cls.api_key,
            "CHATGPT_BASE_URL": "http://127.0.0.1:9",
            "ENABLE_GATEWAY": "false",
            "SCHEDULED_REFRESH": "false",
            "AUTO_SEED": "true",
            "API_PREFIX": "",
        })
        cls.server_env = env
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
        for path in ("/playground", "/tokens", "/accounts", "/connection", "/features"):
            with self.subTest(path=path):
                response = self.client.get(self.base + path)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 401)
        self.assertEqual(self.client.get(self.base + "/local-console/accounts").status_code, 401)
        for path in ("/local-console/connection", "/local-console/features", "/local-console/api-keys"):
            self.assertEqual(self.client.get(self.base + path).status_code, 401)
        self.assertEqual(self.client.post(self.base + "/local-console/connection/key").status_code, 401)
        self.assertEqual(self.client.post(self.base + "/local-console/api-keys", json={"name": "Denied"}).status_code, 401)
        self.assertEqual(self.client.post(self.base + "/local-console/accounts/refresh", json={}).status_code, 401)
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
                self.assertEqual(self.client.post(self.base + "/local-console/accounts/refresh", json={}).status_code, 403)
                self.assertEqual(self.client.post(self.base + "/local-console/login", json=self.login).status_code, 403)
                self.assertEqual(self.client.post(self.base + "/local-console/connection/key").status_code, 403)
                self.assertEqual(self.client.post(self.base + "/local-console/api-keys", json={"name": "Denied"}).status_code, 403)
                self.assertEqual(self.client.delete(self.base + "/local-console/api-keys/missing").status_code, 403)
        self.client.headers["Origin"] = CONSOLE_ORIGIN
        self.assertEqual(self.client.get(self.base + "/local-console/session").status_code, 200)

    def test_foreign_host_is_rejected(self):
        self.client.headers["Host"] = "example.invalid"
        self.assertEqual(self.client.get(self.base + "/login").status_code, 400)
        self.assertEqual(self.client.get(self.base + "/accounts").status_code, 400)
        self.assertEqual(self.client.get(self.base + "/connection").status_code, 400)
        self.assertEqual(self.client.get(self.base + "/features").status_code, 400)
        self.assertEqual(self.client.get(self.base + "/local-console/accounts").status_code, 400)
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

    def test_connection_secret_requires_explicit_authenticated_post(self):
        self.log_in()
        for path in ("/connection", "/features", "/local-console/connection", "/local-console/features", "/local-console/api-keys"):
            response = self.client.get(self.base + path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn(self.api_key, response.text)
            self.assertNotIn(self.login["password"], response.text)
        response = self.client.post(self.base + "/local-console/connection/key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"api_key": self.api_key})
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertNotEqual(self.client.get(self.base + "/local-console/connection/key").status_code, 200)

    def test_managed_keys_lifecycle_and_immediate_authentication(self):
        self.log_in()
        endpoint = self.base + "/local-console/api-keys"
        self.assertEqual(self.client.post(endpoint, json={"name": "   "}).status_code, 422)
        result = self.client.post(endpoint, json={"name": "Offline client"})
        self.assertEqual(result.status_code, 200)
        created = result.json()
        plaintext, identifier = created["api_key"], created["key"]["id"]
        headers = {"Authorization": "Bearer " + plaintext}
        self.assertEqual(self.client.get(self.base + "/v1/models", headers=headers).status_code, 200)
        listing = self.client.get(endpoint)
        self.assertNotIn(plaintext, listing.text)
        self.assertNotIn("key_hash", listing.text)
        row = next(row for row in listing.json()["keys"] if row["id"] == identifier)
        self.assertEqual(row["request_count"], 0)
        # A valid key routes to the empty pool without making an upstream call.
        body = {"messages": [{"role": "user", "content": "Offline only"}], "model": "auto", "stream": False}
        self.assertEqual(self.client.post(self.base + "/v1/chat/completions", headers=headers, json=body).status_code, 429)
        row = next(row for row in self.client.get(endpoint).json()["keys"] if row["id"] == identifier)
        self.assertEqual(row["request_count"], 1)
        self.assertIsNotNone(row["last_used_at"])
        self.assertEqual(self.client.patch(endpoint + "/" + identifier, json={"status": "disabled"}).status_code, 200)
        for path, method, arguments in (("/v1/models", "get", {}), ("/v1/chat/completions", "post", {"json": body})):
            self.assertEqual(getattr(self.client, method)(self.base + path, headers=headers, **arguments).status_code, 401)
        self.assertEqual(self.client.patch(endpoint + "/" + identifier, json={"status": "active"}).status_code, 200)
        self.assertEqual(self.client.get(self.base + "/v1/models", headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(endpoint + "/" + identifier).status_code, 200)
        self.assertEqual(self.client.get(self.base + "/v1/models", headers=headers).status_code, 401)
        self.assertEqual(self.client.patch(endpoint + "/" + identifier, json={"status": "active"}).status_code, 409)
        self.client.cookies.clear()
        self.assertEqual(self.client.delete(endpoint + "/" + identifier).status_code, 401)

    def test_reserved_managed_key_and_model_list_reject_unknown_keys(self):
        for key in ("sk-c2a-invalid", "sk-c2a-", "not-configured"):
            headers = {"Authorization": "Bearer " + key}
            self.assertEqual(self.client.get(self.base + "/v1/models", headers=headers).status_code, 401)
            if key.startswith("sk-c2a-"):
                self.assertEqual(self.client.post(self.base + "/v1/chat/completions", headers=headers, json={}).status_code, 401)
        self.assertEqual(self.client.get(self.base + "/v1/models", headers={"Authorization": "Bearer " + self.api_key}).status_code, 200)

    def test_openai_error_has_request_identifier_without_exposing_credentials(self):
        response = self.client.post(self.base + "/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={"model": "auto", "messages": [{"role": "developer", "content": "Offline only"},
                {"role": "user", "content": "Offline only"}], "max_completion_tokens": 8, "stream": False})
        self.assertEqual(response.status_code, 429)
        self.assertRegex(response.headers.get("x-request-id", ""), r"^req_[0-9a-f]{32}$")
        self.assertIn("error", response.json())
        self.assertNotIn(self.api_key, response.text)

    def test_openai_models_and_image_key_lifecycle(self):
        self.log_in()
        endpoint = self.base + "/local-console/api-keys"
        created = self.client.post(endpoint, json={"name": "Offline image client"}).json()
        headers = {"Authorization": "Bearer " + created["api_key"]}
        catalog = self.client.get(self.base + "/v1/models", headers=headers).json()
        self.assertEqual(catalog["object"], "list")
        by_id = {row["id"]: row for row in catalog["data"]}
        self.assertEqual(by_id["auto"]["type"], "chat")
        self.assertEqual(by_id["gpt-image-2"]["type"], "image")
        self.assertIn("chatgpt-image-latest", by_id)
        detail = self.client.get(self.base + "/v1/models/gpt-image-2", headers=headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json(), by_id["gpt-image-2"])
        self.assertEqual(self.client.get(self.base + "/v1/models/unknown", headers=headers).status_code, 404)
        for model in ("gpt-image-2", "chatgpt-image-latest", "gpt-unknown-model"):
            reply = self.client.post(self.base + "/v1/chat/completions", headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "Offline only"}]})
            self.assertIn(reply.status_code, (400, 404))
            self.assertIn("error", reply.json())
        bad = self.client.post(self.base + "/v1/images/generations", headers=headers,
            json={"prompt": "Offline only", "n": 2})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["param"], "n")
        row = next(row for row in self.client.get(endpoint).json()["keys"] if row["id"] == created["key"]["id"])
        self.assertEqual(row["request_count"], 0)
        response = self.client.post(self.base + "/v1/images/generations", headers=headers,
            json={"model": "chatgpt-image-latest", "prompt": "Offline only"})
        self.assertEqual(response.status_code, 429)
        row = next(row for row in self.client.get(endpoint).json()["keys"] if row["id"] == created["key"]["id"])
        self.assertEqual(row["request_count"], 1)
        self.client.patch(endpoint + "/" + created["key"]["id"], json={"status": "disabled"})
        self.assertEqual(self.client.post(self.base + "/v1/images/generations", headers=headers, json={"prompt": "Offline"}).status_code, 401)
        self.client.delete(endpoint + "/" + created["key"]["id"])
        self.assertEqual(self.client.post(self.base + "/v1/images/generations", headers=headers, json={"prompt": "Offline"}).status_code, 401)

    def test_original_web_gateway_can_coexist_with_admin_console(self):
        previous_port = (self.workdir / "port").read_text()
        with (self.workdir / "gateway-test.log").open("w+") as log:
            child = subprocess.Popen([sys.executable, "-I", str(self.workdir / "run_test_server.py")],
                cwd=self.workdir, env={**self.server_env, "ENABLE_GATEWAY": "true"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                ready = False
                while time.monotonic() < deadline and child.poll() is None:
                    port = (self.workdir / "port").read_text().strip()
                    if port and port != previous_port.strip():
                        base = "http://127.0.0.1:" + port
                        try:
                            ready = self.client.get(base + "/login", timeout=.5).status_code == 200
                        except requests.RequestsError:
                            pass
                        if ready:
                            break
                    time.sleep(.05)
                log.flush()
                self.assertTrue(ready, (self.workdir / "gateway-test.log").read_text()[-3000:])
                self.assertIn("登录工作区", self.client.get(base + "/login").text)
                for route in ("/gateway", "/gateway/login", "/gpts"):
                    response = self.client.get(base + route)
                    self.assertEqual(response.status_code, 200, route)
                    self.assertIn("/gateway?token=", response.text)
                self.assertEqual(self.client.get(base + "/seedtoken").status_code, 403)
                self.assertEqual(self.client.get(base + "/docs").status_code, 200)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

    def test_multimodal_chat_validation_and_image_quota_hint(self):
        self.log_in()
        body = {"model": "auto", "history_disabled": False, "quota_feature": "image_gen", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Offline validation only"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]}]}
        self.assertEqual(self.client.post(self.base + "/local-console/chat", json=body).status_code, 503)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html;base64,AAAA", "https://user:secret@example.invalid/image"):
            invalid = {**body, "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}}]}]}
            self.assertEqual(self.client.post(self.base + "/local-console/chat", json=invalid).status_code, 422)
        self.assertEqual(self.client.post(self.base + "/local-console/chat", json={**body, "quota_feature": "invalid"}).status_code, 422)

    def test_accounts_empty_state_refresh_and_invalid_identifier(self):
        self.log_in()
        page = self.client.get(self.base + "/accounts")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")
        self.assertIn("accounts.js", page.text)
        for method, path in (("get", "/local-console/accounts"), ("post", "/local-console/accounts/refresh")):
            response = getattr(self.client, method)(self.base + path, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["accounts"], [])
            self.assertEqual(response.json()["auto_refresh_seconds"], 0)
            scheduling = response.json()["scheduler"]
            self.assertEqual(scheduling["policy"], "quota_aware")
            self.assertEqual(scheduling["accounts"], {})
            self.assertEqual(scheduling["events"], [])
        self.assertEqual(self.client.post(self.base + "/local-console/accounts/refresh", json={"account_id": "invalid"}).status_code, 422)
        self.assertEqual(self.client.post(self.base + "/local-console/accounts/refresh", json={"account_id": "acct_" + "0" * 24}).status_code, 404)

    def test_chat_routing_validation_precedes_account_selection(self):
        headers = {"Authorization": "Bearer " + self.api_key}
        body = {"messages": [{"role": "user", "content": "Never generate anything."}], "model": "auto"}
        for change in ({"quota_feature": "invalid"}, {"conversation_id": "existing"},
                       {"Chatgpt-Account-Id": "workspace"}, {"messages": [None]}):
            response = self.client.post(self.base + "/v1/chat/completions", headers=headers, json={**body, **change})
            self.assertEqual(response.status_code, 400)
        self.assertFalse((self.workdir / "network-attempted").exists())

    def test_expired_account_stays_unknown_without_exposing_token(self):
        self.log_in()
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        credential = ".".join((encode({"alg": "none"}), encode({"exp": 1, "sub": "offline-test-user"}), "test-signature"))
        try:
            response = self.client.post(self.base + "/tokens/upload", data={"text": credential})
            self.assertEqual(response.status_code, 200)
            response = self.client.post(self.base + "/local-console/accounts/refresh", json={})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(credential, response.text)
            self.assertNotIn("offline-test-user", response.text)
            account = response.json()["accounts"][0]
            self.assertEqual(account["credential_status"], "expired")
            self.assertIsNone(account["features"]["reason"]["remaining"])
            self.assertIsNone(account["features"]["image_gen"]["remaining"])
            self.assertEqual(account["local_usage"]["totals"]["attempts"], 0)
        finally:
            self.client.post(self.base + "/tokens/clear")


if __name__ == "__main__":
    unittest.main()
