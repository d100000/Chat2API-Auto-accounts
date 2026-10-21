"""Cancel a pending operation when its HTTP client has disconnected."""

import asyncio

from anyio import CancelScope


class ClientDisconnected(asyncio.CancelledError):
    """The HTTP client left; a route may return an empty 499 for access logs."""


async def run_connected(request, operation_awaitable):
    """Run once, while the caller still has a connection.

    Read the request body completely before calling this helper. Its watcher
    owns ``receive`` until the operation returns, so it must not overlap body
    parsing or a StreamingResponse's disconnect listener. GET callers without
    a body reader may let the watcher consume the initial http.request event.

    A disconnect raises CancelledError only after the operation's cancellation
    cleanup finishes. This stops local waiting; it cannot undo generation that
    an upstream service already accepted.
    """
    operation = asyncio.ensure_future(operation_awaitable)
    watcher_scope = None

    async def disconnected():
        nonlocal watcher_scope
        # A task.cancel() can coincide with an internal receive cancel scope
        # finishing and be consumed there. An outer watcher scope makes its
        # shutdown reliable for Starlette's BaseHTTPMiddleware receive wrapper.
        with CancelScope() as scope:
            watcher_scope = scope
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return
                # Queued GET body events may return without an await.
                await asyncio.sleep(0)

    watcher = asyncio.create_task(disconnected())
    try:
        await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
        # Both can finish in one event-loop turn. Never discard a completed
        # result or turn its exception into an apparent cancellation.
        if operation.done():
            return await operation
        await watcher  # Propagate an unexpected receive error, if any.
        raise ClientDisconnected
    finally:
        if not operation.done():
            operation.cancel()
        if watcher_scope is not None:
            watcher_scope.cancel()
        elif not watcher.done():
            watcher.cancel()
        cleanup = asyncio.gather(operation, watcher, return_exceptions=True)
        interrupted = False
        # Starlette middleware uses AnyIO's level-triggered cancellation. Its
        # shield lets these child tasks actually run while their parent scope
        # is cancelled; asyncio.shield additionally covers task.cancel().
        with CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not orphan a pending
                    # client close, scheduler lease, or usage finalization.
                    interrupted = True
        if interrupted:
            raise asyncio.CancelledError
