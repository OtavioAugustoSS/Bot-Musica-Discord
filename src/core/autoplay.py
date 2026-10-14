"""Autoplay: quando a fila acaba, continua com faixas relacionadas (YouTube Music).

A rádio fica presa à SEMENTE — a última música pedida por alguém. Antes cada
lote de 3 saía da rádio da última faixa tocada, que já era uma sugestão: a
cadeia se afastava da música pedida a cada lote (27/09: "Amigo da Minha Saudade"
tinha 49 relacionadas, a rádio usou 3 e o lote seguinte já saiu do Panda).
Agora as relacionadas da semente são a ESTAÇÃO, consumida em ordem; esgotada,
ela se estende com as relacionadas das faixas dela mesma — um passo da semente.
"""
from __future__ import annotations

import logging

from core.queue import Track
from services import youtube

log = logging.getLogger(__name__)

_TAMANHO_DA_RADIO = 100  # o YouTube Music devolve ~49
_EXTENSOES_POR_LOTE = 3  # rádios extras buscadas num lote, no máximo
_ESTACOES_GUARDADAS = 8


class _Estacao:
    def __init__(self) -> None:
        self.faixas: list[dict] = []
        self.dadas: set[str] = set()  # já entregues, mesmo as que falharam ao tocar
        self.extensoes = 0  # quantas faixas da estação já viraram semente de extensão


_estacoes: dict[str, _Estacao] = {}


def _estacao(semente: str) -> tuple[_Estacao, bool]:
    """(a estação da semente, se é nova)."""
    if semente in _estacoes:
        return _estacoes[semente], False
    while len(_estacoes) >= _ESTACOES_GUARDADAS:
        _estacoes.pop(next(iter(_estacoes)))
    _estacoes[semente] = _Estacao()
    return _estacoes[semente], True


def _acrescenta(estacao: _Estacao, semente: str, novas: list[dict]) -> int:
    conhecidas = {f["video_id"] for f in estacao.faixas} | {semente}
    antes = len(estacao.faixas)
    for faixa in novas:
        if faixa["video_id"] and faixa["video_id"] not in conhecidas:
            estacao.faixas.append(faixa)
            conhecidas.add(faixa["video_id"])
    return len(estacao.faixas) - antes


async def fetch_autoplay_tracks(
    recent_ids: list[str], limit: int = 3, *, semente: str | None = None
) -> list[Track]:
    """Próximas `limit` da estação da semente (ou da última tocada, sem semente)."""
    semente = semente or (recent_ids[-1] if recent_ids else None)
    if not semente:
        return []
    estacao, nova = _estacao(semente)
    recentes = set(recent_ids)

    def livres() -> list[dict]:
        return [
            f for f in estacao.faixas
            if f["video_id"] not in recentes and f["video_id"] not in estacao.dadas
        ]

    try:
        if nova:
            _acrescenta(estacao, semente, await youtube.related_tracks(semente, limit=_TAMANHO_DA_RADIO))
        extensoes = 0
        while len(livres()) < limit and extensoes < _EXTENSOES_POR_LOTE and estacao.extensoes < len(estacao.faixas):
            base = estacao.faixas[estacao.extensoes]["video_id"]
            estacao.extensoes += 1
            extensoes += 1
            ganhou = _acrescenta(estacao, semente, await youtube.related_tracks(base, limit=_TAMANHO_DA_RADIO))
            log.info("autoplay: estação de %s esgotando — estendida pela rádio de %s (+%d)", semente, base, ganhou)
    except Exception as exc:  # rede/API fora: autoplay simplesmente não adiciona nada
        # autoplay morto parecia autoplay desligado — pelo menos fica no log
        log.warning("autoplay: related_tracks falhou: %r", exc)
        if nova and not estacao.faixas:
            _estacoes.pop(semente, None)  # tenta de novo no próximo lote
    fresh = livres()[:limit]
    estacao.dadas.update(f["video_id"] for f in fresh)
    # rádio que "parou de tocar" era indistinguível de rádio sem sugestão
    log.info(
        "autoplay: %d sugestão(ões) da estação de %s (%d na estação, %d já dadas)",
        len(fresh), semente, len(estacao.faixas), len(estacao.dadas) - len(fresh),
    )
    return [
        Track(
            title=r["title"],
            artist=r["artist"],
            video_id=r["video_id"],
            duration=r["duration"],
            thumbnail=r["thumbnail"],
            requester_id=0,
            from_autoplay=True,
        )
        for r in fresh
    ]
