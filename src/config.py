"""Configuração via variáveis de ambiente (.env na raiz do projeto)."""
import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/bot.db").strip()
# token de usuário do Spotify (criado por `python src/spotify_login.py`) —
# necessário para importar PLAYLISTS (o Spotify exige login de usuário)
SPOTIFY_TOKEN_CACHE = os.getenv("SPOTIFY_TOKEN_CACHE", "data/spotify-token.json").strip()
# tiny = rápido (recomendado p/ comandos de voz); base/small = mais precisos e lentos
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny").strip()
# opcional: chave do Groq (console.groq.com) — transcrição na nuvem com
# whisper-large-v3-turbo: muito melhor com pt+inglês misturado, ~1s, grátis
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# modelo do LLM que interpreta comandos de voz livres. A Groq APOSENTA
# modelos sem aviso (o llama-3.3 morreu e o bot ficou semanas sem
# interpretador, calado). Vazio = o default de services/llm.py; preencha
# para trocar sem deploy quando o 404 aparecer no log.
GROQ_LLM_MODEL = os.getenv("GROQ_LLM_MODEL", "").strip()
# cookies do YouTube (formato Netscape) — necessário em IPs de datacenter,
# onde o YouTube exige login ("Sign in to confirm you're not a bot").
# Use uma conta DESCARTÁVEL, nunca a pessoal (risco de bloqueio da conta).
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "data/cookies.txt").strip()
# ordem dos perfis de extração (services/youtube.py: padrao, cookies,
# cookies_embedded, movel). Vazio = cadeia completa na ordem padrão, com o
# bot elegendo sozinho o que funciona. Preencha só para fixar um vencedor
# conhecido e economizar a tentativa perdida do primeiro /play após restart.
YTDLP_PROFILES = os.getenv("YTDLP_PROFILES", "").strip()
# runtimes de JS que o yt-dlp pode usar para resolver os desafios do YouTube
# (EJS/nsig). SEM nenhum runtime disponível ele cai num conjunto reduzido de
# clients e o áudio despenca para o formato 18 (mp4 com vídeo) ou some.
# Formato: "deno,node" ou "deno:/opt/deno/bin/deno,node". Ordem de prioridade
# é a do yt-dlp (deno > node > quickjs > bun), não a daqui.
YTDLP_JS_RUNTIMES = os.getenv("YTDLP_JS_RUNTIMES", "deno,node,bun,quickjs").strip()
# extrações simultâneas. Vazio = automático (metade dos núcleos, teto 4).
# Extrair é CPU-bound (desafio JS do player do YouTube): passar dos núcleos
# derruba latência E vazão — medido na VM, 4 simultâneas = 66s por faixa
# contra 8s serializado. Preencha só para forçar um valor.
YTDLP_WORKERS = os.getenv("YTDLP_WORKERS", "").strip()
# onde guardar o cache de URLs de stream entre reinicios. As URLs do
# googlevideo valem ~6h e o cache 3h; sem isto todo deploy jogava as
# quentes fora e a proxima faixa pagava ~8,3s de extracao de novo.
STREAM_CACHE_PATH = os.getenv("STREAM_CACHE_PATH", "data/stream-cache.json").strip()
# marcador de execução (detecta reinício sem desligamento limpo) e espelho
# dos últimos WARNING/ERROR — é o que o bot manda por DM ao dono quando
# sobe depois de uma morte (services/alerts.py)
RUN_STATE_PATH = os.getenv("RUN_STATE_PATH", "data/run-state.json").strip()
RECENT_ERRORS_PATH = os.getenv("RECENT_ERRORS_PATH", "data/recent-errors.log").strip()
# canal de texto para os avisos quando a DM ao dono é recusada (403: o dono
# não aceita DM do bot). Vazio = canal de sistema do servidor.
ALERT_CHANNEL_ID = os.getenv("ALERT_CHANNEL_ID", "").strip()
# caixa de avisos: o que roda FORA do bot (o canário do yt-dlp, um shell script)
# deixa um .txt aqui e o bot entrega ao dono. E o caminho inverso: criando o
# arquivo de pedido, o bot acorda o canário (systemd .path) quando os sintomas
# de "o YouTube mudou" passam do limiar.
AVISOS_DIR = os.getenv("AVISOS_DIR", "data/avisos").strip()
CANARIO_PEDIDO_PATH = os.getenv("CANARIO_PEDIDO_PATH", "data/canario-pedido").strip()
# diagnóstico do modo de voz: 1 = grava WAVs dos trechos em data/voice-debug
# e imprime contadores/decisões por segmento. Custa I/O por fala — deixe
# desligado em produção.
VOICE_DEBUG = os.getenv("VOICE_DEBUG", "").strip().lower() in ("1", "true", "yes")
# 1 = guarda em data/wake-amostras/ (WAV 16 kHz mono, teto de 300 arquivos) os
# trechos em que a Groq CONFIRMOU a wake word — fala dirigida ao bot. É o
# conjunto de teste para avaliar um wake word local. Vem desligado: é áudio
# de pessoas em disco.
VOICE_COLETA = os.getenv("VOICE_COLETA", "").strip().lower() in ("1", "true", "yes")


