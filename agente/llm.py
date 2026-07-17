import os
from typing import Any, Dict, Optional

import requests


class QwenLocalAdapter:
    """Adaptador mínimo para un endpoint OpenAI-compatible de Qwen local.

    El objetivo de esta fase es validar la estrategia de integración local y
    ofrecer un health check simple antes de introducir el grafo completo.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: Optional[int] = None):
        self.base_url = (base_url or os.getenv("QWEN_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
        self.api_key = api_key or os.getenv("QWEN_API_KEY") or ""
        self.timeout = timeout or int(os.getenv("QWEN_TIMEOUT_SECONDS", "5"))

    def health_check(self) -> Dict[str, Any]:
        """Consulta un endpoint de salud del modelo local sin depender del grafo."""
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.get(f"{self.base_url}/health", headers=headers, timeout=self.timeout)
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            status_value = payload.get("status") or payload.get("message") or str(response.status_code)
            healthy = response.status_code == 200
            return {
                "healthy": healthy,
                "provider": "qwen_local",
                "status": status_value,
                "base_url": self.base_url,
            }
        except requests.RequestException as exc:
            return {
                "healthy": False,
                "provider": "qwen_local",
                "status": f"error:{exc.__class__.__name__}",
                "base_url": self.base_url,
            }

    def generate(self, prompt: str) -> Dict[str, Any]:
        """Método placeholder para la siguiente iteración del agente."""
        return {
            "provider": "qwen_local",
            "prompt": prompt,
            "response": "",
            "healthy": self.health_check()["healthy"],
        }
