"""Comandos de voz livres → intenção estruturada, via LLM do Groq (grátis).

Usado quando a frase tem a wake word mas o parser fixo (core/intents.py)
não reconhece o comando — o LLM entende linguagem natural e corrige erros
de transcrição ("tira essa e bota algo mais animado" → play).
"""
from __future__ import annotations

import json
import logging
import re

import config
from services import http

log = logging.getLogger(__name__)

_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
# A Groq APOSENTA modelos sem aviso: o `llama-3.3-70b-versatile` morreu em
# 12/09 e o `groq/compound-mini` que o substituiu, em 25/09/2026 — as duas vezes
# o interpretador ficou morto e todo "retirar X da fila" virou "não entendi".
# Por isso uma LISTA: 404 num modelo pula para o próximo na mesma frase.
# Remedido na VM com o prompt real (26/09/2026), 8 frases:
#   openai/gpt-oss-20b    8/8  ~0,32s
#   qwen/qwen3.8-27b      8/8  ~0,33s
#   openai/gpt-oss-120b   7/8  ~0,44s
# Os gpt-oss RACIOCINAM antes de responder: com max_tokens=120 o raciocínio
# comia a saída e a resposta vinha vazia (o "1/5" medido em 12/09).
_CANDIDATOS: list[tuple[str, dict]] = [
    ("openai/gpt-oss-20b", {"reasoning_effort": "low", "include_reasoning": False, "max_tokens": 400}),
    ("qwen/qwen3.8-27b", {"reasoning_effort": "none", "reasoning_format": "hidden", "max_tokens": 120}),
    ("openai/gpt-oss-120b", {"reasoning_effort": "low", "include_reasoning": False, "max_tokens": 400}),
]
# GROQ_LLM_MODEL no .env passa na frente de todos — troca sem deploy
if config.GROQ_LLM_MODEL:
    _extra_conhecido = dict(_CANDIDATOS).get(config.GROQ_LLM_MODEL, {})
    _CANDIDATOS.insert(0, (config.GROQ_LLM_MODEL, _extra_conhecido))
_aposentados: set[str] = set()  # 404 nesta execução: não se tenta de novo
_VALID_ACTIONS = {
    "play", "skip", "pause", "resume", "shuffle", "autoplay", "leave",
    "loop", "loop_off", "clear_queue", "now_playing", "favorite",
    "play_favs", "seek", "volume", "jump", "play_queue", "play_next", "remove",
    "queue", "next_up", "previous", "none",
}
# ações que mexem numa faixa da fila: sem dizer QUAL, não são comando
_PRECISAM_DE_ALVO = ("play", "seek", "volume", "jump", "play_queue", "play_next", "remove")
_SYSTEM = (
    "Você interpreta comandos falados (transcritos, podem conter erros) para o"
    " bot de música Arrodes no Discord. Responda APENAS um JSON:"
    ' {"action": "...", "query": "..."}.\n'
    "Ações: play (tocar música/artista/estilo/playlist — query = o que buscar),"
    " skip (pular a atual), pause, resume (voltar a tocar), shuffle (embaralhar"
    " a fila), autoplay (alternar a rádio automática), leave (sair da chamada),"
    " loop (repetir a música atual), loop_off (parar de repetir),"
    " clear_queue (limpar/esvaziar a fila sem sair),"
    " now_playing (dizer qual música está tocando),"
    " favorite (favoritar/salvar a música atual),"
    " play_favs (tocar a playlist de favoritos de quem falou),"
    ' seek (ir para um ponto da música atual — query = "+30" para avançar 30'
    ' segundos, "-10" para voltar 10, "1:30" para ir ao minuto 1:30, "0" para'
    " recomeçar),"
    ' volume (query = "50" para deixar em 50%, "+20" para aumentar, "-20" para'
    " abaixar),"
    " skip também aceita query = quantas músicas pular de uma vez (\"3\"),"
    " play_queue (tocar AGORA uma música que já está na fila, sem perder as"
    ' outras — query = "#N" para a posição N da fila, "#-1" para a última, ou o'
    " título como aparece na fila),"
    " jump (pular PARA uma música da fila, descartando as que vinham antes —"
    " mesma query), play_next (pôr uma música como a próxima — mesma query, ou o"
    " nome de uma música nova), remove (tirar da fila — mesma query, ou"
    ' "#1-3" para da primeira à terceira, "#3,#5" para várias), queue (mostrar a'
    " fila), next_up"
    " (dizer qual é a próxima), previous (voltar para a música anterior),"
    " none (não é um comando de música).\n"
    "Corrija erros óbvios de transcrição em nomes de músicas e artistas."
    # 26/09: com o exemplo "algo mais animado viram play" o modelo respondia
    # play para qualquer conversa ("o que eu vou postar não é muito bom" →
    # play "algo mais animado") — o bot escuta uma call inteira conversando
    "\nO microfone capta a conversa das pessoas na chamada, e a transcrição"
    " costuma ter o nome Arrodes (ou parecidos, como Herodes ou A Rodes) por"
    " engano. Responda none sempre que a"
    " frase for conversa, comentário, xingamento, piada ou não tiver um pedido"
    " claro ao bot. Só responda play quando a pessoa PEDIR para tocar algo"
    ' ("toca", "põe", "bota", "coloca", "quero ouvir"), e nunca invente uma'
    " busca a partir de uma palavra solta ou dos títulos da fila. Um pedido"
    ' vago com verbo, como "bota algo mais animado", é play com uma busca'
    " adequada. Pedido explícito de pular, pausar, tirar da fila, volume etc."
    " vale mesmo sem o verbo tocar."
    " Quando a mensagem trouxer a fila atual, use-a para descobrir de qual"
    " música da fila a pessoa está falando (por artista, por pedaço do título,"
    " por descrição) e responda com a posição dela."
)


