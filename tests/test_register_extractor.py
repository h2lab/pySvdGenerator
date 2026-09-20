# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Ollama integration used by register extraction."""

from typing import Any

from pySvdGenerator.register_extractor import OllamaAssistant


class RecordingOllamaClient:
    """Minimal Ollama client recording the model submitted to chat."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.chat_model: str | None = None

    def list(self) -> dict[str, list[dict[str, str]]]:
        """Expose the requested model as available on the server."""
        return {"models": [{"model": self.model}]}

    def chat(self, **kwargs: Any) -> dict[str, dict[str, str]]:
        """Record the selected model and return a valid JSON response."""
        self.chat_model = kwargs["model"]
        return {"message": {"content": '{"kind": "unknown"}'}}


def test_ollama_request_uses_requested_model() -> None:
    """The selected LLM model is forwarded to every Ollama chat request."""
    client = RecordingOllamaClient("llama3.2")
    assistant = OllamaAssistant(model="llama3.2", client=client)

    assistant._ask("test")

    assert client.chat_model == "llama3.2"
