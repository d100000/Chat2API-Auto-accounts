"""Versioned API-equivalent estimates; never a statement of provider billing.

Capture get_pricing_config() when a request starts, pass that snapshot to the
pure estimate() function at completion, and persist its result in the request
ledger. Editing prices must not reprice past requests. Imports never read disk.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading


AS_OF = "2026-10-21"
SCHEMA_VERSION = 1
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "pricing.json"
MAX_CONFIG_BYTES = 128 * 1024
MAX_CUSTOM_RATES = 100
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,9})?\Z")
_MONEY_FIELDS = ("input_per_million_usd", "cached_input_per_million_usd", "output_per_million_usd", "image_per_unit_usd")
_LOCK = threading.RLock()
NOTICE = "API 等价估值，不是 OpenAI 账单；本地 token 计数可能为估算。未定价请求不按 0 美元计算；修改价格只影响新请求。"


class PricingError(ValueError):
    """Safe validation/storage messages suitable for the authenticated console."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _official(model, input_price, cached_price, output_price, *, source_model=None):
    # Exact API IDs only. Never infer a web model's identity from its spelling.
    return {"model": model, "kind": "text", "input_per_million_usd": input_price,
            "cached_input_per_million_usd": cached_price, "output_per_million_usd": output_price,
            "image_per_unit_usd": None, "source": "official", "source_url":
            "https://developers.openai.com/api/docs/models/" + (source_model or model), "as_of": AS_OF,
            "label": "官方 Standard 价格参考", "long_context_threshold": 272000,
            "long_context_input_multiplier": "2", "long_context_output_multiplier": "1.5"}


# Read and verified from the linked official model pages on AS_OF. These are
# Standard rates; no tool fees, cache writes, residency or speed-tier surcharges.
# GPT-5.6 Sol's current promotional rate is guaranteed at least through 2026-11-21.
_OFFICIAL = (
    _official("gpt-6-astra", "10", "1", "50"),
    _official("gpt-6.1-sol", "2", "0.10", "10"),
    _official("gpt-6-sol", "2", "0.20", "10"),
    _official("gpt-6-luna", "0.10", "0.01", "0.50"),
    _official("gpt-5.6-sol", "4", "0.40", "20"),
    _official("gpt-5.6-terra", "2", "0.20", "12"),
    _official("gpt-5.6-luna", "0.20", "0.02", "1.20"),
    _official("gpt-5.6", "4", "0.40", "20", source_model="gpt-5.6-sol"),
)


def _money(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PricingError("价格必须是非负十进制数或 null。")
    text = str(value)
    if not _NUMBER.fullmatch(text):
        raise PricingError("价格必须是非负定点数，最多 9 位小数，不能使用 NaN、Infinity 或科学计数法。")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise PricingError("价格格式无效。") from None
    if not amount.is_finite() or not 0 <= amount <= Decimal("1000000"):
        raise PricingError("单价必须介于 0 与 1000000 美元之间。")
    return format(amount.normalize(), "f")


def _custom_rates(rows):
    if not isinstance(rows, list) or len(rows) > MAX_CUSTOM_RATES:
        raise PricingError("custom_rates 必须是最多 100 项的数组。")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"model", "kind", "label", *_MONEY_FIELDS}:
            raise PricingError("价格项包含不支持的字段；来源与版本由服务端生成。")
        model, kind = row.get("model"), row.get("kind")
        if not isinstance(model, str) or not _MODEL.fullmatch(model) or kind not in ("text", "image"):
            raise PricingError("model 必须是最多 100 字符的模型标识，kind 必须是 text 或 image。")
        if (model, kind) in seen:
            raise PricingError("同一模型和计价类型不能重复。")
        seen.add((model, kind))
        label = row.get("label", "自定义估价")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 for c in label):
            raise PricingError("价格说明必须是 1 至 80 个字符的单行文本。")
        prices = {field: _money(row.get(field)) for field in _MONEY_FIELDS}
        if kind == "text":
            if prices["input_per_million_usd"] is None or prices["output_per_million_usd"] is None or prices["image_per_unit_usd"] is not None:
                raise PricingError("文本价格需要输入和输出单价；图片单价请另建 image 类型。")
        elif prices["image_per_unit_usd"] is None or any(prices[field] is not None for field in _MONEY_FIELDS[:-1]):
            raise PricingError("图片价格需要固定每张单价，不能混入文本 token 单价。")
        result.append({"model": model, "kind": kind, "label": label.strip(), **prices})
    return sorted(result, key=lambda row: (row["model"], row["kind"]))


