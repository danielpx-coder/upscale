from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable

Progress = Callable[[int, int], None]


class Backend(ABC):
    name = "base"
    free = True
    description = ""

    def __init__(self, **opts):
        self.opts = opts

    @abstractmethod
    def upscale(self, src: str, dst: str, width: int, height: int, progress: Progress | None = None) -> None:
        """Gera `dst` com resolução width x height a partir de `src`."""
