"""Valida el dataset de regresión conversacional."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Valida estructura, IDs y cobertura mínima del dataset del agente."

    def handle(self, *args, **options):
        dataset_path = (
            Path(__file__).resolve().parents[2] / "evaluation" / "dataset.json"
        )
        try:
            scenarios = json.loads(dataset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Dataset inválido: {exc}") from exc
        if not isinstance(scenarios, list) or len(scenarios) < 12:
            raise CommandError("El dataset debe contener al menos 12 escenarios.")
        ids = set()
        for scenario in scenarios:
            if not all(key in scenario for key in ("id", "turns", "expected")):
                raise CommandError(f"Escenario incompleto: {scenario}")
            if scenario["id"] in ids:
                raise CommandError(f"ID duplicado: {scenario['id']}")
            if not scenario["turns"] or not scenario["expected"]:
                raise CommandError(f"Escenario vacío: {scenario['id']}")
            ids.add(scenario["id"])
        self.stdout.write(
            self.style.SUCCESS(f"Dataset válido: {len(scenarios)} escenarios.")
        )
