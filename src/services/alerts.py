"""Avisos ao dono do bot por DM: reinício sujo, extração quebrada, cookies mortos.

Na VM foram 23 subidas do serviço em 30 dias sem ninguém saber o motivo de
nenhuma: o systemd reinicia (Restart=always) e o journal ninguém lê. E o
apagão de agosto/2026 durou dias porque o bot subia "saudável" enquanto
toda faixa falhava.

Três peças:
  - RunState: marcador em disco. `begin()` no boot devolve o estado da
    execução anterior se ela morreu sem `mark_clean_exit()` (o SIGTERM do
    systemd marca; crash/OOM não). Também guarda quando cada aviso foi
    mandado, para uma condição avisar no máximo uma vez por janela — e isso
    sobrevive a reinícios, senão todo boot repetiria o aviso.
  - install_error_log: handler de logging que espelha WARNING+ num arquivo
    pequeno e rotativo; é a "última página do journal" que o bot consegue
    ler sozinho ao subir depois de uma morte.
  - OwnerAlerts: manda a DM ao dono do app (vem da API do Discord — nada
    para configurar). DM fechada vira log, nunca exceção.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from logging.handlers import RotatingFileHandler

import config
import messages as m
from services.observabilidade import mascara

log = logging.getLogger(__name__)

_now = time.time  # indireção para os testes controlarem o relógio
_DISCORD_MAX = 2000


class RunState:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict = {}

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
        except OSError as exc:
            log.warning("não consegui gravar o estado de execução: %s", exc)

    def begin(self) -> dict | None:
        """Registra o início desta execução; devolve a anterior se morreu suja."""
        anterior = self._load()
        suja = bool(anterior) and not anterior.get("clean_exit", True)
        self._data = {
            "started_at": _now(),
            "clean_exit": False,
            "sent": dict(anterior.get("sent") or {}),
        }
        self._save()
        return anterior if suja else None

    def mark_clean_exit(self) -> None:
        self._data["clean_exit"] = True
        self._save()

    def should_send(self, key: str, *, ttl_s: float) -> bool:
        """True (e registra) se este aviso ainda não saiu nesta janela."""
        sent = self._data.setdefault("sent", {})
        ultimo = sent.get(key)
        if ultimo is not None and _now() - float(ultimo) < ttl_s:
            return False
        sent[key] = _now()
        self._save()
        return True

    def forget(self, key: str) -> None:
        """Desfaz o registro (a entrega falhou: vale tentar de novo depois)."""
        if self._data.get("sent", {}).pop(key, None) is not None:
            self._save()


def install_error_log(path: str, max_bytes: int = 200_000) -> logging.Handler:
    """Espelha WARNING+ de todos os loggers em `path` (rotativo, 2 arquivos)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=1, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    # com máscara de segredos: este arquivo é COLADO numa mensagem do Discord
    # (DM ao dono ou canal do servidor) quando o processo morre
    from services.observabilidade import FORMATO, Redator

    handler.setFormatter(Redator(FORMATO))
    logging.getLogger().addHandler(handler)
    return handler


def tail_errors(path: str, lines: int = 12) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            todas = fh.read().splitlines()
    except OSError:
        return ""
    return "\n".join(todas[-lines:])


class OwnerAlerts:
    def __init__(self, bot, state: RunState) -> None:
        self.bot = bot
        self.state = state

    async def send(self, key: str, text: str, *, ttl_s: float = 24 * 3600) -> bool:
        """Manda `text` ao dono, no máximo uma vez por `ttl_s` para esta `key`.

        Primeiro por DM; se o Discord recusar (403 "Cannot send messages to
        this user" — validado na VM: o dono não aceita DM do bot), cai para
        um canal de texto: ALERT_CHANNEL_ID do .env ou o canal de sistema
        do servidor, mencionando o dono. Sem os dois, desiste e deixa a
        janela aberta para tentar de novo.
        """
        if not self.state.should_send(key, ttl_s=ttl_s):
            return False
        dono = None
        try:
            dono = (await self.bot.application_info()).owner
            await dono.send(text[:_DISCORD_MAX])
        except Exception as exc:  # noqa: BLE001 — DM fechada/API fora: tenta o canal
            log.warning("DM ao dono falhou (%s): %s — tentando um canal", key, exc)
            if not await self._send_to_channel(text, dono):
                self.state.forget(key)
                return False
        log.info("aviso ao dono enviado: %s", key)
        return True

    async def _send_to_channel(self, text: str, dono) -> bool:
        canal = self._channel()
        if canal is None:
            log.warning("sem canal para avisar: defina ALERT_CHANNEL_ID no .env")
            return False
        mencao = f"<@{dono.id}> " if dono is not None and getattr(dono, "id", None) else ""
        try:
            await canal.send((mencao + text)[:_DISCORD_MAX])
        except Exception as exc:  # noqa: BLE001 — canal sem permissão/apagado
            log.warning("não consegui avisar no canal: %s", exc)
            return False
        return True

    def _channel(self):
        if config.ALERT_CHANNEL_ID:
            try:
                canal = self.bot.get_channel(int(config.ALERT_CHANNEL_ID))
            except ValueError:
                canal = None
            if canal is not None:
                return canal
            log.warning("ALERT_CHANNEL_ID=%r não é um canal que eu veja", config.ALERT_CHANNEL_ID)
        for guild in getattr(self.bot, "guilds", []) or []:
            for canal in [guild.system_channel, *getattr(guild, "text_channels", [])]:
                if canal is None:
                    continue
                try:
                    if canal.permissions_for(guild.me).send_messages:
                        return canal
                except Exception:  # noqa: BLE001 — objeto estranho: tenta o próximo
                    continue
        return None


