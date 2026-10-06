"""Builds and reads LLM HTTP requests. It does no networking itself: the caller sends what these return.

There are two wire formats. Anthropic has its own Messages API. Every other provider (OpenRouter,
Nano-GPT, custom endpoints, local servers) speaks OpenAI-compatible chat completions, so a provider
there only decides the default base URL.

Raw HTTP instead of a vendor SDK because the game must run on Ren'Py's bundled Python on every
platform, including the web build, where extra packages are not available.
"""

import json

PROVIDERS = (
    ("anthropic", "Anthropic"),
    ("openrouter", "OpenRouter"),
    ("nanogpt", "Nano-GPT"),
    ("custom", "Custom"),
    ("local", "Local model"),
)

DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "nanogpt": "https://nano-gpt.com/api/v1",
    "custom": "",
    "local": "http://localhost:1234/v1",
}

# Shown as quick picks in the Models screen.
SUGGESTED_MODELS = {
    "anthropic": {"main": "claude-opus-5-5", "utility": "claude-haiku-4-5"},
}

# Current Claude models think before answering and thinking counts against max_tokens, so a small
# cap can leave no room for the reply. Reply length is steered by the prompt instead.
ANTHROPIC_MIN_MAX_TOKENS = 16000

# Models whose safety classifiers can decline a request. For these, let the API re-run a declined
# request on another model instead of handing the refusal to the player.
ANTHROPIC_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")

# Prompt caching. The prompt builder marks where the unchanging part of a prompt ends; here that
# becomes whatever the provider needs:
#   - Anthropic and OpenRouter take a cache_control marker on a content block. OpenRouter passes it
#     to Claude and Gemini and ignores or translates it for providers that cache by themselves.
#   - Nano-GPT takes the same marker for Claude models; its other models cache by themselves.
#   - Custom and local endpoints get plain text. OpenAI-style services and local servers reuse a
#     matching prompt start without being asked, and some reject fields they do not know.
# The hour-long lifetime costs a little more per write, but a player who stops to think for more
# than five minutes would otherwise pay full price for the whole story so far.
CACHE_LONG = {"type": "ephemeral", "ttl": "1h"}
CACHE_SHORT = {"type": "ephemeral"}


def _cache_marker(connection, model):
    provider = connection["provider"]
    if provider in ("anthropic", "openrouter"):
        return CACHE_LONG
    if provider == "nanogpt" and "claude" in model.lower():
        return CACHE_SHORT
    return None


def _marked(text, marker):
    return [{"type": "text", "text": text, "cache_control": marker}]


def _render(messages, marker):
    """Messages in wire form: the "cache" flag is dropped, and turned into a marker where the provider takes one."""
    return [{"role": m["role"], "content": _marked(m["content"], marker) if marker and m.get("cache") else m["content"]} for m in messages]


OPENAI_SAMPLING_KEYS = ("temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens")


class LLMError(Exception):
    """Carries a message that is fit to show to the player."""


def base_url(connection):
    url = (connection.get("base_url") or "").strip() or DEFAULT_BASE_URLS.get(connection["provider"], "")
    if not url:
        raise LLMError("This provider needs a base URL. Open Menu > Settings > Models.")
    return url.rstrip("/")


def _headers(connection):
    key = (connection.get("api_key") or "").strip()
    if connection["provider"] == "anthropic":
        if not key:
            raise LLMError("No API key is set for Anthropic. Open Menu > Settings > Models.")
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": "Bearer " + key} if key else {}


def chat_request(connection, model, system, messages, sampling=None):
    """Returns {"url", "headers", "json"} for one chat call.

    messages is a list of {"role": "user" | "assistant", "content": str}, starting with a user message.
    A message may carry "cache": True to say everything up to and including it will be sent again
    unchanged next time. The system text is always treated that way.
    sampling uses the preset's sampling keys; anything missing is left to the provider's default.
    """
    if not model:
        raise LLMError("No model is set. Open Menu > Settings > Models.")
    sampling = sampling or {}
    headers = _headers(connection)
    marker = _cache_marker(connection, model)

    if connection["provider"] == "anthropic":
        # No temperature/top_p/top_k: current Claude models reject them with a 400.
        body = {
            "model": model,
            "max_tokens": max(sampling.get("max_tokens") or 0, ANTHROPIC_MIN_MAX_TOKENS),
            "messages": _render(messages, marker),
        }
        if system:
            body["system"] = _marked(system, marker)
        if model in ANTHROPIC_FALLBACK_MODELS:
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            body["fallbacks"] = "default"
        return {"url": base_url(connection) + "/messages", "headers": headers, "json": body}

    system_message = [{"role": "system", "content": _marked(system, marker) if marker else system}] if system else []
    body = {"model": model, "messages": system_message + _render(messages, marker)}
    for key in OPENAI_SAMPLING_KEYS:
        if sampling.get(key) is not None:
            body[key] = sampling[key]
    # top_k is not part of the OpenAI format; an unknown endpoint may reject it.
    if sampling.get("top_k") is not None and connection["provider"] != "custom":
        body["top_k"] = sampling["top_k"]
    return {"url": base_url(connection) + "/chat/completions", "headers": headers, "json": body}


