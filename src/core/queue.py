"""Fila de reprodução — lógica pura, sem dependência do Discord."""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum


class LoopMode(Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

    def cycle(self) -> "LoopMode":
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        return order[(order.index(self) + 1) % len(order)]


@dataclass
class Track:
    title: str
    artist: str = ""
    video_id: str | None = None  # None => resolver via busca na hora de tocar (importação Spotify)
    duration: int = 0  # segundos; 0 = desconhecida
    thumbnail: str = ""
    requester_id: int = 0
    from_autoplay: bool = False
    # pedida por voz (ou posta "como próxima"): fica NA FRENTE da fila, e os
    # pedidos seguintes entram atrás dela — ver MusicQueue.poe_na_frente
    na_frente: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}" if self.video_id else ""

    @property
    def search_query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    @property
    def display(self) -> str:
        if not self.artist:
            return self.title
        # faixa com meia dúzia de artistas (no journal de 21/09, um tema de anime
        # com 4 dubladoras) ocupava 3 linhas em TODA mensagem, log e linha do
        # painel. A busca (search_query) continua usando todos.
        nomes = [nome.strip() for nome in self.artist.split(",") if nome.strip()]
        artista = self.artist if len(nomes) <= 3 else f"{', '.join(nomes[:2])} e mais {len(nomes) - 2}"
        return f"{self.title} — {artista}"


MAX_QUEUE = 500  # teto anti-spam: playlists em série não estouram a RAM da VM
MAX_HISTORICO = 30  # quantas faixas já tocadas o "volta pra anterior" alcança


