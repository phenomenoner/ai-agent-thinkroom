from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .schemas import PerspectiveV1, ResearchRequest


class DomainPack(Protocol):
    name: str
    prompt_version: str
    safety: str
    guidance: str

    def frame(self, request: ResearchRequest) -> dict[str, object]: ...
    def fallbacks(self, count: int) -> list[PerspectiveV1]: ...


@dataclass(frozen=True)
class Pack:
    name: str
    prompt_version: str
    guidance: str
    safety: str
    labels: tuple[str, ...]

    def frame(self, request: ResearchRequest) -> dict[str, object]:
        return {
            "question": request.question,
            "context": request.context or "",
            "domain": self.name,
            "guidance": self.guidance,
            "safety": self.safety,
        }

    def fallbacks(self, count: int) -> list[PerspectiveV1]:
        return [
            PerspectiveV1(
                id=f"perspective-{i + 1}",
                title=label,
                hypothesis=f"Assess the question through {label.lower()}.",
                approach=f"Use a {label.lower()} analysis with explicit assumptions and checks.",
                differentiator=f"Prioritizes {label.lower()}.",
            )
            for i, label in enumerate(self.labels[:count])
        ]


PACKS: dict[str, Pack] = {
    "generic": Pack(
        "generic",
        "generic-v1",
        "evidence quality, feasibility, reversibility, consequences, missing information",
        "General decision support; state uncertainty.",
        (
            "Evidence quality",
            "Feasibility",
            "Contrarian",
            "Reversibility",
            "First principles",
            "Missing information",
        ),
    ),
    "coding": Pack(
        "coding",
        "coding-v1",
        "correctness, simplicity, maintainability, migration and rollback cost, security, operability, testability",
        "Advisory only. Do not modify repositories; identify needed repository evidence.",
        (
            "Minimal change",
            "Modular refactor",
            "Performance",
            "Security",
            "Operability",
            "Migration",
        ),
    ),
    "trading": Pack(
        "trading",
        "trading-v1",
        "thesis quality, out-of-sample validity, robustness, drawdown, regime dependency, execution cost, alternative explanations",
        "Research and decision support only. Never execute trades or provide execution instructions.",
        (
            "Fundamentals",
            "Valuation",
            "Macro regime",
            "Contrarian",
            "Robustness",
            "Execution costs",
        ),
    ),
}


def get_pack(name: str) -> Pack:
    try:
        return PACKS[name]
    except KeyError as exc:
        raise ValueError(f"unknown domain pack: {name}") from exc