def chat_text(provider, data):
    """The reply text from a decoded response body."""
    if not isinstance(data, dict):
        raise LLMError("The model sent a reply the game could not read.")
    if data.get("error"):
        raise LLMError("The provider reported an error: %s" % _error_text(data["error"]))

    if provider == "anthropic":
        if data.get("stop_reason") == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            raise LLMError("The model declined to continue this scene%s." % (" (%s)" % category if category else ""))
        text = "".join(b.get("text", "") for b in data.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
    else:
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("The model sent a reply the game could not read.")
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))

    if not text.strip():
        raise LLMError("The model returned an empty reply. If it is a reasoning model, raise max tokens in the preset.")
    return text.strip()


def chat_cut_short(provider, data):
    """True when the reply stopped because it reached the response length limit, not because it was finished."""
    if not isinstance(data, dict):
        return False
    if provider == "anthropic":
        return data.get("stop_reason") == "max_tokens"
    try:
        return data["choices"][0].get("finish_reason") == "length"
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def estimate_tokens(system, messages):
    """A rough size for a prompt, erring high: about one token per three and a half characters."""
    return int((len(system) + sum(len(m["content"]) for m in messages)) / 3.5)


def chat_usage(provider, data):
    """How the prompt was billed: {"input": all prompt tokens, "cached": of those, read from cache at a
    fraction of the price, "written": of those, newly stored in the cache, "output"}. Zeros where the provider says nothing."""
    usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}

    def count(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    cached, written = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
    if provider == "anthropic":
        # Anthropic's input_tokens is only the part that was neither read from nor written to the cache.
        return {"input": count("input_tokens") + cached + written, "cached": cached, "written": written, "output": count("output_tokens")}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    for key, current in (("cached_tokens", cached), ("cache_write_tokens", written)):
        value = details.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not current:
            cached, written = (int(value), written) if key == "cached_tokens" else (cached, int(value))
    return {"input": count("prompt_tokens"), "cached": cached, "written": written, "output": count("completion_tokens")}


def models_request(connection):
    # Nano-GPT only includes each model's context size when asked for the detailed list.
    detail = "?detailed=true" if connection["provider"] == "nanogpt" else ""
    return {"url": base_url(connection) + "/models" + detail, "headers": _headers(connection)}


def model_contexts(data):
    """Model id -> how many tokens that model can be sent, for the models whose listing says.

    Providers name this differently: OpenRouter and Nano-GPT say context_length, Anthropic says
    max_input_tokens, and local servers use a few names of their own. Many OpenAI-style endpoints
    do not say at all.
    """
    entries = data.get("data") if isinstance(data, dict) else data
    found = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        nested = [entry.get(k) for k in ("top_provider", "meta")]
        candidates = [entry.get(k) for k in ("context_length", "max_input_tokens", "context_window", "max_context_length", "loaded_context_length")]
        candidates += [n.get(k) for n in nested if isinstance(n, dict) for k in ("context_length", "n_ctx_train", "n_ctx")]
        for value in candidates:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 1024:
                found[entry["id"]] = int(value)
                break
    return found


def model_ids(data):
    entries = data.get("data") if isinstance(data, dict) else data
    return sorted(e["id"] for e in entries or [] if isinstance(e, dict) and isinstance(e.get("id"), str))


def _error_text(error):
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error)
    return str(error)


def describe_http_error(status, body):
    """A player-facing message for a failed call. body is the raw response text, if there was one."""
    detail = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("error"):
            detail = _error_text(parsed["error"])
    except (TypeError, ValueError):
        pass
    hint = {
        401: "The API key was not accepted.",
        403: "The API key is not allowed to use this model.",
        404: "The model or address was not found.",
        429: "The provider is rate limiting or the account is out of credit.",
    }.get(status, "The provider returned an error." if status else "Could not reach the provider.")
    return "%s%s%s" % (hint, " (HTTP %d)" % status if status else "", " " + detail if detail else "")


def extract_json(text):
    """The first JSON object in text, or None. Helper models often wrap JSON in prose or code fences."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
