"""Model backends behind one tiny interface: messages in, text out. No tools, no streaming."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

import httpx

from bot.config import API_KEY_ENV, Config


class Backend(Protocol):
    name: str

    async def complete(self, messages: list[dict[str, str]]) -> str: ...

    async def aclose(self) -> None: ...


class OllamaBackend:
    name = "ollama"

    def __init__(
        self,
        host: str,
        model: str,
        temperature: float,
        max_tokens: int,
        think: str = "off",
        keep_alive: str = "30m",
    ):
        from ollama import AsyncClient  # imported here so the OpenAI path needs no ollama server

        self._client = AsyncClient(host=host)
        self.model = model
        self._options = {"temperature": temperature, "num_predict": max_tokens}
        self._think = {"off": False, "on": True}.get(think)  # "omit" -> None
        self._keep_alive = keep_alive

    async def complete(self, messages: list[dict[str, str]]) -> str:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "options": self._options,
            "keep_alive": self._keep_alive,
        }
        if self._think is not None:
            kwargs["think"] = self._think
        response = await self._client.chat(**kwargs)
        return response.message.content or ""

    async def aclose(self) -> None:
        client = getattr(self._client, "_client", None)
        if client is not None:
            await client.aclose()


class OpenAICompatBackend:
    name = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        http_client: httpx.AsyncClient | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=httpx.Timeout(120.0)
        )
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"] or ""

    async def aclose(self) -> None:
        await self._client.aclose()


def make_backend(cfg: Config, env: Mapping[str, str] | None = None) -> Backend:
    """Select and construct the backend named in config."""
    env = os.environ if env is None else env
    if cfg.backend == "ollama":
        return OllamaBackend(
            host=cfg.ollama_host,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            think=cfg.ollama_think,
            keep_alive=cfg.ollama_keep_alive,
        )
    if cfg.backend == "openai":
        return OpenAICompatBackend(
            base_url=cfg.openai_base_url,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            api_key=env.get(API_KEY_ENV) or None,
        )
    raise ValueError(f"unknown backend: {cfg.backend}")
