"""One client request, independent of account retries and public forwarding.

Only numeric usage and opaque identifiers reach the ledger. Prompts, generated
text, account credentials and image bytes are never persisted here.
"""

import asyncio
import json
import logging
import time
from anyio import CancelScope

LOG = logging.getLogger(__name__)
MAX_CAPTURE = 2 * 1024 * 1024
_recording_error = False


def recording_status():
    return "incomplete" if _recording_error else "ok"


async def _wait_cleanup(task):
    interrupted = False
    with CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done() and task.cancelled():
                    raise
                interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


def _count(text, model):
    import tiktoken
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text, disallowed_special=()))


class RequestMetrics:
    def __init__(self, store=None, identifier=None, *, kind="chat", model="auto", pricing=None):
        self.store, self.id = store, identifier
        self.kind, self.model, self.pricing = kind, model, pricing
        self.services = ()
        self.text, self.capture_limited = "", False
        self.result_usage = None
        self.token_source = "local_estimate"
        self.chunk_meta = None
        self.error_seen = False
        self.finished = False
        self._finish_task = None

    def attach(self, service):
        self.services = (*self.services, service)

    def observe(self, chunk):
        if not isinstance(chunk, str) or not chunk.startswith("data:"):
            return
        try:
            value = json.loads(chunk[5:].strip())
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        if self.chunk_meta is None and isinstance(value.get("id"), str):
            self.chunk_meta = {key: value[key] for key in ("id", "created", "model") if key in value}
        self.error_seen = self.error_seen or bool(value.get("error"))
        for choice in value.get("choices", []):
            delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                remaining = MAX_CAPTURE - len(self.text)
                self.text += content[:max(0, remaining)]
                self.capture_limited = self.capture_limited or len(content) > remaining

    def observe_result(self, result):
        if isinstance(result, dict) and isinstance(result.get("usage"), dict):
            self.result_usage = result["usage"]
            if self.kind == "image" and "input_tokens" in self.result_usage and "output_tokens" in self.result_usage:
                self.result_usage = {"prompt_tokens": self.result_usage["input_tokens"],
                                     "completion_tokens": self.result_usage["output_tokens"]}
                self.token_source = "upstream_reported"

    async def usage(self):
        if self.result_usage is not None:
            prompt = self.result_usage.get("prompt_tokens")
            completion = self.result_usage.get("completion_tokens")
            if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
                return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        if self.kind != "chat" or self.capture_limited:
            return None
        service = self.services[-1] if self.services else None
        prompt = getattr(service, "prompt_tokens", None)
        if type(prompt) is not int or prompt < 0:
            return None
        completion = await asyncio.to_thread(_count, self.text, self.model)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}

    def outcome(self):
        if self.error_seen:
            return "failed"
        trace = getattr(self.services[-1], "usage_trace", None) if self.services else None
        return "succeeded" if trace and trace.complete else "interrupted"

    async def _finish(self, status, http_status, error_code):
        global _recording_error
        try:
            if self.store is None or self.id is None:
                return
            from chatgpt.pricing import estimate
            usage = await self.usage()
            evidence = [getattr(getattr(service, "usage_trace", None), "evidence", None) for service in self.services]
            assets = frozenset().union(*(item.generated_assets for item in evidence if item is not None))
            images = len(assets)
            image_requests = int(self.kind == "image" or any(item and item.image_requested for item in evidence))
            upstream = next((item.upstream_model for item in reversed(evidence) if item and item.upstream_model), None)
            # A webpage model label is not proof of model identity. The price
            # table only uses exact user-configured/requested identifiers.
            custom_auto = any(row.get("model") == "auto" for row in (self.pricing or {}).get("rates", []))
            price_model = self.model if self.model != "auto" or custom_auto else upstream or "auto"
            price = estimate(price_model, usage["prompt_tokens"] if usage else None,
                usage["completion_tokens"] if usage else None, images, self.kind, pricing=self.pricing,
                token_source=self.token_source)
            await asyncio.to_thread(self.store.finish, self.id, status=status, http_status=http_status,
                upstream_model=upstream, prompt_tokens=usage["prompt_tokens"] if usage else None,
                completion_tokens=usage["completion_tokens"] if usage else None, generated_images=images,
                image_requests=image_requests, token_source=self.token_source if usage else None,
                estimated_cost_usd=price.get("estimated_cost_usd"), price_source=price.get("price_source"),
                estimated_text_cost_usd=price.get("estimated_text_cost_usd"),
                estimated_image_cost_usd=price.get("estimated_image_cost_usd"), rate_version=price.get("rate_version"),
                pricing_status=price.get("pricing_status", "unknown"), error_code=error_code)
        except Exception:
            _recording_error = True
            LOG.error("Request analytics could not be finalized; dashboard coverage may be incomplete.")
        finally:
            self.text = ""
            self.services = ()
            self.finished = True

    async def finish(self, status, http_status=None, error_code=None):
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish(status, http_status, error_code))
        await _wait_cleanup(self._finish_task)


