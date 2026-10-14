"""Cache do player do YouTube já pré-processado pelo solver de desafios JS.

Com o perfil de cookies, TODA extração resolve o desafio `n` do player, e o
yt-dlp manda o player inteiro (3,2MB) para o deno refazer o parse a cada
vez: 11 a 18s de CPU cheia na VM, e o prefetch dispara isso a cada faixa
nova. Nesse tempo o hipervisor pausa a VM inteira (steal de 60–74%) e os
pacotes de voz saem aos trancos — a música trava, acelera e fica lenta.

O yt-dlp sabe guardar o player pré-processado e mandar só ele, mas vem
desligado porque os arquivos são grandes (~4MB) e ele não faz rotação.
Aqui ligamos e cuidamos do resto: rotação, descarte quando o yt-dlp muda de
versão e, se mesmo assim um player em cache não resolver, ele é apagado e o
desafio é refeito do zero na mesma chamada.
"""
from __future__ import annotations

import logging
import os
import time

import yt_dlp
from yt_dlp.utils import expand_path

log = logging.getLogger(__name__)

try:
    from yt_dlp.extractor.youtube.jsc._builtin import ejs as _ejs
except ImportError:  # yt-dlp sem EJS: nada a otimizar
    _ejs = None

MARCADOR = ".bot-versao"
_PREFIXO = "player,"  # como o yt-dlp grava a chave "player:<url>"
MANTER = 2  # o player do YouTube muda a cada poucos dias; 2 cobre a virada
INTERVALO_S = 3600.0
_agora = time.monotonic  # indireção para os testes controlarem o relógio
_ultima_manutencao = float("-inf")


def versao() -> str:
    """O que invalida um player pré-processado: yt-dlp ou script do solver novos."""
    script = getattr(getattr(_ejs, "EJSBaseJCP", None), "_SCRIPT_VERSION", "?")
    return f"yt-dlp {yt_dlp.version.__version__} / ejs {script}"


def raiz() -> str:
    """Pasta da seção do solver — mesma regra do yt-dlp (o bot não muda o cachedir)."""
    base = os.path.join(os.getenv("XDG_CACHE_HOME", "~/.cache"), "yt-dlp")
    secao = getattr(getattr(_ejs, "EJSBaseJCP", None), "_CACHE_SECTION", "challenge-solver")
    return os.path.join(expand_path(base), secao)


def _com_rede(original):
    """Embrulha `_real_bulk_solve`: player em cache que falha é apagado e refeito.

    Sem isto, um player pré-processado ruim (gravado por outra versão do
    solver, por exemplo) quebraria em silêncio TODA extração desse player —
    o yt-dlp só avisa e descarta os formatos que dependiam do desafio.
    """

    def resolve(self, requests):
        entregues = 0
        try:
            for resposta in original(self, requests):
                entregues += 1
                yield resposta
            return
        except Exception as exc:
            chaves = {f"player:{r.input.player_url}" for r in requests}
            em_cache = [c for c in chaves if self.ie.cache.load(self._CACHE_SECTION, c)]
            if not em_cache:
                raise
            for chave in em_cache:
                self.ie.cache.store(self._CACHE_SECTION, chave, None)
            log.warning(
                "player pré-processado em cache não resolveu o desafio (%s) —"
                " apagado; %s",
                str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__,
                "refazendo sem ele" if not entregues else "a próxima extração refaz",
            )
            if entregues:
                raise  # repetir entregaria respostas em dobro ao yt-dlp
        yield from original(self, requests)

    resolve._bot_rede = True
    return resolve


def ativa() -> bool:
    """Liga o cache do player pré-processado. False se o yt-dlp não tiver EJS."""
    cls = getattr(_ejs, "EJSBaseJCP", None)
    if cls is None or not hasattr(cls, "_ENABLE_PREPROCESSED_PLAYER_CACHE"):
        log.debug("yt-dlp sem cache de player pré-processado — seguindo sem")
        return False
    cls._ENABLE_PREPROCESSED_PLAYER_CACHE = True
    if not getattr(cls._real_bulk_solve, "_bot_rede", False):
        cls._real_bulk_solve = _com_rede(cls._real_bulk_solve)
    return True


def manutencao(pasta: str | None = None, *, manter: int = MANTER) -> int:
    """Rotação dos players em cache. Devolve quantos arquivos apagou.

    Barata (lista uma pasta pequena) e com intervalo mínimo entre rodadas, para
    poder ser chamada no caminho de cada extração.
    """
    global _ultima_manutencao
    agora = _agora()
    if agora - _ultima_manutencao < INTERVALO_S:
        return 0
    _ultima_manutencao = agora
    pasta = pasta or raiz()
    try:
        os.makedirs(pasta, exist_ok=True)
        players = [
            os.path.join(pasta, nome)
            for nome in os.listdir(pasta)
            if nome.startswith(_PREFIXO) and nome.endswith(".json")
        ]
        marcador = os.path.join(pasta, MARCADOR)
        try:
            with open(marcador, encoding="utf-8") as fh:
                versao_gravada = fh.read()
        except OSError:
            versao_gravada = None
        atual = versao()
        if versao_gravada != atual:
            apagar = players  # gravados por outro solver (ou sem origem conhecida)
        else:
            players.sort(key=os.path.getmtime, reverse=True)
            apagar = players[manter:]
        removidos = 0
        for caminho in apagar:
            try:
                os.remove(caminho)
                removidos += 1
            except OSError:
                pass  # outra thread chegou antes; o yt-dlp tolera arquivo sumindo
        if versao_gravada != atual:
            with open(marcador, "w", encoding="utf-8") as fh:
                fh.write(atual)
    except OSError as exc:
        log.debug("manutenção do cache de players falhou: %r", exc)
        return 0
    if removidos:
        log.info("cache de players pré-processados: %d antigo(s) apagado(s)", removidos)
    return removidos
