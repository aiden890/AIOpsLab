# Backward-compatibility shim — import from the executor package instead.
from aiopslab.orchestrator.static_actions.executor import StaticRCAActionsWithExecutor

__all__ = ["StaticRCAActionsWithExecutor"]
