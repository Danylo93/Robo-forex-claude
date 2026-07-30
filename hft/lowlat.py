"""Primitivas de baixa latência aplicáveis em Python.

Nenhuma delas transforma Python em C++, mas todas reduzem jitter — e jitter é o
que mata estratégia de curtíssimo prazo. O ganho real vem na cauda (p99/p99.9),
não na média.

O que realmente ajuda, em ordem de impacto:
 1. desligar o coletor de lixo durante a sessão (evita pausas de ms);
 2. pré-alocar tudo no aquecimento (nada de alocar no caminho crítico);
 3. fixar o processo em um núcleo isolado (afinidade de CPU);
 4. laço em busy-spin em vez de sleep (troca CPU por previsibilidade).
"""

from __future__ import annotations

import gc
import os
import time
from typing import Callable, Generic, Iterator, Optional, TypeVar

T = TypeVar("T")


class RingBuffer(Generic[T]):
    """Fila circular de tamanho fixo, sem alocação após a criação."""

    __slots__ = ("_items", "_capacity", "_head", "_tail", "_size", "dropped")

    def __init__(self, capacity: int = 4096):
        self._capacity = max(2, capacity)
        self._items: list[Optional[T]] = [None] * self._capacity
        self._head = 0
        self._tail = 0
        self._size = 0
        self.dropped = 0

    def push(self, item: T) -> bool:
        """Insere; devolve False e conta descarte quando está cheio."""
        if self._size >= self._capacity:
            self.dropped += 1
            return False
        self._items[self._tail] = item
        self._tail = (self._tail + 1) % self._capacity
        self._size += 1
        return True

    def pop(self) -> Optional[T]:
        if self._size == 0:
            return None
        item = self._items[self._head]
        self._items[self._head] = None
        self._head = (self._head + 1) % self._capacity
        self._size -= 1
        return item

    def drain(self) -> Iterator[T]:
        while self._size:
            item = self.pop()
            if item is not None:
                yield item

    def __len__(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def full(self) -> bool:
        return self._size >= self._capacity


class ObjectPool(Generic[T]):
    """Reaproveita objetos para não alocar no caminho crítico."""

    __slots__ = ("_factory", "_reset", "_pool", "created", "reused")

    def __init__(self, factory: Callable[[], T], size: int = 256,
                 reset: Optional[Callable[[T], None]] = None):
        self._factory = factory
        self._reset = reset
        self._pool: list[T] = [factory() for _ in range(size)]
        self.created = size
        self.reused = 0

    def acquire(self) -> T:
        if self._pool:
            self.reused += 1
            return self._pool.pop()
        self.created += 1
        return self._factory()

    def release(self, item: T) -> None:
        if self._reset:
            self._reset(item)
        self._pool.append(item)

    @property
    def available(self) -> int:
        return len(self._pool)


class TradingRuntime:
    """Prepara o processo para a sessão e devolve o ambiente ao normal no fim."""

    def __init__(self, disable_gc: bool = True, cpu: Optional[int] = None,
                 warmup_calls: int = 5_000):
        self.disable_gc = disable_gc
        self.cpu = cpu
        self.warmup_calls = warmup_calls
        self.notes: list[str] = []
        self._gc_was_enabled = False

    def __enter__(self) -> "TradingRuntime":
        self.setup()
        return self

    def __exit__(self, *exc) -> None:
        self.teardown()
        return None

    def setup(self) -> None:
        self._gc_was_enabled = gc.isenabled()
        if self.disable_gc:
            gc.collect()
            if hasattr(gc, "freeze"):
                gc.freeze()  # move o que já existe para fora da varredura
            gc.disable()
            self.notes.append("coletor de lixo desligado durante a sessão")
        if self.cpu is not None:
            if hasattr(os, "sched_setaffinity"):
                try:
                    os.sched_setaffinity(0, {self.cpu})
                    self.notes.append(f"processo fixado no núcleo {self.cpu}")
                except (OSError, ValueError) as exc:
                    self.notes.append(f"não foi possível fixar o núcleo: {exc}")
            else:
                self.notes.append("afinidade de CPU indisponível neste sistema")
        self._warmup()

    def teardown(self) -> None:
        if self.disable_gc:
            if hasattr(gc, "unfreeze"):
                gc.unfreeze()
            if self._gc_was_enabled:
                gc.enable()
            gc.collect()

    def _warmup(self) -> None:
        """Força a JIT do interpretador e o cache de branches antes do primeiro tick."""
        total = 0.0
        for i in range(self.warmup_calls):
            total += (i * 1.000001) / 1.0000001
        self.notes.append(f"aquecimento: {self.warmup_calls} iterações ({total:.0f})")


def spin_wait(nanos: int) -> None:
    """Espera ocupada: previsível, ao custo de queimar CPU."""
    deadline = time.perf_counter_ns() + nanos
    while time.perf_counter_ns() < deadline:
        pass


def busy_poll(
    poll: Callable[[], object],
    on_item: Callable[[object], None],
    duration_s: float,
    idle_spin_ns: int = 1_000,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Laço de espera ocupada: chama `poll` continuamente por `duration_s`.

    Sem `sleep`: o agendador do sistema operacional adiciona de 1 a 15 ms de
    incerteza, que é uma eternidade quando o alvo é 1 pip.
    """
    started = clock()
    processed = 0
    while clock() - started < duration_s:
        item = poll()
        if item is None:
            spin_wait(idle_spin_ns)
            continue
        on_item(item)
        processed += 1
    return processed
