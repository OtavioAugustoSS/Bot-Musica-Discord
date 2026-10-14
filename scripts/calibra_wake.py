"""Calibração da wake word com áudio de verdade.

Uso (da raiz do repositório, com GROQ_API_KEY no .env):

    python scripts/calibra_wake.py                          # os WAV de data/voice-debug/
    python scripts/calibra_wake.py a.wav data/wake-amostras/
    python scripts/calibra_wake.py --modelos whisper-large-v3 --json saida.jsonl
    python scripts/calibra_wake.py --sem-nome-only

Grave a sessão com VOICE_DEBUG=1 (o bot guarda os 10 últimos trechos em
data/voice-debug/, PCM 48 kHz estéreo — o formato do Discord) ou com
VOICE_COLETA=1 (data/wake-amostras/, 16 kHz mono, só chamadas confirmadas):
os dois servem. Cada WAV vai a cada modelo de Whisper da Groq duas vezes —
com o prompt de produção (config.VOICE_STT_PROMPT, que abre com o nome) e com
o da 2ª escuta (speech._prompt_sem_nome(), sem ele) — pelas MESMAS funções
que o bot usa. Por transcrição sai o que o bot decidiria: o começo como vai
para o journal (inicio_para_log), se a grafia é aceita como o nome
(_is_wake_word), se a frase abre com ele (chamou_de_frente) e o comando lido
(parse_command); na passada sem o nome, também nome_no_audio e mesmo_comando.

No fim, o resumo: taxa de acerto por modelo e prompt, as grafias da 1ª
palavra com contagem e as SUGESTÕES — grafias que não chamaram o bot, para
_WAKE_VARIANTS em core/intents.py. As que são palavra do português vêm
marcadas: variação que é palavra de verdade faz a conversa chamar o bot.

A taxa de acerto supõe que os trechos são chamadas ao bot ("Arrodes, pula").
Com trechos de conversa, ela é a taxa de falso positivo.

Ritmo: no máximo --rpm requisições por minuto por modelo (19: o tier grátis
dá 20). A cota do DIA (2000 por modelo) é a mesma do bot se a chave for a
mesma: N trechos gastam 2N de cada modelo (N com --sem-nome-only).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import wave
from collections import Counter

import numpy as np

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_RAIZ, "src"))

# depois do sys.path: config carrega o .env (GROQ_API_KEY)
import config
from core import intents
from services import http, speech

PROMPT_PRODUCAO = "producao"
PROMPT_SEM_NOME = "sem_nome"
_ROTULO = {PROMPT_PRODUCAO: "com nome", PROMPT_SEM_NOME: "sem nome"}
_DIR_PADRAO = os.path.join(_RAIZ, "data", "voice-debug")
_TAXA = 16000
_MIN_AMOSTRAS = 1600  # < 0,1s: o speech.transcribe nem envia
_RPM_PADRAO = 19  # o tier grátis dá 20 por minuto, por modelo
# abaixo disto a grafia não lembra o nome: ele sumiu da transcrição e nenhuma
# variação resolve — conta à parte
_NOTA_MINIMA_SUGESTAO = 0.5
# palavras do português perto do nome. Sem dicionário instalado, é esta lista
# que marca a sugestão perigosa; com /usr/share/dict/brazilian (pacote
# wbrazilian) ou --dicionario, entra o dicionário inteiro
PALAVRAS_PT = frozenset(intents._WAKE_BLOCKLIST) | {
    "arroz", "arrota", "arroto", "arrotos", "arrotar", "arroba", "arrobas", "arroio", "arroios",
    "rode", "roda", "rodas", "rodar", "rodou", "rodo", "rodos", "rodeio", "rodeios", "rodeia",
    "rodear", "rodela", "rodelas", "rodovia", "rodizio", "acorde", "acordes", "arredor",
    "arredores", "rede", "redes", "arde", "ardes", "arder", "arrojes", "arredondes", "arrombes",
    "arrostes", "arrolhes", "ode", "odes", "heroi", "herois", "erro", "erros", "aro", "aros",
    "arreio", "arreios", "dores", "adores", "torres",
}
_DICIONARIOS = ("/usr/share/dict/brazilian", "/usr/share/dict/portuguese")


# ── áudio ────────────────────────────────────────────────────────────────────


def audio_do_wav(caminho: str) -> np.ndarray:
    """WAV PCM de 16 bits → float32 mono 16 kHz, o que o speech manda à Groq.

    48 kHz estéreo (VOICE_DEBUG) passa pela mesma conversão do bot
    (speech.pcm_to_float_mono_16k); o resto é misturado em mono e reamostrado
    — por média de blocos quando a taxa é múltipla de 16 kHz, como o bot.
    """
    with wave.open(caminho, "rb") as wav:
        canais, largura, taxa = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        quadros = wav.readframes(wav.getnframes())
    if largura != 2:
        raise ValueError(f"só PCM de 16 bits (este tem {8 * largura})")
    if canais == 2 and taxa == 48000:
        return speech.pcm_to_float_mono_16k(quadros)
    amostras = np.frombuffer(quadros, dtype=np.int16).astype(np.float32)
    if canais > 1:
        amostras = amostras[: len(amostras) - len(amostras) % canais].reshape(-1, canais).mean(axis=1)
    if taxa % _TAXA == 0 and taxa > _TAXA:
        bloco = taxa // _TAXA
        amostras = amostras[: len(amostras) - len(amostras) % bloco].reshape(-1, bloco).mean(axis=1)
    elif taxa != _TAXA and len(amostras):
        n = max(1, round(len(amostras) * _TAXA / taxa))
        amostras = np.interp(np.linspace(0, len(amostras) - 1, n), np.arange(len(amostras)), amostras)
    return (amostras / 32768.0).astype(np.float32)


def wavs_de(caminhos: list[str]) -> list[str]:
    """Os .wav dos caminhos: pasta vira os .wav de dentro, em ordem de nome."""
    achados = []
    for caminho in caminhos:
        if os.path.isdir(caminho):
            achados += sorted(
                os.path.join(caminho, nome) for nome in os.listdir(caminho) if nome.lower().endswith(".wav")
            )
        else:
            achados.append(caminho)
    return achados


def carrega_palavras_pt(caminhos: list[str]) -> frozenset[str]:
    """PALAVRAS_PT + as palavras (uma por linha, normalizadas) dos dicionários que existirem."""
    palavras = set(PALAVRAS_PT)
    for caminho in caminhos:
        if not os.path.isfile(caminho):
            continue
        with open(caminho, encoding="utf-8", errors="replace") as fh:
            for linha in fh:
                palavra = intents._normalize(linha.split("/")[0])  # formato hunspell: "palavra/FLAGS"
                if palavra and " " not in palavra:
                    palavras.add(palavra)
    return frozenset(palavras)


# ── ritmo ────────────────────────────────────────────────────────────────────


def espera_para(envios, agora: float, rpm: int) -> float:
    """Segundos até a próxima requisição caber em `rpm` no último minuto.

    `envios`: os instantes das requisições do modelo (speech._envios_por_modelo
    conta todas, inclusive os retries de 429/5xx)."""
    recentes = sorted(t for t in envios if t > agora - 60.0)
    if rpm <= 0 or len(recentes) < rpm:
        return 0.0
    return max(0.0, recentes[len(recentes) - rpm] + 60.0 - agora)


# ── o que o bot decidiria (puro: sem rede) ───────────────────────────────────


def veredito(texto: str, prompt: str, acao_da_1a: str | None = None) -> dict:
    """A transcrição pelos olhos do bot. `acao_da_1a`: o comando que a passada
    com o nome leu no mesmo trecho e modelo (para o mesmo_comando da 2ª escuta)."""
    palavras = intents._sem_muletas_na_frente(texto)
    grafia, nota = intents.candidato_ao_nome(texto)
    comando = intents.parse_command(texto)
    v = {
        "primeira": palavras[0] if palavras else "",
        "grafia": grafia,
        "nota": round(nota, 3),
        "inicio": intents.inicio_para_log(texto),
        "nome_ok": bool(grafia) and intents._is_wake_word(grafia),
        "de_frente": intents.chamou_de_frente(texto),
        "comando": list(comando) if comando else None,
        "nome_no_audio": None,
        "mesmo_comando": None,
    }
    if prompt == PROMPT_SEM_NOME:
        v["nome_no_audio"] = intents.nome_no_audio(texto)
        if acao_da_1a:
            v["mesmo_comando"] = intents.mesmo_comando(texto, acao_da_1a)
    return v


def _falhou(registro: dict) -> bool:
    """Com o nome no prompt: não leu comando. Sem ele: a 2ª escuta não ouviu o nome."""
    if registro["prompt"] == PROMPT_PRODUCAO:
        return registro["comando"] is None
    return not registro["nome_no_audio"]


def resumo(registros: list[dict]) -> dict:
    """Taxas por modelo e prompt, a chamada de ponta a ponta e as grafias da 1ª palavra."""
    validos = [r for r in registros if not r.get("erro")]
    taxas: dict[str, dict] = {}
    for r in validos:
        t = taxas.setdefault(f"{r['modelo']} {_ROTULO[r['prompt']]}", {"n": 0, "acertos": 0})
        t["n"] += 1
        t["acertos"] += not _falhou(r)
    # ponta a ponta, por modelo: a 1ª escuta leu um comando E a 2ª confirmou
    # (pelo nome no áudio ou pelo mesmo comando) — o que o bot atenderia
    pares: dict[tuple[str, str], dict] = {}
    for r in validos:
        pares.setdefault((r["arquivo"], r["modelo"]), {})[r["prompt"]] = r
    ponta: dict[str, dict] = {}
    for (_arquivo, modelo), par in pares.items():
        if PROMPT_PRODUCAO not in par or PROMPT_SEM_NOME not in par:
            continue
        p = ponta.setdefault(modelo, {"n": 0, "atendidas": 0})
        segunda = par[PROMPT_SEM_NOME]
        p["n"] += 1
        p["atendidas"] += par[PROMPT_PRODUCAO]["comando"] is not None and bool(
            segunda["nome_no_audio"] or segunda["mesmo_comando"]
        )
    primeiras = {
        prompt: Counter(r["primeira"] for r in validos if r["prompt"] == prompt)
        for prompt in (PROMPT_PRODUCAO, PROMPT_SEM_NOME)
    }
    nome_sumiu = sum(1 for r in validos if _falhou(r) and r["nota"] < _NOTA_MINIMA_SUGESTAO)
    return {
        "taxas": taxas,
        "ponta_a_ponta": ponta,
        "primeiras": {k: dict(v.most_common()) for k, v in primeiras.items() if v},
        "nome_sumiu": nome_sumiu,
        "erros": len(registros) - len(validos),
    }


def sugestoes(registros: list[dict], palavras_pt: frozenset[str] = PALAVRAS_PT) -> list[dict]:
    """As grafias do nome que não chamaram o bot → candidatas a _WAKE_VARIANTS.

    A grafia é a que mais lembrou o nome no começo da frase (a mesma do
    journal). Marcadas: `palavra_pt` (variação assim faria a conversa chamar o
    bot), `bloqueada` (está na _WAKE_BLOCKLIST de propósito) e `ja_aceita` (o
    parser aceita a grafia: o que falhou foi a posição dela na frase).
    """
    vistas: dict[str, dict] = {}
    for r in registros:
        if r.get("erro") or not r["grafia"] or not _falhou(r) or r["nota"] < _NOTA_MINIMA_SUGESTAO:
            continue
        s = vistas.setdefault(r["grafia"], {"grafia": r["grafia"], "nota": r["nota"], "vezes": 0, "prompts": Counter()})
        s["vezes"] += 1
        s["prompts"][r["prompt"]] += 1
    for s in vistas.values():
        s["prompts"] = dict(s["prompts"])
        s["palavra_pt"] = s["grafia"] in palavras_pt
        s["bloqueada"] = s["grafia"] in intents._WAKE_BLOCKLIST
        s["ja_aceita"] = intents._is_wake_word(s["grafia"])
    return sorted(vistas.values(), key=lambda s: (-s["vezes"], -s["nota"], s["grafia"]))


def formata_resumo(res: dict, sugs: list[dict]) -> str:
    linhas = ["", "=" * 78, "RESUMO", "=" * 78, "acerto (com nome: leu comando; sem nome: nome_no_audio):"]
    for chave, t in sorted(res["taxas"].items()):
        linhas.append(f"  {chave:38s} {t['acertos']:4d}/{t['n']:<4d} {100 * t['acertos'] / max(1, t['n']):5.1f}%")
    if res["ponta_a_ponta"]:
        linhas.append("atendidas de ponta a ponta (1ª leu comando E a 2ª confirmou):")
        for modelo, p in sorted(res["ponta_a_ponta"].items()):
            linhas.append(f"  {modelo:38s} {p['atendidas']:4d}/{p['n']:<4d} {100 * p['atendidas'] / max(1, p['n']):5.1f}%")
    for prompt, contagem in res["primeiras"].items():
        linhas.append(f"1ª palavra ({_ROTULO[prompt]}): " + ", ".join(f"{p or '-'} x{n}" for p, n in contagem.items()))
    if res["nome_sumiu"]:
        linhas.append(f"sem nada parecido com o nome (nota < {_NOTA_MINIMA_SUGESTAO}): {res['nome_sumiu']}"
                      " — o nome sumiu da transcrição; variação não resolve")
    if res["erros"]:
        linhas.append(f"transcrições que falharam (rede/cota): {res['erros']}")
    linhas.append("")
    if not sugs:
        linhas.append("SUGESTÕES: nenhuma — toda grafia parecida com o nome já chamou o bot.")
        return "\n".join(linhas)
    linhas.append(f"SUGESTÕES para _WAKE_VARIANTS (core/intents.py; WAKE_WORD={intents.WAKE_WORD!r}):")
    for s in sugs:
        avisos = []
        if s["bloqueada"]:
            avisos.append("na _WAKE_BLOCKLIST de propósito")
        elif s["palavra_pt"]:
            avisos.append("PALAVRA DO PORTUGUÊS: como variação, a conversa chamaria o bot")
        if s["ja_aceita"]:
            avisos.append("já aceita: falhou pela posição na frase")
        prompts = ", ".join(f"{_ROTULO[p]} x{n}" for p, n in sorted(s["prompts"].items()))
        aviso = f"  <- {'; '.join(avisos)}" if avisos else ""
        linhas.append(f"  {s['grafia']:18s} x{s['vezes']:<3d} nota {s['nota']:.2f}  ({prompts}){aviso}")
    boas = [s["grafia"] for s in sugs if not (s["palavra_pt"] or s["bloqueada"] or s["ja_aceita"])]
    if boas:
        linhas.append(f"candidatas limpas: {', '.join(boas)}")
    return "\n".join(linhas)


def _sim(valor) -> str:
    return "-" if valor is None else ("sim" if valor else "não")


def formata_transcricao(r: dict) -> str:
    modelo = r["modelo"].removeprefix("whisper-large-")
    cabeca = f"  {modelo:9s} {_ROTULO[r['prompt']]}  "
    if r.get("erro"):
        return cabeca + f"ERRO {r['erro']}"
    comando = "/".join(x for x in r["comando"] if x) if r["comando"] else "-"
    linha = (f"{cabeca}{r['texto']!r}\n{' ' * 22}{r['inicio']} | nome={_sim(r['nome_ok'])}"
             f" de_frente={_sim(r['de_frente'])} comando={comando}")
    if r["prompt"] == PROMPT_SEM_NOME:
        linha += f" | nome_no_audio={_sim(r['nome_no_audio'])} mesmo_comando={_sim(r['mesmo_comando'])}"
    return linha


# ── a rede ───────────────────────────────────────────────────────────────────


async def _transcreve(audio: np.ndarray, wav: bytes, modelo: str, prompt: str) -> str:
    """Pelo mesmo caminho do bot (speech._transcribe_no_modelo), num modelo fixo."""
    vocabulario = None if prompt == PROMPT_PRODUCAO else speech._prompt_sem_nome()
    return await speech._transcribe_no_modelo(audio, wav, "", modelo, vocabulario)


async def calibra(arquivos: list[str], modelos: list[str], prompts: list[str], rpm: int,
                  saida=None) -> list[dict]:
    """O laço da calibração. `saida`: arquivo aberto para o JSON Lines (um
    registro por transcrição, gravado na hora — uma sessão interrompida não se perde)."""
    registros: list[dict] = []
    esgotados: set[str] = set()
    try:
        for caminho in arquivos:
            nome = os.path.basename(caminho)
            try:
                audio = await asyncio.to_thread(audio_do_wav, caminho)
            except (OSError, EOFError, ValueError, wave.Error) as exc:
                print(f"\n== {nome}: não deu para ler ({exc})")
                continue
            duracao = len(audio) / _TAXA
            if len(audio) < _MIN_AMOSTRAS:
                print(f"\n== {nome}: {duracao:.2f}s, curto demais (o bot nem envia)")
                continue
            wav = await asyncio.to_thread(speech._to_wav_bytes, audio)
            print(f"\n== {nome} ({duracao:.1f}s)")
            acao_da_1a: dict[str, str] = {}
            for prompt in prompts:
                for modelo in modelos:
                    if modelo in esgotados:
                        continue
                    espera = espera_para(speech._envios_por_modelo.get(modelo, ()), time.monotonic(), rpm)
                    if espera > 0:
                        await asyncio.sleep(espera)
                    registro = {"arquivo": nome, "duracao_s": round(duracao, 2), "modelo": modelo,
                                "prompt": prompt, "texto": "", "erro": None}
                    try:
                        registro["texto"] = await _transcreve(audio, wav, modelo, prompt)
                    except speech._CotaDoModeloAcabou:
                        esgotados.add(modelo)
                        registro["erro"] = "a cota do dia do modelo acabou"
                    except Exception as exc:  # noqa: BLE001 — um trecho ruim não para a calibração
                        registro["erro"] = f"{type(exc).__name__}: {exc}"
                    if speech._cota_estimada(modelo) <= speech._RESERVA_DO_DIA and modelo not in esgotados:
                        esgotados.add(modelo)
                        print(f"  !! {modelo}: a cota do dia está no fim — parando de usar (o bot precisa dela)")
                    if not registro["erro"]:
                        registro.update(veredito(registro["texto"], prompt, acao_da_1a.get(modelo)))
                        if prompt == PROMPT_PRODUCAO and registro["comando"]:
                            acao_da_1a[modelo] = registro["comando"][0]
                    registros.append(registro)
                    print(formata_transcricao(registro))
                    if saida:
                        saida.write(json.dumps(registro, ensure_ascii=False) + "\n")
                        saida.flush()
    finally:
        await http.close()
    return registros


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # o console do Windows é cp1252
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("caminhos", nargs="*", help="WAVs ou pastas (padrão: data/voice-debug/)")
    parser.add_argument("--modelos", default=",".join(speech._MODELOS),
                        help="modelos da Groq, separados por vírgula (padrão: %(default)s)")
    parser.add_argument("--sem-nome-only", action="store_true",
                        help="só a passada sem o nome no prompt (a da 2ª escuta)")
    parser.add_argument("--json", metavar="SAIDA.jsonl", help="grava um registro por transcrição")
    parser.add_argument("--rpm", type=int, default=_RPM_PADRAO,
                        help="requisições por minuto por modelo (padrão: %(default)s)")
    parser.add_argument("--dicionario", action="append", default=[], metavar="ARQUIVO",
                        help="lista de palavras do português, uma por linha, para marcar as sugestões")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if not config.GROQ_API_KEY:
        print("GROQ_API_KEY vazia: ponha a chave no .env da raiz do repositório.")
        return 2
    modelos = [m.strip() for m in args.modelos.split(",") if m.strip()]
    if not modelos:
        print("--modelos vazio.")
        return 2
    arquivos = wavs_de(args.caminhos or [_DIR_PADRAO])
    if not arquivos:
        print(f"nenhum WAV em {', '.join(args.caminhos or [_DIR_PADRAO])} (grave com VOICE_DEBUG=1)")
        return 2
    prompts = [PROMPT_SEM_NOME] if args.sem_nome_only else [PROMPT_PRODUCAO, PROMPT_SEM_NOME]
    if not config.VOICE_STT_PROMPT:
        print("aviso: VOICE_STT_PROMPT vazio — as duas passadas vão sem prompt nenhum.")
    pedidos = len(arquivos) * len(prompts)
    print(f"{len(arquivos)} WAV, {len(modelos)} modelo(s): {pedidos} requisições por modelo,"
          f" no máximo {args.rpm}/min cada (~{pedidos / max(1, args.rpm):.1f} min)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as saida:
            registros = asyncio.run(calibra(arquivos, modelos, prompts, args.rpm, saida))
    else:
        registros = asyncio.run(calibra(arquivos, modelos, prompts, args.rpm))
    palavras_pt = carrega_palavras_pt([*_DICIONARIOS, *args.dicionario])
    print(formata_resumo(resumo(registros), sugestoes(registros, palavras_pt)))
    if args.json:
        print(f"\nregistros em {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
