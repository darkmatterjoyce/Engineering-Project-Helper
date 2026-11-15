"""The model menu: every LLM the backend can use, grouped by provider, with availability.

The user picks one model for the Engineer (builder) and one for the Bar Raiser before each run. A model is
*available* when its provider's API key is in .env; unavailable ones are shown greyed out ("no key").
Choosing a different AI company for the Bar Raiser gives the most independent review."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Settings
from .llm import LLM, make_llm_for

OFFLINE = "offline"


@dataclass(frozen=True)
class ModelSpec:
    id: str          # "<provider>:<model>" (or "offline")
    label: str
    provider: str    # mock | anthropic | gemini | groq | openai
    model: str
    group: str       # heading in the dropdown
    note: str = ""   # e.g. "free tier"


CATALOGUE: list[ModelSpec] = [
    ModelSpec(OFFLINE, "Offline demo (canned answers)", "mock", "mock-llm", "Offline", "no API calls, no cost"),
    ModelSpec("anthropic:claude-opus-5-5", "Claude Opus 5.5", "anthropic", "claude-opus-5-5", "Anthropic", "most capable"),
    ModelSpec("anthropic:claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", "claude-sonnet-5-5", "Anthropic", "fast, strong"),
    ModelSpec("anthropic:claude-haiku-5-5", "Claude Haiku 5.5", "anthropic", "claude-haiku-5-5", "Anthropic", "fastest, cheapest"),
    ModelSpec("gemini:gemini-3.8-flash", "Gemini 3.8 Flash", "gemini", "gemini-3.8-flash", "Google Gemini", "free tier"),
    ModelSpec("gemini:gemini-3.6-flash", "Gemini 3.6 Flash", "gemini", "gemini-3.6-flash", "Google Gemini", "free tier"),
    ModelSpec("gemini:gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "gemini", "gemini-3.5-flash-lite", "Google Gemini", "free tier"),
    ModelSpec("groq:openai/gpt-oss-120b", "GPT-OSS 120B", "groq", "openai/gpt-oss-120b", "Groq (open models)", "free tier"),
    ModelSpec("groq:llama-3.3-70b-versatile", "Llama 3.3 70B", "groq", "llama-3.3-70b-versatile", "Groq (open models)", "free tier"),
]
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
           "openai": "OPENAI_COMPAT_API_KEY"}


def _available(spec: ModelSpec, s: Settings) -> bool:
    return {"mock": True, "anthropic": bool(s.anthropic_api_key), "gemini": bool(s.gemini_api_key),
            "groq": bool(s.groq_api_key),
            "openai": bool(s.openai_compat_api_key and s.openai_compat_base_url)}[spec.provider]


def catalogue(s: Settings) -> list[ModelSpec]:
    """Built-in list plus anything named in .env (EPH_MODEL / EPH_REVIEW_MODEL) that isn't in it,
    so a new model name from your provider's console can be used without code changes."""
    out = list(CATALOGUE)
    known = {m.id for m in out}
    for name in {s.model, s.review_model}:
        if name and s.llm_provider in KEY_ENV:
            mid = f"{s.llm_provider}:{name}"
            if mid not in known:
                group = {"anthropic": "Anthropic", "gemini": "Google Gemini", "groq": "Groq (open models)",
                         "openai": "Custom (OpenAI-compatible)"}[s.llm_provider]
                out.append(ModelSpec(mid, name, s.llm_provider, name, group, "from .env"))
                known.add(mid)
    return out


def get_spec(s: Settings, model_id: str) -> ModelSpec:
    for m in catalogue(s):
        if m.id == model_id:
            return m
    raise ValueError(f"unknown model '{model_id}'")


def listing(s: Settings) -> list[dict]:
    rows = []
    for m in catalogue(s):
        ok = _available(m, s)
        rows.append({**asdict(m), "available": ok,
                     "reason": "" if ok else f"no key: set {KEY_ENV.get(m.provider, '')} in .env"})
    return rows


def defaults(s: Settings) -> dict[str, str]:
    """Engineer: the model configured in .env if usable, else the first usable one. Bar Raiser: prefer a
    *different AI company* (most independent review), else a different model from the same company."""
    if s.mock:
        return {"engineer": OFFLINE, "bar_raiser": OFFLINE}
    usable = [m for m in catalogue(s) if m.provider != "mock" and _available(m, s)]
    if not usable:
        return {"engineer": OFFLINE, "bar_raiser": OFFLINE}
    preferred = f"{s.llm_provider}:{s.model}"
    eng = next((m for m in usable if m.id == preferred), usable[0])
    other_ai = [m for m in usable if m.provider != eng.provider]
    review_pref = f"{s.llm_provider}:{s.review_model}"
    br = (other_ai[0] if other_ai else
          next((m for m in usable if m.id == review_pref and m.id != eng.id), None) or
          next((m for m in usable if m.id != eng.id), eng))
    return {"engineer": eng.id, "bar_raiser": br.id}


def validate(s: Settings, model_id: str) -> ModelSpec:
    spec = get_spec(s, model_id)
    if not _available(spec, s):
        raise ValueError(f"{spec.label} can't be used: set {KEY_ENV.get(spec.provider)} in .env")
    return spec


def build(s: Settings, model_id: str) -> LLM:
    spec = validate(s, model_id)
    return make_llm_for(s, spec.provider, spec.model if spec.provider != "mock" else "offline-demo")
