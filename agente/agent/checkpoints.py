"""Construcción del checkpointer durable de LangGraph sobre SQLite."""

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Mapping, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "agent_checkpoints.sqlite3"


@dataclass(frozen=True)
class CheckpointSettings:
    backend: str
    sqlite_path: str

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None):
        values = environ if environ is not None else os.environ
        backend = values.get("AGENT_CHECKPOINT_BACKEND", "sqlite").lower().strip()
        if backend not in {"memory", "sqlite"}:
            raise ValueError(
                "AGENT_CHECKPOINT_BACKEND debe ser 'memory' o 'sqlite'."
            )

        raw_path = values.get(
            "AGENT_CHECKPOINT_SQLITE_PATH",
            str(DEFAULT_SQLITE_PATH),
        ).strip()
        path = Path(raw_path or DEFAULT_SQLITE_PATH).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path

        return cls(
            backend=backend,
            sqlite_path=str(path.resolve()),
        )


class CheckpointRuntime:
    def __init__(self, saver, backend: str, connection=None):
        self.saver = saver
        self.backend = backend
        self.connection = connection

    @property
    def durable(self) -> bool:
        return self.backend == "sqlite"

    def close(self):
        if self.connection is not None:
            self.connection.close()


def build_checkpoint_runtime(
    settings: Optional[CheckpointSettings] = None,
) -> CheckpointRuntime:
    config = settings or CheckpointSettings.from_env()
    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=None,
    )
    if config.backend == "memory":
        return CheckpointRuntime(
            saver=InMemorySaver(serde=serializer),
            backend="memory",
        )

    sqlite_path = Path(config.sqlite_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        sqlite_path,
        timeout=30,
        check_same_thread=False,
    )
    connection.execute("PRAGMA busy_timeout=30000")
    saver = SqliteSaver(connection, serde=serializer)
    saver.setup()
    return CheckpointRuntime(
        saver=saver,
        backend="sqlite",
        connection=connection,
    )


_runtime = None
_runtime_lock = Lock()


def get_checkpoint_runtime() -> CheckpointRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = build_checkpoint_runtime()
    return _runtime
