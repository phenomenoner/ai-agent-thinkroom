from __future__ import annotations

from .packs import Pack
from .schemas import PerspectiveV1, ResearchRequest


def orthogonal(request: ResearchRequest, pack: Pack) -> list[PerspectiveV1]:
    return pack.fallbacks(request.branch_count)


STRATEGIES = {"orthogonal": orthogonal}
