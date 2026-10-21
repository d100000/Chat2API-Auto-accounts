"""Offline lifecycle checks; never spawn a gateway or tunnel."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from scripts import public_service as service


class PublicServiceTests(unittest.TestCase):
    def test_public_info_requires_both_owned_processes_and_valid_url(self):
        state = {"url": "https://test-fixture.trycloudflare.com", "gateway_pid": 123, "tunnel_pid": 124}
        with patch.object(service, "saved_state", return_value=state), patch.object(service, "process_state", return_value="running"):
            self.assertEqual(service.public_connection_info(), {"public_status": "running", "public_base_url": state["url"] + "/v1", "public_streaming": False})
        with patch.object(service, "saved_state", return_value=state), patch.object(service, "process_state", side_effect=["running", "unverified"]):
            self.assertEqual(service.public_connection_info()["public_status"], "stopped")
        for url in ("https://test.trycloudflare.com.attacker.invalid", "http://test.trycloudflare.com", None):
            with patch.object(service, "saved_state", return_value={**state, "url": url}), patch.object(service, "process_state") as inspect:
                self.assertIsNone(service.public_connection_info()["public_base_url"])
                inspect.assert_not_called()

    def test_saved_state_rejects_unsafe_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "state.json"
            with patch.object(service, "STATE_FILE", file):
                for pid in (-1, 0, 1, True, "123"):
                    file.write_text(json.dumps({"gateway_pid": pid}))
                    with self.assertRaises(RuntimeError):
                        service.saved_state()

    def test_ownership_requires_exact_command_and_cwd(self):
        command = ["/fixture/python", "-m", "uvicorn"]
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "different command\n", "")) as run:
            self.assertEqual(service.process_state(123, command), "unverified")
            self.assertEqual(run.call_count, 1)
        results = [subprocess.CompletedProcess([], 0, " ".join(command) + "\n", ""), subprocess.CompletedProcess([], 0, "n/different/repository\n", "")]
        with patch.object(service.subprocess, "run", side_effect=results):
            self.assertEqual(service.process_state(123, command), "unverified")

    def test_stop_never_signals_unverified_pid(self):
        with patch.object(service, "process_state", return_value="unverified"), patch.object(service.os, "kill") as kill:
            with self.assertRaises(RuntimeError):
                service.stop_owned(123, ["safe-command"])
            kill.assert_not_called()

    def test_start_checks_key_before_any_spawn(self):
        with patch("dotenv.dotenv_values", return_value={}), patch.dict(service.os.environ, {"AUTHORIZATION": " , "}), patch.object(service, "spawn") as spawn, patch.object(service, "commands") as commands:
            with self.assertRaisesRegex(RuntimeError, "AUTHORIZATION"):
                service.start()
            commands.assert_not_called()
            spawn.assert_not_called()

    def test_state_permissions_and_atomic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            file = data / "public-api.json"
            with patch.object(service, "DATA", data), patch.object(service, "STATE_FILE", file):
                service.write_state({"gateway_pid": 123, "streaming": False})
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(file.read_text())["gateway_pid"], 123)
                self.assertEqual(list(data.iterdir()), [file])

    def test_tunnel_timeout_cleans_only_new_owned_children(self):
        gateway = Mock(pid=123); gateway.poll.return_value = None
        tunnel = Mock(pid=124); tunnel.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service, "STATE_FILE", Path(directory) / "state.json"), \
                patch.object(service, "TUNNEL_LOG", Path(directory) / "tunnel.log"), \
                patch.object(service, "validate_authorization"), \
                patch.object(service, "commands", return_value=(["gateway"], ["tunnel"])), \
                patch.object(service, "saved_state", return_value={}), \
                patch.object(service.Path, "is_file", return_value=True), \
                patch.object(service, "port_open", return_value=False), \
                patch.object(service, "gateway_ready", return_value=True), \
                patch.object(service, "spawn", side_effect=[gateway, tunnel]), \
                patch.object(service, "write_state"), \
                patch.object(service.time, "monotonic", side_effect=[0, 46]), \
                patch.object(service, "stop_owned") as stop:
            with self.assertRaisesRegex(RuntimeError, "45 seconds"):
                service.start()
            self.assertEqual(stop.call_args_list, [call(124, ["tunnel"], tunnel), call(123, ["gateway"], gateway)])
            gateway.wait.assert_called_once()
            tunnel.wait.assert_called_once()

    def test_gateway_readiness_checks_listener_owner(self):
        with patch.object(service, "port_open", return_value=True), patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertFalse(service.gateway_ready(123))
        with patch.object(service, "port_open", return_value=True), patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "p123\nn127.0.0.1:5006\n", "")):
            self.assertTrue(service.gateway_ready(123))

    def test_readiness_does_not_depend_on_local_dns_or_service_names(self):
        def lsof_result(command, **kwargs):
            numeric = "-nP" in command or ("-n" in command and "-P" in command)
            address = "127.0.0.1:5006" if numeric else "localhost:wsm-server"
            return subprocess.CompletedProcess(command, 0, "p123\nn" + address + "\n", "")
        with patch.object(service, "port_open", return_value=True), patch.object(service.subprocess, "run", side_effect=lsof_result):
            self.assertTrue(service.gateway_ready(123))


if __name__ == "__main__":
    unittest.main()
