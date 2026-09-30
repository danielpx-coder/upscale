from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

Progress = Callable[[int, int], None]


@dataclass
class Capabilities:
    quality: int = 3                # 1..5 (subjetivo)
    speed: int = 3                  # 1..5 (subjetivo)
    free: bool = True
    needs_gpu: bool = False
    models: tuple[str, ...] = ()
    default_model: str | None = None
    install: str = ""               # dica de instalação quando faltar dependência
    env: tuple[str, ...] = ()       # variáveis de ambiente relevantes


class Backend(ABC):
    name = "base"
    description = ""
    capabilities = Capabilities()

    def __init__(self, **opts):
        self.opts = {k: v for k, v in opts.items() if v is not None}

    # utilidades -----------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """Retorna (disponível, motivo se indisponível)."""
        return True, ""

    def check(self) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"Backend '{self.name}' indisponível: {why}. {self.capabilities.install}".strip())

    @abstractmethod
    def upscale(self, src: str, dst: str, width: int, height: int, progress: Progress | None = None,
                should_cancel: Callable[[], bool] | None = None) -> None:
        """Gera `dst` com resolução width x height a partir de `src`."""


def which(name: str) -> str | None:
    return shutil.which(name)