def _configuration(custom=(), updated_at=None):
    rates = {(row["model"], row["kind"]): dict(row) for row in _OFFICIAL}
    as_of = updated_at[:10] if updated_at else AS_OF
    for row in custom:
        rates[(row["model"], row["kind"])] = {**row, "source": "custom", "source_url": None, "as_of": as_of,
            "long_context_threshold": None, "long_context_input_multiplier": "1", "long_context_output_multiplier": "1"}
    rows = [rates[key] for key in sorted(rates)]
    version = "pricing-v1-" + _digest(rows)
    return {"schema_version": SCHEMA_VERSION, "rate_version": version, "currency": "USD", "label": "API 等价估值",
            "updated_at": updated_at, "rates": [{**row, "rate_version": "rate-" + _digest(row)} for row in rows],
            "custom_rates": [dict(row) for row in custom], "notice": NOTICE}


def get_pricing_config(*, path=None):
    """Load a detached snapshot, or official defaults when no config exists."""
    target = Path(path) if path is not None else DEFAULT_PATH
    with _LOCK:
        try:
            if target.is_symlink() or target.stat().st_size > MAX_CONFIG_BYTES:
                raise PricingError("价格配置文件无效，请在后台重新保存。")
            value = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _configuration()
        except (OSError, ValueError, UnicodeError):
            raise PricingError("价格配置无法读取，请在后台重新保存；未采用隐性默认价格。") from None
        if not isinstance(value, dict) or set(value) != {"schema_version", "updated_at", "custom_rates"} or value["schema_version"] != SCHEMA_VERSION:
            raise PricingError("价格配置版本或结构无效。")
        stamp = value["updated_at"]
        try:
            if not isinstance(stamp, str) or len(stamp) > 40 or datetime.fromisoformat(stamp).tzinfo is None:
                raise ValueError
            date.fromisoformat(stamp[:10])
        except ValueError:
            raise PricingError("价格配置时间格式无效。") from None
        return _configuration(_custom_rates(value["custom_rates"]), stamp)


def update_pricing_config(payload, *, path=None):
    """Atomically replace all custom overrides. Does not touch the usage ledger."""
    if not isinstance(payload, dict) or set(payload) != {"custom_rates"}:
        raise PricingError("请求只能包含 custom_rates 数组。")
    rows = _custom_rates(payload["custom_rates"])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = {"schema_version": SCHEMA_VERSION, "updated_at": stamp, "custom_rates": rows}
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_CONFIG_BYTES:
        raise PricingError("价格配置过大。")
    target = Path(path) if path is not None else DEFAULT_PATH
    temporary = None
    with _LOCK:
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.is_symlink():
                raise PricingError("价格配置不能是符号链接。")
            fd, temporary = tempfile.mkstemp(prefix=".pricing-", dir=target.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        except OSError:
            raise PricingError("价格配置保存失败；原有配置未被部分覆盖。") from None
        finally:
            if temporary:
                os.unlink(temporary)
    return _configuration(rows, stamp)


def _count(value):
    return type(value) is int and 0 <= value <= 10**12


def _usd(value):
    with localcontext() as context:
        context.prec = 50
        return float(value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP))


