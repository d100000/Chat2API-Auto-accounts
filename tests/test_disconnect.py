"""HTTP disconnect lifecycle checks without sockets or account credentials."""

import asyncio
from types import SimpleNamespace
import unittest

from anyio import CancelScope

from utils.disconnect import ClientDisconnected, run_connected


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_shuts_down_listener_without_cancelling_result(self):
        listening, stopped = asyncio.Event(), asyncio.Event()

        async def receive():
            listening.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def operation():
            await listening.wait()
            return {"result": "completed"}

        result = await run_connected(SimpleNamespace(receive=receive), operation())
        self.assertEqual(result, {"result": "completed"})
        self.assertTrue(stopped.is_set())

    async def test_operation_exception_is_preserved(self):
        async def receive():
            await asyncio.Future()

        async def operation():
            raise ValueError("offline failure")

        with self.assertRaisesRegex(ValueError, "offline failure"):
            await run_connected(SimpleNamespace(receive=receive), operation())

    async def test_disconnect_cancels_once_and_waits_for_cleanup(self):
        received = asyncio.Queue()
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        attempts = []

        async def operation():
            attempts.append(1)
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        task = asyncio.create_task(run_connected(SimpleNamespace(receive=received.get), operation()))
        await started.wait()
        await received.put({"type": "http.disconnect"})
        await cleaning.wait()
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(ClientDisconnected):
            await task
        self.assertTrue(finished.is_set())
        self.assertEqual(attempts, [1])

    async def test_same_turn_completion_wins_over_disconnect(self):
        # Both tasks become ready before asyncio.wait resumes the helper.
        gate = asyncio.Event()

        async def receive():
            await gate.wait()
            return {"type": "http.disconnect"}

        async def operation():
            await gate.wait()
            return "completed once"

        task = asyncio.create_task(run_connected(SimpleNamespace(receive=receive), operation()))
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual(await task, "completed once")

    async def test_repeated_caller_cancel_does_not_abandon_cleanup(self):
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

        async def receive():
            await asyncio.Future()

        async def operation():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        task = asyncio.create_task(run_connected(SimpleNamespace(receive=receive), operation()))
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await task
        self.assertNotIsInstance(raised.exception, ClientDisconnected)
        self.assertTrue(finished.is_set())

    async def test_receive_failure_cancels_operation_and_preserves_failure(self):
        started, finished = asyncio.Event(), asyncio.Event()

        async def receive():
            await started.wait()
            raise RuntimeError("receive failed")

        async def operation():
            started.set()
            try:
                await asyncio.Future()
            finally:
                finished.set()

        with self.assertRaisesRegex(RuntimeError, "receive failed"):
            await run_connected(SimpleNamespace(receive=receive), operation())
        self.assertTrue(finished.is_set())

    async def test_get_initial_request_is_consumed_before_disconnect(self):
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"", "more_body": False})
        started, finished = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Future()
            finally:
                finished.set()

        task = asyncio.create_task(run_connected(SimpleNamespace(receive=queue.get), operation()))
        await started.wait()
        await queue.put({"type": "http.disconnect"})
        with self.assertRaises(ClientDisconnected):
            await task
        self.assertTrue(finished.is_set())

    async def test_anyio_cancel_scope_still_waits_for_operation_cleanup(self):
        started, finished = asyncio.Event(), asyncio.Event()

        async def receive():
            await asyncio.Future()

        async def operation():
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                finished.set()

        with CancelScope() as scope:
            asyncio.get_running_loop().call_soon(scope.cancel)
            await run_connected(SimpleNamespace(receive=receive), operation())
        self.assertTrue(started.is_set())
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
