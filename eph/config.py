"""Runtime settings, read from environment (and an optional .env file at repo root)."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_dotenv(text: str) -> dict[str, str]:
    """KEY=value lines. A key listed twice keeps its last *non-empty* value, so an empty template line
    (GEMINI_API_KEY=) above your real key can't blank it out. Handles `export KEY=...`, quotes and
    trailing `# comments`."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if v[:1] in ('"', "'") and v[1:].find(v[0]) >= 0:
            v = v[1:1 + v[1:].find(v[0])]
        else:
            v = v.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if v or k not in out:
            out[k] = v
    return out


def _load_dotenv() -> None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    raw = env.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # saved as "Unicode" in Notepad
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")   # Notepad may add a BOM
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    for k, v in _parse_dotenv(text).items():
        if not os.environ.get(k):            # a real environment variable wins, unless it is empty
            os.environ[k] = v

_load_dotenv()


# Builder / Bar Raiser default models per provider. Using two different models for the builder and the
# reviewer keeps the review independent, and on free tiers it also doubles the quota (limits are per model).
DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-5-5", "claude-opus-5-5"),
    "gemini": ("gemini-3.8-flash", "gemini-3.6-flash"),
    "groq": ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"),
    "openai": ("", ""),          # any OpenAI-compatible server: set EPH_MODEL (and EPH_REVIEW_MODEL)
}
OPENAI_COMPAT_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
}


def _provider(e) -> str:
    """EPH_LLM_PROVIDER, or the first provider that has a key."""
    p = (e("EPH_LLM_PROVIDER") or "").strip().lower()
    if p:
        return p
    for name, key in (("anthropic", "ANTHROPIC_API_KEY"), ("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
                      ("openai", "OPENAI_COMPAT_BASE_URL")):
        if e(key):
            return name
    return "anthropic"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    llm_provider: str            # anthropic | gemini | groq | openai (any OpenAI-compatible server)
    llm_api_key: str | None      # key for the chosen non-Anthropic provider
    llm_base_url: str | None     # OpenAI-compatible base URL (preset for gemini / groq)
    llm_min_interval: float      # seconds between LLM calls (free tiers: stay under requests-per-minute caps)
    max_tokens_cap: int | None   # clamp max_tokens (some free tiers count max_tokens against tokens-per-minute)
    gemini_api_key: str | None
    groq_api_key: str | None
    openai_compat_base_url: str | None
    openai_compat_api_key: str | None
    model: str
    mongodb_uri: str | None
    mongodb_db: str
    databricks_host: str | None
    databricks_http_path: str | None
    databricks_token: str | None
    databricks_catalog: str
    databricks_schema: str
    linkup_api_key: str | None
    linkup_depth: str
    tavily_api_key: str | None
    arduino_cli: str
    fqbn: str
    arduino_extra_args: list[str]  # e.g. --build-property ... (EPH_ARDUINO_EXTRA_ARGS)
    max_fix_attempts: int
    review_model: str        # Bar Raiser model: a different model from the builder = more independent review
    bar_raiser: bool
    review_rounds: int       # max review → rebuild cycles per run
    autofix_severity: str    # challenges at/above this severity trigger an automatic rebuild
    voyage_api_key: str | None
    voyage_model: str
    voyage_dims: int
    precedents: bool
    precedent_k: int
    precedent_min_score: float | None  # None = per-embedder default
    mock: bool  # canned LLM + search responses (offline demo / tests)

    @property
    def use_mock_db(self) -> bool:
        return not self.mongodb_uri

    @property
    def use_databricks(self) -> bool:
        """Databricks Delta tables as the durable store (used when MONGODB_URI is not set)."""
        return bool(self.databricks_host and self.databricks_http_path and self.databricks_token) and not self.mongodb_uri


def get_settings() -> Settings:
    def e(key: str, default=None):  # an empty line in .env (KEY=) means "use the default"
        return os.getenv(key) or default
    provider = _provider(e)
    builder_default, reviewer_default = DEFAULT_MODELS.get(provider, ("", ""))
    key = {"gemini": e("GEMINI_API_KEY"), "groq": e("GROQ_API_KEY"), "openai": e("OPENAI_COMPAT_API_KEY")}.get(provider)
    return Settings(
        anthropic_api_key=e("ANTHROPIC_API_KEY"),
        llm_provider=provider,
        llm_api_key=key,
        llm_base_url=e("OPENAI_COMPAT_BASE_URL") or OPENAI_COMPAT_URLS.get(provider),
        llm_min_interval=float(e("EPH_LLM_MIN_INTERVAL", {"gemini": "4", "groq": "2.5"}.get(provider, "0"))),
        max_tokens_cap=int(e("EPH_MAX_TOKENS_CAP")) if e("EPH_MAX_TOKENS_CAP") else ({"groq": 8000}.get(provider)),
        gemini_api_key=e("GEMINI_API_KEY"),
        groq_api_key=e("GROQ_API_KEY"),
        openai_compat_base_url=e("OPENAI_COMPAT_BASE_URL"),
        openai_compat_api_key=e("OPENAI_COMPAT_API_KEY"),
        model=e("EPH_MODEL") or builder_default,
        mongodb_uri=e("MONGODB_URI"),
        mongodb_db=e("MONGODB_DB", "engineering_project_helper"),
        databricks_host=(e("DATABRICKS_HOST") or "").replace("https://", "").replace("http://", "").strip("/") or None,
        databricks_http_path=e("DATABRICKS_HTTP_PATH"),
        databricks_token=e("DATABRICKS_TOKEN"),
        databricks_catalog=e("DATABRICKS_CATALOG", "workspace"),
        databricks_schema=e("DATABRICKS_SCHEMA", "eph"),
        linkup_api_key=e("LINKUP_API_KEY"),
        linkup_depth=e("LINKUP_DEPTH", "standard"),
        tavily_api_key=e("TAVILY_API_KEY"),
        arduino_cli=e("ARDUINO_CLI", "arduino-cli"),
        fqbn=e("EPH_FQBN", "esp32:esp32:esp32"),
        arduino_extra_args=shlex.split(e("EPH_ARDUINO_EXTRA_ARGS", "")),
        max_fix_attempts=int(e("EPH_MAX_FIX_ATTEMPTS", "3")),
        review_model=e("EPH_REVIEW_MODEL") or reviewer_default or e("EPH_MODEL") or builder_default,
        bar_raiser=e("EPH_BAR_RAISER", "1") == "1",
        review_rounds=int(e("EPH_REVIEW_ROUNDS", "2")),
        autofix_severity=e("EPH_AUTOFIX_SEVERITY", "high"),
        voyage_api_key=e("VOYAGE_API_KEY"),
        voyage_model=e("VOYAGE_MODEL", "voyage-4-lite"),
        voyage_dims=int(e("VOYAGE_DIMS", "1024")),
        precedents=e("EPH_PRECEDENTS", "1") == "1",
        precedent_k=int(e("EPH_PRECEDENT_K", "3")),
        precedent_min_score=float(e("EPH_PRECEDENT_MIN_SCORE")) if e("EPH_PRECEDENT_MIN_SCORE") else None,
        # Offline by default: no API calls (and no spend) unless you explicitly set EPH_MOCK=0.
        mock=e("EPH_MOCK", "1") == "1",
    )