def _env_liga(name: str) -> bool:
    """Chave que vem LIGADA: só "0", "false", "nao"/"não" ou "off" desligam."""
    return os.getenv(name, "").strip().lower() not in ("0", "false", "nao", "não", "off", "no")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# segundos de áudio adiantados na memória entre o FFmpeg e o Discord. É o
# pulmão que impede a música de "cortar pedaços" quando o CDN do YouTube
# derruba a conexão (a cada ~31s, medido) ou quando a CPU satura. Custa
# ~190 KB por segundo, por servidor tocando.
AUDIO_BUFFER_S = _env_float("AUDIO_BUFFER_S", 8.0)
# 1 = o Opus do YouTube vai para o Discord SEM decodificar nem recodificar
# (core/audio.py::FonteOpus). Medido na VM em 20/09/2026: 118 → 17 ms de CPU por
# segundo de áudio. Muda duas coisas que se OUVEM — o volume passa a ser o
# original do YouTube (era 0,8) e o estalo de escuta interrompe a música ~0,7s
# em vez de tocar por cima —, por isso vem desligada: liga-se no .env da VM e
# só vira o padrão depois do teste de ouvido.
AUDIO_OPUS_DIRETO = os.getenv("AUDIO_OPUS_DIRETO", "").strip().lower() in ("1", "true", "yes")

# segundos sem pacotes de áudio até considerar que a pessoa parou de falar.
# Era 1,2s: com o Groq em 0,3–0,7s, esse corte era a maior parte da espera
# de todo comando de voz. 0,8s é o padrão de assistentes de voz; frase
# partida ao meio é coberta pela janela de continuação (core/listen.py).
VOICE_GAP_S = _env_float("VOICE_GAP_S", 0.8)
# energia mínima (RMS por janela de 20ms) para um trecho contar como fala
VOICE_MIN_RMS = _env_float("VOICE_MIN_RMS", 250.0)
# vocabulário passado ao Whisper do Groq como bias ("prompt") — ajuda a
# transcrever o nome e os comandos certos. Abre com o nome (o WAKE_WORD de
# core/intents.py), que a 2ª escuta tira do prompt para conferir se ele foi
# dito mesmo (services/speech.py). Vazio no .env desliga
VOICE_STT_PROMPT = os.getenv(
    "VOICE_STT_PROMPT",
    "Arrodes, toca, pula, pausa, continua, sai da call, embaralha,"
    " próxima, repete, tira o loop, limpa a fila, que música é essa,"
    " avança 30 segundos, volta 10 segundos, volume 50, aumenta o volume,"
    " pula três músicas, toca a terceira da fila, pula pra quinta, tira a última,"
    " o que tem na fila, volta uma música.",
).strip()
# estalo de "estou ouvindo" quando alguém chama o bot no /ouvir (como a
# Alexa). VOICE_SOM_ESCUTA=0 desliga.
VOICE_SOM_ESCUTA = _env_liga("VOICE_SOM_ESCUTA")
# teto de requisições por minuto do Whisper na Groq (tier grátis: 20). Perto
# dele o bot para de enviar os trechos compridos — que nunca são comando — e
# guarda o minuto para os curtos (services/speech.py). 0 desliga o controle.
GROQ_STT_RPM = int(_env_float("GROQ_STT_RPM", 20))
# duração máxima (s) de um trecho de fala enviado à Groq. Medido em cinco dias
# de journal: nenhum dos 80 comandos passou de 5,6s; o que passava de 6s eram
# 13% das requisições e 37% do áudio. 0 desliga. Sem Groq o teto não vale.
VOICE_TETO_S = _env_float("VOICE_TETO_S", 6.0)
# fallback para o Whisper LOCAL quando a Groq falha. VOICE_WHISPER_LOCAL=0
# desliga: na VM de produção ele leva de 83 a 131s por frase (medido duas
# vezes) e só carregar o modelo são ~46s de CPU cheia com a música tocando.
# Sem chave da Groq o local é o único motor e esta chave é ignorada.
VOICE_WHISPER_LOCAL = _env_liga("VOICE_WHISPER_LOCAL")


def spotify_enabled() -> bool:
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)
