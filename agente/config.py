"""Configuracion validada del proveedor LLM del agente."""

from dataclasses import dataclass
import os
from typing import Mapping, Optional


SUPPORTED_PROVIDERS = {"qwen_local", "qwen_cloud"}


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    temperature: float
    max_tokens: int

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None):
        values = environ if environ is not None else os.environ
        provider = values.get("LLM_PROVIDER", "qwen_local").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                "LLM_PROVIDER debe ser 'qwen_local' o 'qwen_cloud'."
            )

        default_base_url = (
            "http://127.0.0.1:11434/v1" if provider == "qwen_local" else ""
        )
        base_url = values.get("QWEN_BASE_URL", default_base_url).strip().rstrip("/")
        if not base_url:
            raise ValueError("QWEN_BASE_URL es obligatorio para qwen_cloud.")

        model = values.get("QWEN_MODEL", "qwen3:8b").strip()
        if not model:
            raise ValueError("QWEN_MODEL no puede estar vacio.")

        try:
            timeout_seconds = float(values.get("QWEN_TIMEOUT_SECONDS", "30"))
            temperature = float(values.get("QWEN_TEMPERATURE", "0"))
            max_tokens = int(values.get("QWEN_MAX_TOKENS", "512"))
        except (TypeError, ValueError) as exc:
            raise ValueError("La configuracion numerica de Qwen no es valida.") from exc

        if timeout_seconds <= 0:
            raise ValueError("QWEN_TIMEOUT_SECONDS debe ser mayor que cero.")
        if not 0 <= temperature <= 2:
            raise ValueError("QWEN_TEMPERATURE debe estar entre 0 y 2.")
        if max_tokens <= 0:
            raise ValueError("QWEN_MAX_TOKENS debe ser mayor que cero.")

        api_key = values.get("QWEN_API_KEY", "").strip()
        if provider == "qwen_cloud" and not api_key:
            raise ValueError("QWEN_API_KEY es obligatorio para qwen_cloud.")

        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            max_tokens=max_tokens,
        )
