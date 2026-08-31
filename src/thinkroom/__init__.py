from __future__ import annotations

from typing import Any

from .schemas import *  # noqa: F403

__version__ = "0.2.3"
__all__ = [  # noqa: F405
    "Thinkroom",
    "ThinkroomClient",
    "ThinkroomError",
    "create_app",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Load transport/composition exports without polluting core imports."""
    if name in {"Thinkroom", "ThinkroomClient", "ThinkroomError"}:
        from . import sdk

        value = getattr(sdk, name)
    elif name == "create_app":
        from .api import create_app

        value = create_app
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
