from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class MemoryCheckpointStore:
    """Small checkpoint store used by webhook and poll workers in local/dev."""

    checkpoints: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def put(self, thread_id: str, state: dict[str, Any]) -> str:
        checkpoint_id = str(uuid4())
        self.checkpoints[(thread_id, checkpoint_id)] = deepcopy(state)
        return checkpoint_id

    def get(self, thread_id: str, checkpoint_id: str) -> dict[str, Any]:
        return deepcopy(self.checkpoints[(thread_id, checkpoint_id)])

    def inject_node_result(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        resume_node: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.get(thread_id, checkpoint_id)
        state.setdefault("callback_results", {})[resume_node] = result
        self.checkpoints[(thread_id, checkpoint_id)] = deepcopy(state)
        return state
