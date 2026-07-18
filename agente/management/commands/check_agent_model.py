"""Prueba capacidades mínimas del modelo configurado sin ejecutar tools."""

import json

from django.core.management.base import BaseCommand, CommandError

from agente.llm import get_llm_adapter


class Command(BaseCommand):
    help = "Comprueba chat, español, JSON y tool calling de Qwen."

    def handle(self, *args, **options):
        adapter = get_llm_adapter()
        health = adapter.health_check()
        if not health.get("healthy") or not health.get("model_available", True):
            raise CommandError(f"Modelo no disponible: {health.get('error_code', health)}")

        spanish = adapter.generate("Responde exactamente TORRE_OK")
        if spanish.get("response", "").strip() != "TORRE_OK":
            raise CommandError("La prueba de chat en español falló.")

        structured = adapter.generate_json(
            "Devuelve JSON válido con category=PLOMERIA y urgency=ALTA."
        )
        payload = structured.get("structured_response", {})
        if payload.get("category") != "PLOMERIA" or payload.get("urgency") != "ALTA":
            raise CommandError("La prueba de salida JSON falló.")

        tool = {
            "type": "function",
            "function": {
                "name": "get_area_availability",
                "description": "Consulta disponibilidad real.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "area_id": {"type": "integer"},
                        "date": {"type": "string"},
                    },
                    "required": ["area_id", "date"],
                },
            },
        }
        tool_result = adapter.chat(
            [
                {
                    "role": "user",
                    "content": "Consulta el área 3 para 2026-07-20.",
                }
            ],
            tools=[tool],
        )
        calls = tool_result.get("tool_calls", [])
        if not calls:
            raise CommandError("La prueba de tool calling falló.")
        try:
            arguments = json.loads(calls[0]["function"]["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CommandError("Los argumentos de la tool no son JSON válido.") from exc
        if arguments != {"area_id": 3, "date": "2026-07-20"}:
            raise CommandError(f"Argumentos de tool inesperados: {arguments}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Qwen OK: {adapter.provider}/{adapter.model}; chat, JSON y tools."
            )
        )
