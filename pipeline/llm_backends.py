"""LLM backends for the enrichment task. All of them take the same payload and return an Enrichment.

  none   - disabled
  claude - Anthropic API (structured output via messages.parse); also the optional teacher
  local  - the fine-tuned student (or any stock model) served by llama-server (OpenAI-compatible,
           port 8081) or Ollama (native /api/chat with a JSON schema `format`, port 11434)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import ROOT, settings
from app.models import Enrichment, enrichment_json_schema
from app.taxonomy import topic_ids

log = logging.getLogger("pipeline")

PROMPT_FILE = ROOT / "pipeline" / "prompts" / "enrich_v1.md"

# USD per million tokens (input, output); prefix-matched on the model id
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class BackendError(Exception):
    """The backend could not produce an answer (network, refusal, malformed output)."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""


def load_system_prompt() -> str:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    return text.replace("{topics}", ", ".join(topic_ids()))


def build_payload(channel_title: str | None, title: str | None, segments_de: list[str]) -> dict[str, Any]:
    return {"channel": channel_title or "", "title": title or "",
            "segments": [{"i": i, "de": t} for i, t in enumerate(segments_de)]}


def user_message(payload: dict[str, Any], error_hint: str | None = None) -> str:
    msg = json.dumps(payload, ensure_ascii=False)
    if error_hint:
        msg += f"\n\nYour previous answer was rejected: {error_hint}. Return a corrected JSON object."
    return msg


def parse_enrichment_text(text: str) -> Enrichment:
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    try:
        return Enrichment.model_validate_json(cleaned)
    except Exception as e:  # pydantic ValidationError or JSON errors
        raise BackendError(f"malformed JSON output: {str(e)[:200]}") from e


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (pin, pout) in PRICES.items():
        if model.startswith(prefix):
            return (input_tokens * pin + output_tokens * pout) / 1_000_000
    return 0.0


# ---------------------------------------------------------------------------
class NoneBackend:
    name = "none"

    def is_ready(self) -> bool:
        return False

    def enrich(self, payload: dict[str, Any], system_prompt: str, error_hint: str | None = None) -> tuple[Enrichment, Usage]:
        raise BackendError("LLM backend disabled (LLM_BACKEND=none)")


class ClaudeBackend:
    name = "claude"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.llm_model
        self._api_key = api_key if api_key is not None else (settings.anthropic_api_key or None)
        self._client = None

    def is_ready(self) -> bool:
        import os
        return bool(self._api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def enrich(self, payload: dict[str, Any], system_prompt: str, error_hint: str | None = None) -> tuple[Enrichment, Usage]:
        t0 = time.perf_counter()
        try:
            response = self._get_client().messages.parse(
                model=self.model,
                max_tokens=8000,
                output_config={"effort": "low"},
                system=system_prompt,
                messages=[{"role": "user", "content": user_message(payload, error_hint)}],
                output_format=Enrichment,
            )
        except Exception as e:
            raise BackendError(f"claude: {type(e).__name__}: {str(e)[:200]}") from e
        if getattr(response, "stop_reason", None) == "refusal":
            raise BackendError("claude: refusal")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise BackendError("claude: no parsed output")
        usage = getattr(response, "usage", None)
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        return parsed, Usage(it, ot, price(self.model, it, ot), int((time.perf_counter() - t0) * 1000), self.model)


class LocalBackend:
    """llama-server (OpenAI-compatible) or Ollama (native chat with schema)."""

    name = "local"

    def __init__(self, url: str | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 300.0) -> None:
        self.url = (url or settings.local_llm_url).rstrip("/")
        self.model = model or settings.local_llm_model
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._kind: str | None = None  # "ollama" | "openai"

    def kind(self) -> str:
        if self._kind is None:
            try:
                r = self._client.get(f"{self.url}/api/tags", timeout=3.0)
                self._kind = "ollama" if r.status_code == 200 else "openai"
            except httpx.HTTPError:
                self._kind = "openai"
        return self._kind

    def is_ready(self) -> bool:
        try:
            if self.kind() == "ollama":
                return self._client.get(f"{self.url}/api/tags", timeout=3.0).status_code == 200
            return self._client.get(f"{self.url}/v1/models", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def enrich(self, payload: dict[str, Any], system_prompt: str, error_hint: str | None = None) -> tuple[Enrichment, Usage]:
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message(payload, error_hint)}]
        schema = enrichment_json_schema()
        t0 = time.perf_counter()
        try:
            if self.kind() == "ollama":
                r = self._client.post(f"{self.url}/api/chat", json={
                    "model": self.model, "messages": messages, "stream": False, "format": schema, "think": False,
                    "options": {"temperature": 0.2, "num_ctx": 8192},
                })
                r.raise_for_status()
                data = r.json()
                text = (data.get("message") or {}).get("content", "")
                it, ot = int(data.get("prompt_eval_count", 0) or 0), int(data.get("eval_count", 0) or 0)
            else:
                r = self._client.post(f"{self.url}/v1/chat/completions", json={
                    "model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": 4096,
                    "chat_template_kwargs": {"enable_thinking": False},   # Qwen3 hybrid models; ignored by others
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "enrichment", "schema": schema, "strict": True}},
                })
                r.raise_for_status()
                data = r.json()
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                usage = data.get("usage") or {}
                it, ot = int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)
        except httpx.HTTPError as e:
            raise BackendError(f"local: {type(e).__name__}: {str(e)[:200]}") from e
        enr = parse_enrichment_text(text)
        return enr, Usage(it, ot, 0.0, int((time.perf_counter() - t0) * 1000), self.model)


def get_backend(name: str | None = None):
    name = (name or settings.llm_backend or "none").lower()
    if name == "claude":
        return ClaudeBackend()
    if name == "local":
        return LocalBackend()
    return NoneBackend()
