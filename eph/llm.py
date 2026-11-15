"""LLM client. Every call is a *forced tool call* so the model must return JSON that matches a
schema; no free-text parsing anywhere in the pipeline."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Protocol

import httpx

from .config import Settings


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str
    model: str

    def call_tool(
        self, *, system: str, prompt: str, tool_name: str, tool_description: str,
        schema: dict, max_tokens: int = 8000,
    ) -> dict[str, Any]: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=180)
        self.model = model

    # Models that reject a forced tool_choice ("tool"/"any"); they get tool_choice "auto" plus an instruction.
    _NO_FORCED_TOOL: set[str] = set()

    def call_tool(self, *, system, prompt, tool_name, tool_description, schema, max_tokens=8000):
        try:
            return self._call_tool(system, prompt, tool_name, tool_description, schema, max_tokens)
        except LLMError as e:
            if "truncated" not in str(e) or max_tokens >= 16000:
                raise
            # the answer didn't fit: once more with plenty of room
            return self._call_tool(system, prompt, tool_name, tool_description, schema, min(16000, max_tokens * 3))

    def _call_tool(self, system, prompt, tool_name, tool_description, schema, max_tokens):
        import anthropic

        tool = {"name": tool_name, "description": tool_description, "input_schema": schema}
        forced = self.model not in self._NO_FORCED_TOOL
        if forced:
            try:
                return self._call(system, prompt, tool, {"type": "tool", "name": tool_name}, max_tokens)
            except anthropic.BadRequestError as e:
                if "tool_choice" not in str(e):
                    raise
                self._NO_FORCED_TOOL.add(self.model)   # newer models: remember, don't pay for the failed call again
        must = (f"\n\nYou must answer by calling the `{tool_name}` tool exactly once with every required field. "
                "Do not answer in plain text.")
        try:
            return self._call(system + must, prompt, tool, {"type": "auto"}, max_tokens)
        except LLMError as first:
            if "no tool call" not in str(first):
                raise
            return self._call(system + must, prompt + must, tool, {"type": "auto"}, max_tokens)  # one firmer retry

    def _call(self, system, prompt, tool, tool_choice, max_tokens):
        name = tool["name"]
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[tool],
            tool_choice=tool_choice,
        )
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{name}: output truncated at max_tokens={max_tokens}")
        for block in resp.content:
            if block.type == "tool_use" and block.name == name:
                return dict(block.input)
        text = "".join(getattr(b, "text", "") for b in resp.content)
        data = _extract_json(text)   # some models write the JSON as text instead of calling the tool
        if isinstance(data, dict) and data:
            return data
        raise LLMError(f"{name}: model returned no tool call")


# --------------------------------------------------------------------------- OpenAI-compatible providers
_THROTTLE: dict[str, float] = {}          # base_url -> time of the last request (shared by builder + reviewer)
_THROTTLE_LOCK = threading.Lock()
DROP_KEYS = {"minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
             "default", "examples", "$schema", "additionalProperties"}


def sanitize_schema(schema: Any) -> Any:
    """Reduce a JSON schema to the subset every OpenAI-compatible provider accepts (Gemini is the strictest):
    no type unions, no numeric/length bounds. Nullable fields become plain types the code already treats
    as 'empty' (0, "", [])."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in DROP_KEYS:
            continue
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {name: sanitize_schema(sub) for name, sub in v.items()}
        elif k == "items":
            out["items"] = sanitize_schema(v)
        else:
            out[k] = v
    if isinstance(schema.get("type"), list) and "null" in schema["type"]:
        out["description"] = (schema.get("description", "") + " (use an empty value if not applicable)").strip()
    return out


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(t[i:j + 1])
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


MAX_WAIT_PER_RETRY = 75.0     # a longer "retry after" means a daily/hourly quota: fail now with a clear message
MAX_RETRY_TOTAL = 180.0       # never spend more than this waiting on retries for one request


