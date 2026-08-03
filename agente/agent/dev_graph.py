import os

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "condominio_app.settings",
)

import django

django.setup()

from agente.agent.graph import build_agent_graph

# Agent Server administra el checkpointer en modo desarrollo.
graph = build_agent_graph(checkpointer=None)