async def entrega_avisos(dono: OwnerAlerts, pasta: str) -> int:
    """Entrega ao dono cada .txt deixado em `pasta` e apaga os entregues.

    É o canal de quem roda FORA do bot — o canário do yt-dlp é um shell script
    num timer do systemd e não tem como mandar DM. Aviso que não saiu (DM e
    canal recusados) fica no lugar para a próxima rodada; arquivo vazio some.
    """
    try:
        nomes = sorted(n for n in os.listdir(pasta) if n.endswith(".txt"))
    except OSError:
        return 0
    entregues = 0
    for nome in nomes:
        caminho = os.path.join(pasta, nome)
        try:
            with open(caminho, encoding="utf-8", errors="replace") as fh:
                texto = mascara(fh.read().strip())[:1900]
        except OSError:
            continue
        if texto and not await dono.send(f"aviso:{nome}", texto, ttl_s=0):
            continue
        entregues += bool(texto)
        estado = getattr(dono, "state", None)
        if estado is not None:
            estado.forget(f"aviso:{nome}")  # cada arquivo sai uma vez: a chave não precisa ficar
        try:
            os.remove(caminho)
        except OSError:
            pass
    return entregues


class Sintomas:
    """Conta, numa janela de 1h, os sinais de que o YouTube mudou de novo.

    O apagão de agosto/2026 durou dias: o bot subia "saudável", anunciava
    "tocando" e ficava mudo; o dono só soube quando reclamaram. Os sinais já
    estavam no journal — agora alguém os conta. Passou do limiar: `conta()`
    devolve True UMA vez por janela, e quem chamou dispara `avisa()`.

    Limiares diferentes porque os sinais têm ruído diferente: extração falha
    por vídeo removido/privado (playlist velha dá 4 seguidas sem o YouTube ter
    mudado nada); faixa que morre na largada e áudio que não é Opus são raros.
    """

    LIMIARES = {"largada": 3, "formato": 3, "extracao": 5}

    def __init__(self, dono: OwnerAlerts, *, pedido_path: str = "", janela_s: float = 3600.0,
                 agora=time.monotonic) -> None:
        self._dono = dono
        self._pedido_path = pedido_path
        self._janela_s = janela_s
        self._agora = agora
        self._vistos: dict[str, deque[float]] = {}
        self._aceso_ate: dict[str, float] = {}

    def conta(self, nome: str) -> bool:
        agora = self._agora()
        vistos = self._vistos.setdefault(nome, deque())
        vistos.append(agora)
        while vistos and agora - vistos[0] > self._janela_s:
            vistos.popleft()
        if len(vistos) < self.LIMIARES.get(nome, 3) or agora < self._aceso_ate.get(nome, float("-inf")):
            return False
        self._aceso_ate[nome] = agora + self._janela_s
        return True

    def quantas(self, nome: str) -> int:
        return len(self._vistos.get(nome, ()))

    async def avisa(self, nome: str, detalhe: str) -> None:
        """Conta ao dono e pede o canário (scripts/canario_ytdlp.sh, via systemd .path)."""
        log.warning("sintoma de YouTube quebrado: %s (%dx em 1h) — %s", nome, self.quantas(nome), detalhe)
        if self._pedido_path:
            try:
                with open(self._pedido_path, "w", encoding="utf-8") as fh:
                    fh.write(f"{nome}\n")
            except OSError as exc:
                log.warning("não consegui pedir o canário do yt-dlp (%s): %s", self._pedido_path, exc)
        texto = m.ALERT_SINTOMA.format(
            o_que=m.SINTOMA_NOMES.get(nome, nome), quantas=self.quantas(nome), detalhe=detalhe[:300]
        )
        await self._dono.send(f"sintoma:{nome}", texto, ttl_s=6 * 3600)
