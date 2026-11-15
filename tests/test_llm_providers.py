"""OpenAI-compatible provider (Gemini / Groq / Databricks / OpenRouter) against a fake HTTP server."""
import json

import httpx
import pytest

from eph.llm import LLMError, OpenAICompatLLM, sanitize_schema

SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "library": {"type": ["string", "null"]},
    "n": {"type": "integer", "minimum": 1},
    "items": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"x": {"type": "number", "maximum": 3}}}},
}, "required": ["name", "n"]}


def make(handler, **kw):
    return OpenAICompatLLM("gemini", "https://fake/v1", "k", "gemini-test", transport=httpx.MockTransport(handler), **kw)


def tool_reply(args):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "tool_calls": [{"type": "function", "function": {"name": "t", "arguments": json.dumps(args)}}]}}]})


def call(llm):
    return llm.call_tool(system="s", prompt="p", tool_name="t", tool_description="d", schema=SCHEMA)


def test_sanitize_schema_removes_unions_and_bounds():
    s = sanitize_schema(SCHEMA)
    assert s["properties"]["library"]["type"] == "string" and "empty value" in s["properties"]["library"]["description"]
    assert "minimum" not in s["properties"]["n"] and "minItems" not in s["properties"]["items"]
    assert "maximum" not in s["properties"]["items"]["items"]["properties"]["x"]
    assert s["required"] == ["name", "n"]


def test_named_tool_choice_happy_path():
    seen = []

    def h(req):
        body = json.loads(req.content)
        seen.append(body["tool_choice"])
        assert body["tools"][0]["function"]["parameters"]["properties"]["library"]["type"] == "string"
        return tool_reply({"name": "a", "n": 2})
    assert call(make(h)) == {"name": "a", "n": 2}
    assert seen == [{"type": "function", "function": {"name": "t"}}]


def test_falls_back_when_provider_rejects_tool_choice():
    seen = []

    def h(req):
        tc = json.loads(req.content)["tool_choice"]
        seen.append(tc)
        if tc != "auto":
            return httpx.Response(400, json={"error": {"message": "Expected a string 'tool_choice' to be one of 'none' or 'auto'"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '```json\n{"name": "b", "n": 1}\n```'}}]})
    llm = make(h)
    assert call(llm) == {"name": "b", "n": 1}
    assert seen[-1] == "auto" and llm.strategy == 2
    seen.clear()
    call(llm)                                        # remembered: goes straight to the working style
    assert seen == ["auto"]


def test_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("eph.llm.time.sleep", lambda s: None)
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429, json={"error": {"message": "quota", "details": [{"retryDelay": "7s"}]}})
        return tool_reply({"name": "c", "n": 3})
    assert call(make(h))["name"] == "c" and n["i"] == 3


def test_missing_required_field_gets_one_retry():
    prompts = []

    def h(req):
        body = json.loads(req.content)
        prompts.append(body["messages"][1]["content"])
        return tool_reply({"name": "d"} if len(prompts) == 1 else {"name": "d", "n": 5})
    assert call(make(h))["n"] == 5 and "missing required fields ['n']" in prompts[1]


def test_clear_error_on_auth_failure():
    with pytest.raises(LLMError, match="HTTP 401"):
        call(make(lambda req: httpx.Response(401, json={"error": "bad key"})))


def test_max_tokens_cap():
    seen = []

    def h(req):
        seen.append(json.loads(req.content)["max_tokens"])
        return tool_reply({"name": "e", "n": 1})
    make(h, max_tokens_cap=8000).call_tool(system="s", prompt="p", tool_name="t", tool_description="d",
                                           schema=SCHEMA, max_tokens=16000)
    assert seen == [8000]


def test_provider_selection_from_env(monkeypatch):
    from eph.config import get_settings
    from eph.llm import make_llm
    for k in ("ANTHROPIC_API_KEY", "EPH_LLM_PROVIDER", "EPH_MODEL", "EPH_REVIEW_MODEL", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EPH_MOCK", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    s = get_settings()
    assert s.llm_provider == "gemini" and s.model != s.review_model
    b, r = make_llm(s), make_llm(s, s.review_model)
    assert b.name == "gemini" and "generativelanguage.googleapis.com" in b.base_url and r.model == s.review_model
