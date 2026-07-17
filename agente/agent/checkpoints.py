"""Construccion del checkpointer de LangGraph."""

from dataclasses import dataclass
import os
from threading import Lock
from typing import Mapping, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class CheckpointSettings:
    backend: str
    database_url: str
    pool_max_size: int

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None):
        values = environ if environ is not None else os.environ
        backend = values.get("AGENT_CHECKPOINT_BACKEND", "memory").lower().strip()
        if backend not in {"memory", "postgres"}:
            raise ValueError(
                "AGENT_CHECKPOINT_BACKEND debe ser 'memory' o 'postgres'."
            )

        database_url = values.get(
            "AGENT_CHECKPOINT_DATABASE_URL",
            values.get("DATABASE_URL", ""),
        ).strip()
        if backend == "postgres" and not database_url:
            raise ValueError(
                "AGENT_CHECKPOINT_DATABASE_URL o DATABASE_URL es obligatorio "
                "para checkpoints PostgreSQL."
            )

        try:
            pool_max_size = int(values.get("AGENT_CHECKPOINT_POOL_MAX_SIZE", "4"))
        except (TypeError, ValueError) as exc:
            raise ValueError("AGENT_CHECKPOINT_POOL_MAX_SIZE no es valido.") from exc
        if pool_max_size < 1:
            raise ValueError("AGENT_CHECKPOINT_POOL_MAX_SIZE debe ser positivo.")

        return cls(
            backend=backend,
            database_url=database_url,
            pool_max_size=pool_max_size,
        )


class CheckpointRuntime:
    def __init__(self, saver, backend: str, pool=None):
        self.saver = saver
        self.backend = backend
        self.pool = pool

    @property
    def durable(self) -> bool:
        return self.backend == "postgres"

    def close(self):
        if self.pool is not None:
            self.pool.close()


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

    pool = ConnectionPool(
        conninfo=config.database_url,
        min_size=1,
        max_size=config.pool_max_size,
        open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    saver = PostgresSaver(pool, serde=serializer)
    saver.setup()
    return CheckpointRuntime(saver=saver, backend="postgres", pool=pool)


_runtime = None
_runtime_lock = Lock()


def get_checkpoint_runtime() -> CheckpointRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = build_checkpoint_runtime()
    return _runtime