def parse_llm_reply(content: str) -> tuple[str, str] | None:
    """Extrai (action, query) da resposta do LLM; None se inválida."""
    match = re.search(r"\{.*\}", content, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    action = str(data.get("action", "")).strip().lower()
    query = str(data.get("query") or "").strip()
    if action not in _VALID_ACTIONS:
        return None
    if action in _PRECISAM_DE_ALVO and not query:
        return None  # sem o que tocar / para onde ir / quanto / qual, não é comando
    return action, query


async def interpret(text: str, fila: list[str] | None = None) -> tuple[str, str] | None:
    """Frase transcrita → (action, query), ou None se indisponível/inválido.

    `fila`: os títulos das próximas faixas, na ordem — é o que deixa o LLM
    resolver "aquela do Frank Ocean" para uma posição da fila.
    """
    if not config.GROQ_API_KEY or not text:
        return None
    if fila:
        text += "\n\nFila atual:\n" + "\n".join(f"{i}. {nome}" for i, nome in enumerate(fila, 1))
    import aiohttp  # dependência do py-cord, já instalada

    for modelo, extra in _CANDIDATOS:
        if modelo in _aposentados:
            continue
        payload = {
            "model": modelo,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 120,
            **extra,
        }
        try:
            async with http.get_session().post(
                _CHAT_URL,
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            content = data["choices"][0]["message"]["content"] or ""
            break
        except Exception as exc:  # noqa: BLE001 — LLM fora: segue sem interpretação
            # sem este log, chave inválida/429/timeout viravam um silencioso
            # "não entendi" — indistinguível de frase realmente sem sentido.
            # NUNCA use %r aqui: o repr da ClientResponseError do aiohttp inclui os
            # headers da request, ou seja, grava a chave da Groq em texto puro no
            # journal (foi o que aconteceu até 12/09/2026).
            status = getattr(exc, "status", None)
            if status == 404:
                _aposentados.add(modelo)
                log.warning(
                    "LLM do Groq: 404 — o modelo %r não existe mais (a Groq aposenta"
                    " modelos); seguindo com o próximo da lista. Veja os disponíveis"
                    " em /v1/models e ajuste GROQ_LLM_MODEL no .env",
                    modelo,
                )
                continue
            log.warning("LLM do Groq falhou (%s): %s", type(exc).__name__, exc)
            return None
    else:
        return None  # todos aposentados: o aviso de cada um já está no journal
    return parse_llm_reply(content)