def _requested_wait(resp: httpx.Response) -> float | None:
    """Seconds the provider asked us to wait (header or message: '7m30.5s', '12.3s', '1h2m'), if it said."""
    h = resp.headers.get("retry-after")
    if h:
        try:
            return float(h)
        except ValueError:
            pass
    m = re.search(r'try again in ((?:\d+(?:\.\d+)?h)?(?:\d+(?:\.\d+)?m(?!s))?(?:\d+(?:\.\d+)?s)?(?:\d+(?:\.\d+)?ms)?)',
                  resp.text, re.I)
    if m and m.group(1):
        total, txt = 0.0, m.group(1)
        for num, unit in re.findall(r'(\d+(?:\.\d+)?)(ms|h|m|s)', txt):
            total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
        return total
    m = re.search(r'retryDelay["\s:]*"?(\d+(?:\.\d+)?)s', resp.text, re.I)
    return float(m.group(1)) if m else None


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    w = _requested_wait(resp)
    if w is not None:
        return min(w + 1, 90.0)
    return min(5.0 * 2 ** attempt, 60.0)


class OpenAICompatLLM:
    """Gemini (free tier), Groq (free tier), Databricks, OpenRouter, OpenAI... through the Chat Completions API.

    Forced tool calling is attempted three ways, because providers differ: tool_choice={function: name},
    then "required", then "auto" with an instruction. The first that the provider accepts is remembered.
    If a model answers in text instead of a tool call, JSON in the text is accepted."""

    STRATEGIES = ("named", "required", "auto")

    def __init__(self, name: str, base_url: str, api_key: str, model: str, min_interval: float = 0.0,
                 max_tokens_cap: int | None = None, transport: httpx.BaseTransport | None = None):
        if not model:
            raise LLMError(f"{name}: set EPH_MODEL to a model name")
        self.name, self.model = name, model
        self.base_url = base_url.rstrip("/")
        self.min_interval, self.max_tokens_cap = min_interval, max_tokens_cap
        self.client = httpx.Client(timeout=httpx.Timeout(150, connect=15), transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.strategy = 0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with _THROTTLE_LOCK:
            wait = _THROTTLE.get(self.base_url, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _THROTTLE[self.base_url] = time.monotonic()

    def _post(self, body: dict) -> httpx.Response:
        waited = 0.0
        for attempt in range(7):
            self._throttle()
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TransportError as e:   # timeout / dropped connection: retry once, then say which model
                if attempt == 0:
                    continue
                raise LLMError(f"{self.name} {self.model}: no answer ({type(e).__name__}); the provider may be "
                               "overloaded. Try again, or pick another model.") from e
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 6:
                asked = _requested_wait(r) if r.status_code == 429 else None
                if asked is not None and asked > MAX_WAIT_PER_RETRY:
                    mins = asked / 60
                    raise LLMError(f"{self.name} {self.model}: rate limit / quota reached, the provider says try again "
                                   f"in {mins:.0f} min. Pick another model (or another provider) for now.")
                wait = _retry_after(r, attempt)
                if waited + wait > MAX_RETRY_TOTAL:
                    raise LLMError(f"{self.name} {self.model}: still HTTP {r.status_code} after {waited:.0f}s of "
                                   f"retries ({r.text[:160]})")
                time.sleep(wait)
                waited += wait
                continue
            return r
        return r

    def call_tool(self, *, system, prompt, tool_name, tool_description, schema, max_tokens=8000):
        if self.max_tokens_cap:
            max_tokens = min(max_tokens, self.max_tokens_cap)
        tool = {"type": "function", "function": {"name": tool_name, "description": tool_description,
                                                 "parameters": sanitize_schema(schema)}}
        required = schema.get("required", [])
        feedback = ""
        for _ in range(2):  # one retry if the answer is missing required fields / isn't valid JSON / was cut off
            out, err = self._once(system, prompt + feedback, tool, tool_name, max_tokens)
            if out is None and "truncated" in (err or ""):
                cap = self.max_tokens_cap or 16000
                if max_tokens < cap:
                    max_tokens = min(cap, max_tokens * 3)
                    continue
            if out is not None:
                missing = [k for k in required if k not in out]
                if not missing:
                    return out
                err = f"missing required fields {missing}"
            feedback = (f"\n\nYour previous answer was rejected ({err}). Call `{tool_name}` again with ALL "
                        f"required fields: {required}.")
        raise LLMError(f"{tool_name}: {err}")

    def _once(self, system, prompt, tool, tool_name, max_tokens) -> tuple[dict | None, str]:
        while True:
            strat = self.STRATEGIES[self.strategy]
            sys_msg = system
            if strat == "named":
                choice: Any = {"type": "function", "function": {"name": tool_name}}
            elif strat == "required":
                choice = "required"
            else:
                choice = "auto"
                sys_msg += f"\n\nYou MUST reply by calling the function `{tool_name}` with every required field."
            body = {"model": self.model, "max_tokens": max_tokens, "tools": [tool], "tool_choice": choice,
                    "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}]}
            r = self._post(body)
            if r.status_code == 400 and "tool" in r.text.lower() and self.strategy < len(self.STRATEGIES) - 1:
                self.strategy += 1          # provider rejected this tool_choice style: try the next one
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.name} HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            choice0 = (data.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, dict):
                    return args, ""
                try:
                    v = json.loads(args or "")
                    if isinstance(v, dict):
                        return v, ""
                except json.JSONDecodeError:
                    pass
            text_json = _extract_json(msg.get("content") or "")
            if text_json is not None:
                return text_json, ""
            if choice0.get("finish_reason") == "length":
                return None, f"output truncated at max_tokens={max_tokens}"
            return None, "no tool call and no JSON in the reply"


PROVIDER_LIMITS = {"gemini": (4.0, None), "groq": (2.5, 8000)}   # (seconds between calls, max_tokens cap)


def make_llm_for(settings: Settings, provider: str, model: str) -> LLM:
    """Build a client for one provider + model, using that provider's key from .env."""
    if provider == "mock":
        from .mock import MockLLM

        return MockLLM(model or "mock-llm")
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set in .env")
        return AnthropicLLM(settings.anthropic_api_key, model)
    keys = {"gemini": settings.gemini_api_key, "groq": settings.groq_api_key,
            "openai": settings.openai_compat_api_key}
    urls = {"gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "groq": "https://api.groq.com/openai/v1", "openai": settings.openai_compat_base_url}
    if provider not in keys:
        raise LLMError(f"unknown provider '{provider}'")
    if not keys[provider]:
        env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_COMPAT_API_KEY"}[provider]
        raise LLMError(f"{env} is not set in .env")
    if not urls[provider]:
        raise LLMError("OPENAI_COMPAT_BASE_URL is not set in .env")
    interval, cap = PROVIDER_LIMITS.get(provider, (0.0, None))
    if provider == settings.llm_provider:   # explicit .env overrides apply to the configured provider
        interval, cap = settings.llm_min_interval, settings.max_tokens_cap or cap
    return OpenAICompatLLM(provider, urls[provider], keys[provider], model, interval, cap)


def make_llm(settings: Settings, model: str | None = None) -> LLM:
    """Client for the provider configured in .env (used by scripts/check_setup.py)."""
    if settings.mock:
        from .mock import MockLLM

        return MockLLM(f"mock:{model}" if model else "mock-llm")
    p = settings.llm_provider
    if p == "anthropic" and not settings.anthropic_api_key:
        raise LLMError("No LLM key set. Put ANTHROPIC_API_KEY, GEMINI_API_KEY or GROQ_API_KEY in .env "
                       "(or set EPH_MOCK=1 for the offline demo)")
    if p not in ("anthropic", "gemini", "groq", "openai"):
        raise LLMError(f"unknown EPH_LLM_PROVIDER '{p}' (use anthropic, gemini, groq or openai)")
    if p == "openai" and not settings.openai_compat_base_url:
        raise LLMError("set OPENAI_COMPAT_BASE_URL for EPH_LLM_PROVIDER=openai")
    return make_llm_for(settings, p, model or settings.model)
