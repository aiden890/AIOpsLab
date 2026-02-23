# Backward-compatibility shim — import from the executor package instead.
from aiopslab.orchestrator.static_actions.executor.api_router import (
    load_config,
    get_chat_completion,
)

__all__ = ["load_config", "get_chat_completion"]
