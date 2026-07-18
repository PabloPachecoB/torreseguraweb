"""Adaptador Qwen sobre la API compatible con OpenAI."""

import json
from typing import Any, Dict, List, Mapping, Optional

import requests

from .config import LLMSettings


class QwenAdapter:
    """Contrato comun para Qwen local y Qwen Cloud.

    El grafo consume este adaptador y no conoce la URL ni las credenciales del
    proveedor. Los resultados excluyen prompts, tokens y razonamiento interno.
    """

    def __init__(self, settings: Optional[LLMSettings] = None):
        self.settings = settings or LLMSettings.from_env()

    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def model(self) -> str:
        return self.settings.model

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def health_check(self) -> Dict[str, Any]:
        """Comprueba conectividad y consulta los modelos del proveedor."""
        try:
            response = requests.get(
                f"{self.settings.base_url}/models",
                headers=self._headers(),
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            model_ids = [
                item.get("id")
                for item in payload.get("data", [])
                if isinstance(item, Mapping) and item.get("id")
            ]
            return {
                "healthy": True,
                "provider": self.provider,
                "model": self.model,
                "model_available": self.model in model_ids,
                "status": "ok",
            }
        except requests.Timeout:
            return self._error("provider_timeout")
        except (requests.RequestException, ValueError, TypeError):
            return self._error("provider_unavailable")

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Envia una conversacion y devuelve una respuesta normalizada."""
        request_payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        # Qwen3 razona por defecto. En el agente se desactiva para no exponer ni
        # almacenar contenido de razonamiento y reducir latencia local.
        if self.provider == "qwen_local":
            request_payload["reasoning_effort"] = "none"
        if tools:
            request_payload["tools"] = tools
            request_payload["tool_choice"] = "auto"
        if response_format:
            request_payload["response_format"] = response_format

        try:
            response = requests.post(
                f"{self.settings.base_url}/chat/completions",
                json=request_payload,
                headers=self._headers(),
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Contenido de respuesta invalido")
            return {
                "healthy": True,
                "provider": self.provider,
                "model": self.model,
                "response": content,
                "tool_calls": choice["message"].get("tool_calls", []),
                "finish_reason": choice.get("finish_reason"),
                "usage": payload.get("usage", {}),
                "status": "ok",
            }
        except requests.Timeout:
            return self._error("provider_timeout", response="")
        except requests.RequestException:
            return self._error("provider_unavailable", response="")
        except (KeyError, IndexError, TypeError, ValueError):
            return self._error("invalid_provider_response", response="")

    def generate(self, prompt: str) -> Dict[str, Any]:
        """Atajo compatible con el Incremento 1 para un unico turno."""
        return self.chat([{"role": "user", "content": prompt}])

    def chat_json(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Solicita JSON para una conversacion y valida que sea un objeto."""
        result = self.chat(
            messages,
            response_format={"type": "json_object"},
        )
        if not result.get("healthy"):
            return result
        try:
            structured = json.loads(result["response"])
            if not isinstance(structured, dict):
                raise ValueError("JSON no es un objeto")
        except (json.JSONDecodeError, TypeError, ValueError):
            return self._error("invalid_structured_response", response="")
        return {**result, "structured_response": structured}

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        """Atajo estructurado compatible con el Incremento 1."""
        return self.chat_json([{"role": "user", "content": prompt}])

    def _error(self, error_code: str, **extra: Any) -> Dict[str, Any]:
        result = {
            "healthy": False,
            "provider": self.provider,
            "model": self.model,
            "status": "error",
            "error_code": error_code,
        }
        result.update(extra)
        return result


class OllamaLocalAdapter(QwenAdapter):
    """Alias de compatibilidad para integraciones locales existentes."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        api_key: Optional[str] = None,
    ):
        configured = LLMSettings.from_env()
        defaults = (
            configured
            if configured.provider == "qwen_local"
            else LLMSettings.from_env({})
        )
        settings = LLMSettings(
            provider="qwen_local",
            model=model or defaults.model,
            base_url=(base_url or defaults.base_url).rstrip("/"),
            api_key=defaults.api_key if api_key is None else api_key,
            timeout_seconds=(
                defaults.timeout_seconds if timeout is None else timeout
            ),
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
        )
        super().__init__(settings=settings)


def get_llm_adapter(settings: Optional[LLMSettings] = None) -> QwenAdapter:
    """Construye el adaptador configurado sin acoplar consumidores a Ollama."""
    return QwenAdapter(settings=settings)
