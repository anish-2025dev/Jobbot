"""Provider-agnostic LLM client.

LLM_PROVIDER:
  none       no LLM; every caller has a deterministic fallback (default)
  anthropic  Anthropic Messages API
  openai     any OpenAI-compatible /chat/completions endpoint: OpenAI, Groq,
             Gemini's OpenAI endpoint, OpenRouter, or local Ollama
             (LLM_BASE_URL=http://ollama:11434/v1) for the privacy option

Two tiers (section 7/12): "cheap" for extraction/scoring/classification,
"strong" for resume writing and outreach emails.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import httpx

from app.config import get_settings

Tier = Literal["cheap", "strong"]


class LLMUnavailable(Exception):
    pass


class LLMError(Exception):
    pass


def extract_json(text: str) -> dict | list | None:
    """First JSON object/array in a model reply (tolerates prose and ``` fences)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for cand in candidates:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            while start != -1:
                depth, in_str, esc = 0, False, False
                for i in range(start, len(cand)):
                    ch = cand[i]
                    if in_str:
                        esc = (ch == "\\") and not esc
                        if ch == '"' and not esc:
                            in_str = False
                        elif ch != "\\":
                            esc = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == opener:
                        depth += 1
                    elif ch == closer:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(cand[start:i + 1])
                            except json.JSONDecodeError:
                                break
                start = cand.find(opener, start + 1)
    return None


@dataclass
class LLM:
    provider: str
    api_key: str
    base_url: str
    cheap_model: str
    strong_model: str
    transport: httpx.BaseTransport | None = None
    max_retries: int = 3

    @property
    def available(self) -> bool:
        return self.provider in ("anthropic", "openai") and bool(self.cheap_model)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=90, transport=self.transport)

    def complete(self, prompt: str, *, system: str = "", tier: Tier = "cheap",
                 max_tokens: int = 1200, json_mode: bool = False) -> str:
        if not self.available:
            raise LLMUnavailable("LLM_PROVIDER is none")
        model = self.strong_model if tier == "strong" else self.cheap_model
        if self.provider == "anthropic":
            url = f"{self.base_url.rstrip('/')}/v1/messages"
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
        else:
            url = f"{self.base_url.rstrip('/')}/chat/completions"
            headers = {"content-type": "application/json"}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            msgs = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
            body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": 0.2}
            if json_mode:
                body["response_format"] = {"type": "json_object"}

        last: Exception | None = None
        with self._client() as c:
            for attempt in range(self.max_retries):
                try:
                    r = c.post(url, headers=headers, json=body)
                except httpx.HTTPError as e:
                    last = e
                else:
                    if r.status_code == 200:
                        data = r.json()
                        if self.provider == "anthropic":
                            return "".join(b.get("text", "") for b in data.get("content", [])
                                           if b.get("type") == "text")
                        return data["choices"][0]["message"]["content"] or ""
                    if r.status_code not in (408, 429, 500, 502, 503, 504, 529):
                        raise LLMError(f"{self.provider} {r.status_code}: {r.text[:300]}")
                    last = LLMError(f"{self.provider} {r.status_code}")
                time.sleep(min(2 ** attempt, 8) if self.transport is None else 0)
        raise LLMError(f"LLM failed after {self.max_retries} attempts: {last}")

    def complete_json(self, prompt: str, *, system: str = "", tier: Tier = "cheap",
                      max_tokens: int = 1500) -> dict | list:
        """Reply parsed as JSON; one corrective retry, then LLMError."""
        sys = (system + "\n" if system else "") + "Reply with a single JSON value and nothing else."
        text = self.complete(prompt, system=sys, tier=tier, max_tokens=max_tokens, json_mode=True)
        parsed = extract_json(text)
        if parsed is None:
            text = self.complete(prompt + "\n\nYour previous reply was not valid JSON. "
                                 "Return only the JSON.", system=sys, tier=tier,
                                 max_tokens=max_tokens, json_mode=True)
            parsed = extract_json(text)
        if parsed is None:
            raise LLMError("model did not return JSON")
        return parsed


_DEFAULTS = {
    "anthropic": ("https://api.anthropic.com", "claude-haiku-5-5", "claude-sonnet-5-5"),
    "openai": ("https://api.openai.com/v1", "", ""),
    "none": ("", "", ""),
}


@lru_cache
def get_llm() -> LLM:
    s = get_settings()
    provider = (s.llm_provider or "none").lower()
    base, cheap, strong = _DEFAULTS.get(provider, _DEFAULTS["none"])
    cheap = s.llm_cheap_model or cheap
    return LLM(provider=provider, api_key=s.llm_api_key, base_url=s.llm_base_url or base,
               cheap_model=cheap, strong_model=s.llm_strong_model or strong or cheap)
