"""Trazas LangSmith opcionales y sin contenido conversacional."""

from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4


SAFE_METADATA_KEYS = {
    "environment",
    "model_provider",
    "model_name",
    "graph_version",
    "intent",
    "tool_name",
    "action_type",
    "outcome",
    "llm_invoked",
    "guardrail_triggered",
}
SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "password",
    "token",
    "jwt",
    "secret",
    "photo",
    "image",
    "document",
    "file",
}
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def sanitize_trace_data(value: Any):
    """Redacta secretos conocidos sin mutar el objeto original."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(sensitive in normalized_key for sensitive in SENSITIVE_KEYS):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_trace_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_data(item) for item in value]
    if isinstance(value, str):
        redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
        redacted = BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
        return JWT_PATTERN.sub("[REDACTED_JWT]", redacted)
    return value


@dataclass(frozen=True)
class ObservabilitySettings:
    tracing: bool
    api_key: str
    endpoint: str
    project: str
    environment: str

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None):
        values = environ if environ is not None else os.environ
        tracing = values.get("LANGSMITH_TRACING", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        api_key = values.get("LANGSMITH_API_KEY", "").strip()
        if tracing and not api_key:
            raise ValueError("LANGSMITH_API_KEY es obligatorio al activar trazas.")
        return cls(
            tracing=tracing,
            api_key=api_key,
            endpoint=values.get(
                "LANGSMITH_ENDPOINT",
                "https://api.smith.langchain.com",
            ).rstrip("/"),
            project=values.get("LANGSMITH_PROJECT", "torresegura-agent"),
            environment=values.get("AGENT_ENVIRONMENT", "development"),
        )


class SafeTraceRecorder:
    def __init__(self, settings: ObservabilitySettings = None, client=None):
        self.settings = settings or ObservabilitySettings.from_env()
        self.client = client
        if self.settings.tracing and self.client is None:
            from langsmith import Client

            self.client = Client(
                api_url=self.settings.endpoint,
                api_key=self.settings.api_key,
                hide_inputs=True,
                hide_outputs=True,
                hide_metadata=self._safe_metadata,
                omit_traced_runtime_info=True,
            )

    def record(self, metadata: Dict[str, Any]):
        if not self.settings.tracing:
            return
        safe_metadata = self._safe_metadata(
            {"environment": self.settings.environment, **metadata}
        )
        try:
            self.client.create_run(
                "torresegura_agent",
                inputs={},
                outputs={},
                run_type="chain",
                id=uuid4(),
                project_name=self.settings.project,
                extra={"metadata": safe_metadata},
                tags=[
                    safe_metadata.get("environment", "unknown"),
                    safe_metadata.get("outcome", "unknown"),
                ],
            )
        except Exception:
            # La observabilidad nunca debe interrumpir la operación de negocio.
            return

    @staticmethod
    def _safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = sanitize_trace_data(metadata)
        return {
            key: value
            for key, value in sanitized.items()
            if key in SAFE_METADATA_KEYS and isinstance(value, (str, int, float, bool))
        }