async def start_request(supplied_key, data, kind):
    metrics = RequestMetrics(kind=kind, model=data.get("model", "auto"))
    task = asyncio.create_task(_initialize_request(metrics, supplied_key))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _wait_cleanup(task)
        finally:
            await metrics.finish("cancelled", 499, "client_cancelled_during_setup")
        raise


async def _initialize_request(metrics, supplied_key):
    global _recording_error
    try:
        from chatgpt.analytics import get_store
        from chatgpt.api_keys import usage_key_id
        from chatgpt.pricing import get_pricing_config
        from utils.configs import authorization_list
        key_id = await asyncio.to_thread(usage_key_id, supplied_key, authorization_list)
        store = await asyncio.to_thread(get_store)
        identifier = await asyncio.to_thread(store.start, key_id=key_id, kind=metrics.kind, requested_model=metrics.model)
        metrics.store, metrics.id = store, identifier
        try:
            metrics.pricing = await asyncio.to_thread(get_pricing_config)
        except Exception:
            # Price configuration problems must not erase request/image counts
            # or silently replace a custom tariff with an official default.
            metrics.pricing = {"rates": [], "rate_version": "unavailable"}
            LOG.error("Pricing snapshot unavailable; this request will retain unknown cost.")
    except Exception:
        _recording_error = True
        LOG.error("Request analytics could not start; proxy generation is still available.")
    return metrics


async def measured_stream(metrics, service, response, *, include_usage=False):
    status = "interrupted"
    try:
        async for chunk in response:
            metrics.observe(chunk)
            if isinstance(chunk, str) and chunk.strip() == "data: [DONE]":
                continue
            if include_usage and isinstance(chunk, str) and chunk.startswith("data:"):
                try:
                    value = json.loads(chunk[5:].strip())
                    if isinstance(value, dict) and "choices" in value:
                        chunk = "data: " + json.dumps({**value, "usage": None}) + "\n\n"
                except (TypeError, ValueError):
                    pass  # Error/non-JSON events retain their existing handling.
            yield chunk
        status = metrics.outcome()
        if include_usage and status == "succeeded":
            usage = await metrics.usage()
            if usage is not None:
                yield "data: " + json.dumps({"object": "chat.completion.chunk", "created": int(time.time()),
                    "model": getattr(service, "resp_model", metrics.model), **(metrics.chunk_meta or {}),
                    "choices": [], "usage": usage}) + "\n\n"
        yield "data: [DONE]\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if status != "succeeded":
            status = "cancelled"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        async def finalize():
            try:
                if hasattr(response, "aclose"):
                    await response.aclose()
            finally:
                await metrics.finish(status, 200 if status == "succeeded" else 499 if status == "cancelled" else 502,
                                     None if status == "succeeded" else "stream_" + status)
        await _wait_cleanup(asyncio.create_task(finalize()))