class MusicQueue:
    """Duas filas: a que a pessoa pediu e a rádio do autoplay.

    Ficavam numa lista só, e isso produzia o bug relatado em 12/09 — "as
    músicas do autoplay se embaralham no meio da playlist que eu coloquei":
    o que era adicionado depois entrava ATRÁS das faixas de rádio já
    enfileiradas, e o `shuffle` misturava as duas. Separadas, a rádio só é
    consumida quando `upcoming` acaba, então o pedido da pessoa sempre vem
    primeiro — que é o comportamento que qualquer um espera.
    """

    def __init__(self) -> None:
        self.upcoming: list[Track] = []  # o que a pessoa pediu
        self.radio: list[Track] = []  # autoplay: só entra quando upcoming acaba
        self.current: Track | None = None
        self.loop_mode: LoopMode = LoopMode.OFF
        # o que já tocou, da mais antiga para a mais recente ("volta pra anterior")
        self.historico: list[Track] = []
        self._voltando = False  # o próximo advance() é a volta: não é "já tocou"

    def __len__(self) -> int:
        return len(self.upcoming)

    def add(self, track: Track) -> None:
        if len(self.upcoming) < MAX_QUEUE:
            self.upcoming.append(track)

    def add_many(self, tracks: list[Track]) -> int:
        """Adiciona até o teto da fila; retorna quantas entraram de fato."""
        space = MAX_QUEUE - len(self.upcoming)
        if space <= 0:
            return 0
        self.upcoming.extend(tracks[:space])
        return min(len(tracks), space)

    def add_radio(self, tracks: list[Track]) -> int:
        """Enfileira faixas do autoplay, atrás de tudo que a pessoa pediu."""
        space = MAX_QUEUE - len(self.radio)
        if space <= 0:
            return 0
        self.radio.extend(tracks[:space])
        return min(len(tracks), space)

    def next_up(self) -> Track | None:
        """A próxima a tocar, venha da fila ou da rádio (para o prefetch)."""
        if self.upcoming:
            return self.upcoming[0]
        return self.radio[0] if self.radio else None

    def proximas(self, quantas: int) -> list[Track]:
        """As próximas N na ordem real de reprodução (fila e depois rádio)."""
        return (self.upcoming + self.radio)[:quantas]

    def advance(self, *, ignore_loop: bool = False) -> Track | None:
        """Avança para a próxima faixa e a retorna (None se acabou tudo).

        ignore_loop força o avanço mesmo em LoopMode.TRACK (caso do skip).
        """
        if self.current is not None:
            if self.loop_mode is LoopMode.TRACK and not ignore_loop:
                return self.current
            voltando, self._voltando = self._voltando, False
            if not voltando:
                self.historico.append(self.current)
                del self.historico[:-MAX_HISTORICO]
            # voltando: a que tocava já foi posta na frente da fila — devolvê-la
            # também ao fim (loop de fila) a duplicaria
            if self.loop_mode is LoopMode.QUEUE and not voltando:
                # devolve para a lista de origem: faixa de rádio em loop de
                # fila não pode virar "escolha da pessoa"
                destino = self.radio if self.current.from_autoplay else self.upcoming
                destino.append(self.current)
        if self.upcoming:
            self.current = self.upcoming.pop(0)
            # em loop de fila ela volta para o FIM: lá não é mais "da frente"
            self.current.na_frente = False
        elif self.radio:
            self.current = self.radio.pop(0)
        else:
            self.current = None
        return self.current

    # --- por posição e por faixa (comandos de voz de fila) ---
    # As posições são as do painel: 1 = a próxima a tocar, contando a fila da
    # pessoa e DEPOIS a rádio. Negativas contam do fim (-1 = a última).

    def todas(self) -> list[Track]:
        return self.upcoming + self.radio

    def na_posicao(self, posicao: int) -> Track | None:
        todas = self.todas()
        indice = posicao - 1 if posicao > 0 else len(todas) + posicao
        return todas[indice] if posicao != 0 and 0 <= indice < len(todas) else None

    def posicao_de(self, track: Track) -> int:
        """1-based; 0 = não está na fila."""
        for i, outra in enumerate(self.todas(), 1):
            if outra is track:
                return i
        return 0

    def tira(self, tracks: list[Track]) -> list[Track]:
        """Remove estas faixas (por identidade) e devolve as que estavam mesmo na fila."""
        tiradas = []
        for track in tracks:
            for lista in (self.upcoming, self.radio):
                for i, outra in enumerate(lista):
                    if outra is track:
                        tiradas.append(lista.pop(i))
                        break
                else:
                    continue
                break
        return tiradas

    def puxa_para_a_frente(self, track: Track, *, quem: int = 0) -> bool:
        """`track` vira a PRÓXIMA; as outras seguem na ordem ("toca a terceira").

        Faixa da rádio escolhida por alguém deixa de ser rádio: senão sumiria
        junto quando desligassem o autoplay.
        """
        if not self.tira([track]):
            return False
        if track.from_autoplay:
            track.from_autoplay = False
            track.requester_id = quem or track.requester_id
        track.na_frente = True  # o pedido por voz seguinte entra ATRÁS dela
        self.upcoming.insert(0, track)
        return True

    def quantas_na_frente(self) -> int:
        """Quantos pedidos "da frente" (por voz, ou postos como próxima) ainda não tocaram."""
        quantas = 0
        for track in self.upcoming:
            if not track.na_frente:
                break
            quantas += 1
        return quantas

    def poe_na_frente(self, track: Track, *, quem: int = 0) -> int:
        """O pedido por voz: vira a PRÓXIMA — atrás dos outros pedidos da frente que
        ainda não tocaram (ninguém fura ninguém) e na frente do resto. Devolve a
        posição em que ficou.

        Decisão do Otavio em 21/09/2026: no teste dele, "quero escutar a ferida
        curada" e "toca Realidade ou Fantasia" entraram no fim de uma fila de 38
        e, nas duas vezes, ele precisou de um segundo comando para ouvir. Se a
        faixa já estava na fila (ou na rádio), muda de lugar em vez de duplicar.
        """
        self.tira([track])
        if track.from_autoplay:
            track.from_autoplay = False
            track.requester_id = quem or track.requester_id
        posicao = self.quantas_na_frente()
        track.na_frente = True
        self.upcoming.insert(posicao, track)
        return posicao + 1

    def descarta_da_frente(self, quantas: int) -> list[Track]:
        """Pula as `quantas` próximas sem tocar. Em loop de fila elas voltam para
        o fim — pular não é apagar de uma fila que gira."""
        puladas = self.todas()[: max(0, quantas)]
        self.tira(puladas)
        if self.loop_mode is LoopMode.QUEUE:
            for track in puladas:
                (self.radio if track.from_autoplay else self.upcoming).append(track)
        return puladas

    def descarta_ate(self, track: Track) -> list[Track]:
        """Tudo que vinha ANTES de `track` é pulado ("pula pra quarta")."""
        posicao = self.posicao_de(track)
        return self.descarta_da_frente(posicao - 1) if posicao else []

    def volta_para_a_anterior(self, quantas: int = 1) -> Track | None:
        """Põe na frente da fila as últimas `quantas` que tocaram (na ordem em que
        tocaram) e a atual logo atrás. Devolve a que vai tocar, ou None.

        Quem chama dá o skip em seguida. Esse avanço NÃO registra a atual no
        histórico: senão "anterior" duas vezes ficaria alternando entre duas
        faixas em vez de ir cada vez mais para trás.
        """
        if not self.historico:
            return None
        quantas = max(1, min(quantas, len(self.historico)))
        anteriores = self.historico[-quantas:]
        del self.historico[-quantas:]
        self.tira(anteriores)  # em loop de fila elas já tinham voltado para o fim
        if self.current is not None:
            self.upcoming.insert(0, self.current)
            self._voltando = True
        self.upcoming[0:0] = anteriores
        return anteriores[0]

    def shuffle(self) -> None:
        # os pedidos da frente ficam onde estão: embaralhar a playlist não pode
        # enterrar a música que alguém acabou de pedir por voz
        frente = self.quantas_na_frente()
        resto = self.upcoming[frente:]
        random.shuffle(resto)
        self.upcoming[frente:] = resto

    def remove(self, position: int) -> Track | None:
        """Remove e retorna a faixa na posição 1-based da fila; None se inválida."""
        if 1 <= position <= len(self.upcoming):
            return self.upcoming.pop(position - 1)
        return None

    def clear(self) -> None:
        self.upcoming.clear()
        self.radio.clear()
        self.current = None
        self.historico.clear()
        self._voltando = False