def estimate(model, prompt_tokens, completion_tokens, generated_images, kind, *, pricing=None,
             cached_prompt_tokens=0, token_source="local_estimate"):
    """Pure calculation against an explicit snapshot, with unknown != free.

    Text prices exclude unobserved image/tool/cache-write costs. A chat response
    containing generated images is only fully priced if both text and image
    rates are configured for its exact model. Image requests use only their
    explicit per-image rate; no unsupported token-to-image conversion is made.
    """
    snapshot = _configuration() if pricing is None else pricing
    rates = snapshot.get("rates", []) if isinstance(snapshot, dict) else []
    version = snapshot.get("rate_version") if isinstance(snapshot, dict) else None
    result = {"estimated_cost_usd": None, "estimated_text_cost_usd": None, "estimated_image_cost_usd": None,
              "price_source": "unknown", "pricing_status": "unknown", "rate_version": version,
              "rate_snapshot": [], "cost_basis": "api_equivalent_estimate", "currency": "USD",
              "token_source": token_source if token_source in ("local_estimate", "upstream_reported", "unknown") else "unknown",
              "pricing_reason": "unknown_model", "assumptions": ["standard_rate", "no_unobserved_tool_or_cache_write_fees"]}
    if not isinstance(rates, list) or len(rates) > MAX_CUSTOM_RATES + len(_OFFICIAL):
        return {**result, "pricing_reason": "invalid_pricing"}
    if kind not in ("text", "chat", "image", "images") or not isinstance(model, str) or not _MODEL.fullmatch(model):
        return {**result, "pricing_reason": "invalid_usage"}
    if not _count(generated_images):
        return {**result, "pricing_reason": "missing_usage" if generated_images is None else "invalid_usage"}
    is_text = kind in ("text", "chat")
    if is_text and not all(_count(value) for value in (prompt_tokens, completion_tokens, cached_prompt_tokens)):
        return {**result, "pricing_reason": "missing_usage" if prompt_tokens is None or completion_tokens is None else "invalid_usage"}
    if is_text and cached_prompt_tokens > prompt_tokens:
        return {**result, "pricing_reason": "invalid_usage"}
    selected = {row.get("kind"): row for row in rates if isinstance(row, dict) and row.get("model") == model}
    text_cost = Decimal(0) if not is_text else None
    image_cost = Decimal(0) if generated_images == 0 else None
    used = []
    try:
        if is_text and (row := selected.get("text")) is not None:
            input_rate = _money(row.get("input_per_million_usd"))
            output_rate = _money(row.get("output_per_million_usd"))
            cached_rate = _money(row.get("cached_input_per_million_usd")) if cached_prompt_tokens else "0"
            if input_rate is not None and output_rate is not None and cached_rate is not None:
                threshold = row.get("long_context_threshold")
                long = type(threshold) is int and prompt_tokens > threshold
                input_multiplier = Decimal(row.get("long_context_input_multiplier", "1")) if long else Decimal(1)
                output_multiplier = Decimal(row.get("long_context_output_multiplier", "1")) if long else Decimal(1)
                if input_multiplier not in (Decimal(1), Decimal(2)) or output_multiplier not in (Decimal(1), Decimal("1.5")):
                    raise PricingError("无效价格快照。")
                text_cost = ((Decimal(prompt_tokens - cached_prompt_tokens) * Decimal(input_rate) + Decimal(cached_prompt_tokens) * Decimal(cached_rate)) * input_multiplier
                             + Decimal(completion_tokens) * Decimal(output_rate) * output_multiplier) / Decimal(1000000)
                used.append(dict(row))
                if long:
                    result["assumptions"] = [*result["assumptions"], "long_context_multiplier"]
        if (generated_images or not is_text) and (row := selected.get("image")) is not None:
            rate = _money(row.get("image_per_unit_usd"))
            if rate is not None:
                image_cost = Decimal(generated_images) * Decimal(rate)
                used.append(dict(row))
    except (PricingError, InvalidOperation, TypeError, ValueError):
        return {**result, "pricing_reason": "invalid_pricing"}
    # A zero-image attempt still needs a known image tariff; otherwise missing
    # price information would be silently reported as a free image request.
    if not is_text and selected.get("image") is None:
        image_cost = None
    sources = {row.get("source") for row in used}
    source = "custom" if "custom" in sources else "official" if sources == {"official"} else "unknown"
    known = text_cost is not None and image_cost is not None
    return {**result, "estimated_cost_usd": _usd(text_cost + image_cost) if known else None,
            "estimated_text_cost_usd": _usd(text_cost) if text_cost is not None else None,
            "estimated_image_cost_usd": _usd(image_cost) if image_cost is not None else None,
            "price_source": source, "pricing_status": "known" if known else "unknown", "rate_snapshot": used,
            "pricing_reason": "estimated" if known else "unpriced_image" if image_cost is None else "unknown_model"}
