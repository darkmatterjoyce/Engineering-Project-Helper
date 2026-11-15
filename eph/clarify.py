"""Step 1: at most two clarifying questions, only when something essential is missing."""
from __future__ import annotations

SYSTEM = """You help start an ESP32 hardware project from a one-sentence idea.
Ask AT MOST 2 short questions, and only for information that is essential and cannot be reasonably
assumed (e.g. budget, USB/mains vs battery, indoor vs outdoor, where it runs). If the idea is clear
enough, ask nothing. Never ask about things you can decide yourself (which sensor, which pins)."""

SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "maxItems": 2, "items": {"type": "string"}}},
    "required": ["questions"],
}

IMPROVE_SYSTEM = """A user wants to improve an ESP32 project that was already delivered to them.
Ask AT MOST 2 short questions, only if their feedback is too vague to act on. Otherwise ask nothing."""


def questions(llm, prompt: str) -> list[str]:
    out = llm.call_tool(system=SYSTEM, prompt=f'Project idea: "{prompt}"', tool_name="clarifying_questions",
                        tool_description="Ask the user 0-2 essential questions.", schema=SCHEMA, max_tokens=1500)
    return [q for q in out.get("questions", []) if q.strip()][:2]


def improvement_questions(llm, prompt: str, feedback: str) -> list[str]:
    out = llm.call_tool(system=IMPROVE_SYSTEM, prompt=f'Project: "{prompt}"\nFeedback: "{feedback}"',
                        tool_name="clarifying_questions", tool_description="Ask 0-2 questions.",
                        schema=SCHEMA, max_tokens=1500)
    return [q for q in out.get("questions", []) if q.strip()][:2]
