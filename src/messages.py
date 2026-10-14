"""TODAS as mensagens do bot num lugar só — edite à vontade! 🎨

Regras de ouro:
- Mantenha os {placeholders} (ex.: {track}, {count}) — o bot preenche na hora.
- Emojis, **negrito**, *itálico* e `código` do Discord funcionam normalmente.
- Depois de editar, reinicie o bot para as mudanças valerem.
- O guia do /help fica em cogs/help.py; a bio do perfil é via API (peça ao Claude).
"""

# ── Painel do player ─────────────────────────────────────────────────────────
PANEL_IDLE_TITLE = "😴 Tá tocando nada painho"
PANEL_IDLE_SUBTITLE = "Bota um `/play` pra começar caba safado!😛"
PANEL_PLAYING = "🎧 TOCANDO AGORA"
PANEL_PAUSED = "⏸️ PAUSADO"
PANEL_REQUESTED_BY = "Pedida por {user}"
PANEL_STARTED = "começou {timestamp}"
PANEL_AUTOPLAY_TAG = "🎲 autoplay"
PANEL_UP_NEXT = "A SEGUIR ({count})"
PANEL_MORE = "… e mais {count}"
FOOTER_LOOP = "🔁 loop: {mode}"
FOOTER_AUTOPLAY = "🎲 autoplay ligado"
BTN_FAV = "Favoritar"
BTN_AUTOPLAY = "Autoplay"
BTN_LEAVE = "Sair"

# ── Comandos de música ───────────────────────────────────────────────────────
JOIN_VOICE_FIRST = "Entra num canal de voz primeiro! 🎧"
OTHER_CHANNEL = "Já estou tocando (não em você) em outro canal 😅"
ADDED = "➕ **{track}** na fila!"
# com música tocando o /play diz ONDE entrou e em quanto tempo toca ({eta} é "7 min",
# "1h23", "menos de 1 min"); sem saber a duração de alguma da frente, só a posição
ADDED_POS = "➕ **{track}** na fila — é a **{pos}ª**, toca em ~{eta}"
ADDED_POS_NO_ETA = "➕ **{track}** na fila — é a **{pos}ª**"
SEARCH_RESULTS = "🔎 Resultados para **{query}**, escolhe uma ai, caba safado!😛"
SEARCH_PLACEHOLDER = "Escolhe a música fi da mulesta…"
SEARCH_NOTHING = "Não achei nada, caba véi: `{query}` 😕"
SEARCH_NOT_YOURS = "Só quem buscou pode escolher 😉"
PAUSED = "⏸️  Tá pausado fi da mulesta 😪"
RESUMED = "▶️ Tamo de volta macho! 🤠"
SKIPPED = "⏭️ Tá pulada essa desgraça! 🙄"
SKIP_PREPARANDO = "⏳ **{track}** ainda está sendo preparada — pula quando ela começar"
SKIPPED_NEXT = "⏭️ Tá pulada essa desgraça! 🙄 Agora vem **{track}**"
SKIPPED_END = "⏭️ Tá pulada essa desgraça! 🙄 Era a última — a fila acabou."
ALREADY_PAUSED = "⏸️ Já tá pausado, painho — `/resume` volta a tocar."
NOT_PAUSED = "▶️ Já tá tocando, macho — não tinha nada pausado."
VOTE_REGISTERED = "🗳️ Voto registrado: **{votes}/{needed}** para pular."
STOPPED = "⏹️ Parado e fila limpa painho 😎"
REPLACED = "🔄 Troquei a fila: **{count}** música(s) novas na área!"
CLEARED = "🧹 Fila limpa! O que tá tocando continua."
CLEAR_EMPTY = "Fila já tá vazia painho 🤷"
NOTHING_PLAYING = "Nada tocando agora 🤷"
ALREADY_QUIET = "Já estou quieto minino arretado 😴"
SHUFFLED = "🔀 Fila embaralhada!"
SHUFFLE_EMPTY = "Fila vazia, nada para embaralhar 🤷"
LOOP_CHANGED = "🔁 Loop: **{mode}**"
LOOP_NOMES = {
    "off": "desligado",
    "track": "faixa — essa música fica repetindo",
    "queue": "fila — o que toca volta pro fim e a fila gira",
}
REMOVED = "🗑️ **{track}** removida da fila."
REMOVE_INVALID = "Não existe posição {pos} na fila 🤔"
SEEK_DONE = "⏩ Fui pra **{pos}** de {dur}, arrocha!"
SEEK_INVALID = "Não entendi esse tempo 🤔 Usa `1:30`, `90`, `+30` ou `-10`."
SEEK_FAILED = "Não consegui ir pra **{pos}** 😖 A música segue de onde tava."
SEEK_BEYOND = "Essa música só tem **{dur}** 🤷 Deixei ela onde tava."
VOLUME_SET = "🔊 Volume em **{volume}%**."
VOLUME_NOW = "🔊 O volume tá em **{volume}%** — `/volume 0 a 150` muda."
AUTOPLAY_ON = "Autoplay ta on, não posso parar -Atrain! 🎲"
AUTOPLAY_OFF = "Autoplay parado e triste 😞"
TRACK_FAILED = "⚠️ Falha ao tocar **{track}** ({error}) — pulando."
# motivo (vai no {error} acima) da faixa que o YouTube entregou e não tocou,
# nem na segunda tentativa
TRACK_DIED_EARLY = "o YouTube recusou o áudio duas vezes seguidas"
# resumo quando várias faixas seguidas falham (playlist inteira, YouTube fora)
TRACKS_FAILED = (
    "⚠️ Pulei **{count}** faixas seguidas — {error}. (ex.: {examples})"
    "\n💡 Roda `/diag` que eu digo exatamente o que caiu."
)
QUEUE_EMPTY = "Fila vazia, manda um `/play` ai misera! 🥺"
QUEUE_ONLY_RADIO = "Ninguém pediu nada — a rádio 🎲 segue sozinha. Manda um `/play` que ele fura a fila!"
QUEUE_RADIO_HEADER = "🎲 Rádio do autoplay ({count}) — toca quando a fila acabar"
URL_FAIL = (
    "Não consegui tocar esse link 😕 — se for de site com DRM (Deezer,"
    " Apple Music etc.), manda o **nome da música** ou um link do YouTube!"
)
QUEUE_REMOVE_PLACEHOLDER = "🗑️ Remover da fila…"
QUEUE_PAGE_PLACEHOLDER = "📄 Ir para as músicas…"
URL_NOT_ALLOWED = (
    "🔗 Só aceito links do YouTube (youtube.com / youtu.be) —"
    " para o resto, manda o **nome da música** que eu acho!"
)
# canal, busca, feed…: o yt-dlp sairia listando (e extraindo) tudo que há ali
URL_NOT_VIDEO = (
    "🔗 Esse link do YouTube não é de um **vídeo** nem de uma **playlist** —"
    " manda o link da música (ou o nome dela) que eu toco!"
)
YT_PLAYLIST_ADDED = "🎶 Playlist **{name}** do YouTube: {count} música(s) na fila!{note}"
YT_PLAYLIST_LIMIT_NOTE = " *(primeiras 100 — playlist grande demais pra fila)*"
YT_PLAYLIST_FAIL = (
    "Não consegui ler essa playlist do YouTube 😕 — se ela for **privada**,"
    " deixa pública (ou não listada) que eu consigo!"
)
YT_PLAYLIST_EMPTY = "Essa playlist tá vazia (ou só tem vídeo privado/apagado) 🤔"
# list=RD…: o YouTube monta na hora para quem está assistindo; não é listável
YT_PLAYLIST_MIX = (
    "🎧 Esse link é um **Mix** do YouTube (rádio que ele monta pra você) — não dá"
    " pra importar. Manda o link de uma música dele e liga o `/autoplay`, que eu"
    " sigo no mesmo clima!"
)
CTRL_SAME_CHANNEL = "🔒 Entra no canal de voz do bot para usar isso."
CTRL_NO_PERMISSION = (
    "🔒 Tem mais gente ouvindo — só quem gerencia o servidor pode fazer isso agora."
)
PLAYBACK_BROKEN = (
    "🧯 Muitas falhas seguidas ao tocar — parei e desliguei o autoplay."
    " Tenta de novo com `/play` daqui a pouco; se persistir, `/diag` mostra"
    " qual camada caiu (yt-dlp velho, cookies vencidos ou client bloqueado)."
)
GUILD_WELCOME = (
    "🎵 **Salve! Sou o Arrodes.**\n"
    "Entra num canal de voz e manda `/play <nome da música>` — também aceito"
    " link do YouTube e playlist do Spotify.\n"
    "Tem painel com botões, favoritos, autoplay e até comando por voz"
    " (`/ouvir`). O guia completo tá no `/help`, e o `/diag` diz se tem"
    " algo errado comigo."
)
AUTO_LEFT_EMPTY = "👋 Fiquei sozinho na call — saí para não gastar banda. Chama de novo com /play!"
# painel enquanto a extração fria roda (~8s na VM): sem isto parecia travado
PANEL_PREPARING_TITLE = "⏳ Preparando **{track}**…"
PANEL_PREPARING_SUBTITLE = "buscando o áudio no YouTube — uns segundos"

# ── Avisos por DM ao dono do bot (services/alerts.py) ────────────────────────
ALERT_UNCLEAN_RESTART = (
    "⚠️ **Arrodes reiniciou sem desligamento limpo** — a execução anterior"
    " começou {inicio} e morreu sem SIGTERM (crash, OOM ou queda do gateway)."
    " O systemd já subiu de novo.\n"
    "Últimos avisos/erros antes de cair:\n```\n{erros}\n```"
)
ALERT_NO_RECENT_ERRORS = "(nenhum WARNING/ERROR registrado)"
ALERT_SINTOMA = (
    "⚠️ **{o_que}** — {quantas}x na última hora.\n"
    "Último caso: {detalhe}\n"
    "Pedi o canário do yt-dlp: ele testa a produção de verdade e, se ela estiver"
    " quebrada E o nightly passar, atualiza sozinho (e avisa aqui)."
)
SINTOMA_NOMES = {
    "largada": "faixas morrendo na largada",
    "extracao": "extrações do YouTube falhando",
    "formato": "áudio vindo em formato pior (não é Opus)",
}
ALERT_EXTRACTION_BROKEN = (
    "🧯 **A extração de áudio NÃO está funcionando** ({error}).\n"
    "Toda faixa vai ser pulada até resolver. Roda `/diag` no Discord ou, na VM,"
    " `.venv/bin/python src/doctor.py`."
)
ALERT_STREAM_REFUSED = (
    "🔇 **O YouTube está recusando o áudio** — a extração funciona, mas a URL"
    " do stream respondeu `{status}` em todas as tentativas. As faixas vão ser"
    " anunciadas e não vão tocar. Suspeitos, nessa ordem: cookies velhos"
    " (`data/cookies.txt`), yt-dlp desatualizado, IP da VM marcado."
)
ALERT_COOKIES_DEAD = (
    "🍪 **Os cookies do YouTube perderam o login** — a conta foi invalidada e o"
    " yt-dlp reescreveu o `data/cookies.txt` sem a sessão. Renove com a conta"
    " descartável (ou apague o arquivo e teste sem ele)."
)
ALERT_YTDLP_OUTDATED = (
    "⬆️ Saiu o yt-dlp **{nova}** (instalado: `{instalada}`). O apagão de"
    " agosto/2026 foi versão velha: bump em `requirements.txt` + deploy."
)
COMMAND_ERROR = "💥 Deu ruim aqui do meu lado — tenta de novo! (o erro ficou registrado)"

# ── Spotify ──────────────────────────────────────────────────────────────────
SPOTIFY_ADDED = "🎶 {kind} **{name}** do Spotify: {count} música(s) na fila!{note}"
SPOTIFY_LIMIT_NOTE = " *(primeiras 100 — limite da prévia pública do Spotify)*"
SPOTIFY_PLAYLIST_FAIL = (
    "Não consegui ler essa playlist 😕 — se ela for **privada**,"
    " deixa pública que eu consigo!"
)
SPOTIFY_LINK_FAIL = "Não consegui ler esse link do Spotify, caba véi da peste 😕"
# Daily Mix, rádio, Discover Weekly: o Spotify monta pra VOCÊ e não expõe
# na API — mudar a visibilidade não adianta, então não peça isso.
SPOTIFY_ALGORITMICA = (
    "🎧 Essa é uma playlist que o Spotify monta só pra você (Daily Mix, rádio,"
    " Discover Weekly) — nem liberada a API me deixa ler 😕\n"
    "Faz assim: abre ela no Spotify, **Adicionar a uma playlist** → cria uma"
    " tua com as músicas, e me manda o link dessa!"
)
SPOTIFY_EMPTY = (
    "Não achei faixas nesse link 🤔, se for uma playlist **privada**,"
    " deixa ela pública que eu consigo ler!"
)
SPOTIFY_OFF = (
    "Integração com Spotify não configurada — preencha"
    " `SPOTIFY_CLIENT_ID` e `SPOTIFY_CLIENT_SECRET` no `.env`."
)

# ── Favoritos e histórico ────────────────────────────────────────────────────
FAV_ADDED = "⭐ **{track}** salva nos seus favoritos!"
FAV_ALREADY = "Essa já estava nos seus favoritos 😉"
FAV_REMOVED = "🗑️ **{track}** removida dos favoritos."
FAVS_EMPTY = "Você ainda não tem favoritos, usa o botão ⭐ do player!"
FAVS_HEADER = "⭐ Seus favoritos ({count}):"
FAVS_NOT_YOURS = "Esses favoritos não são seus 😉"
FAVS_PLAY_PLACEHOLDER = "▶️ Escolhe para tocar…"
FAVS_REMOVE_PLACEHOLDER = "🗑️ Escolhe para remover…"
FAVS_PLAYING = "⭐ Tua playlist de favoritos na área: **{count}** música(s)! 🎧"
FAVS_PLAYING_REPLACE = "⭐🔄 Fila trocada pela tua playlist de favoritos (**{count}** música(s))!"
HISTORY_TITLE = "🕘 Seu histórico"
HISTORY_EMPTY = "Seu histórico está vazio — manda um `/play`!"
REPLAY = "🔂 De novo: **{track}**!"

# ── Modo de voz ("Arrodes") ─────────────────────────────────────────────────
# As frases entre aspas retas daqui e do VOICE_HELLO são conferidas por
# tests/test_help.py: cada uma tem de ser um comando que a gramática entende.
VOICE_LISTENING = (
    "🎙️ **Cuidado, to escutando tudo agora👂!** Começa por **Arrodes**, espera o"
    " estalo e fala comigo bebê:\n"
    '> "toca <música>" (entra como a próxima) · "toca <música> agora" (corta) ·'
    ' "pula" · "pula 3" · "toca a terceira" · "o que tem na fila?" · "pausa" ·'
    ' "volume 50" · "sai da call"\n'
    "-# Falar só **Arrodes** me faz mandar a cola; a completa tá em `/help` →"
    " 🎙️ Voz. `/ouvir` de novo desliga."
)
VOICE_STOPPED = "🎙️ Parei de escutar a call, graças a Deus! 🙏"
VOICE_CONN_DROPPED = "Acabou antes mesmo de começar, igual aquela pessoa que dizia te amar 😖 Tenta de novo a conexão(Comigo, não a ex)."
VOICE_START_FAILED = "Não consegui começar a escutar (não tem um pau no meu ouvido) 😖 (`{error}`)"
VOICE_HEARD_PLAY = '🎙️ *"{heard}"* → **{track}** na fila!'
VOICE_HEARD_PLAY_NEXT = '🎙️ *"{heard}"* → **{track}** é a próxima! (pra ouvir já: "Arrodes, pula")'
VOICE_HEARD_PLAY_POS = '🎙️ *"{heard}"* → **{track}** entra na {pos}ª posição, atrás dos outros pedidos por voz'
VOICE_PLAY_NOW = '🎙️ *"{heard}"* → ▶️ **{track}** agora!'
VOICE_HEARD_PLAY_END = '🎙️ *"{heard}"* → **{track}** no fim da fila ({pos}ª)'
VOICE_JA_NA_FILA = "🎙️ **{track}** já está na fila ({pos}ª) 😉"
VOICE_ALREADY_PLAYING = "🎙️ **{track}** já tá tocando, macho 😅"
VOICE_NOT_FOUND = "🎙️ Não achei **{query}** 😕"
VOICE_SKIPPED = "🎙️ Tá pulada essa desgraça! 🙄 ⏭️"
VOICE_PAUSED = "🎙️ Tá pausado fi da mulesta 😪 ⏸️"
VOICE_RESUMED = "🎙️ Tamo de volta macho! 🤠 ▶️"
VOICE_SHUFFLED = "🎙️ Fila embaralhada 🔀"
VOICE_AUTOPLAY = "🎙️ Autoplay {state} 🎲"
VOICE_BYE = "🎙️ Falou(morra)! 👋"
VOICE_HELLO = (
    "🎙️ Oi, amor! Começa por **Arrodes** e fala:\n"
    '▶️ "toca <música>" (entra como a próxima) · "toca <música> agora" (corta) ·'
    ' "toca minhas favoritas"\n'
    '📜 "o que tem na fila?" · "toca a terceira" · "pula" · "pula 3" · "pula pra'
    ' quinta" · "bota a última como próxima" · "tira a segunda" · "volta uma'
    ' música" · "limpa a fila"\n'
    '🎚️ "pausa" · "continua" · "avança 30 segundos" · "minuto 2" · "segundo 30" ·'
    ' "volume 50" · "repete essa" · "tira o loop"\n'
    '💬 "que música é essa?" · "favorita essa" · "sai da call"\n'
    "-# Pediu e não tocou? Pede de novo que eu corto pra ela. A cola completa:"
    " `/help` → 🎙️ Voz"
)
VOICE_FAV_ADDED = "🎙️ ⭐ {user} favoritou **{track}**!"
VOICE_FAV_ALREADY = "🎙️ Essa já tá nos teus favoritos {user} 😉"
VOICE_FAV_WHO = "🎙️ Não consegui identificar quem falou 😅 — tenta de novo!"
VOICE_PLAY_FAVS = "🎙️ ⭐ Bora! Tua playlist de favoritos: **{count}** música(s) na fila!"
VOICE_LOOP_ON = "🎙️ Deixei essa no repeat, arrocha! 🔁"
VOICE_SKIPPED_N = "🎙️ ⏭️ Pulei **{n}** de uma vez — agora vem **{track}**"
VOICE_SKIPPED_N_END = "🎙️ ⏭️ Pulei **{n}** e a fila acabou 🤷"
VOICE_PLAY_QUEUE = "🎙️ ▶️ Puxei **{track}** (era a {pos}ª da fila) pra agora!"
VOICE_JUMPED = "🎙️ ⏭️ Fui direto pra **{track}** — {n} ficaram pelo caminho"
VOICE_JUMPED_NEXT = "🎙️ ⏭️ Fui direto pra **{track}**"
VOICE_PLAY_NEXT = "🎙️ ⏭️ **{track}** é a próxima!"
VOICE_PLAY_NEXT_NEW = "🎙️ ➕ **{track}** entra como a próxima!"
VOICE_REMOVED = "🎙️ 🗑️ Tirei **{track}** da fila."
VOICE_REMOVED_N = "🎙️ 🗑️ Tirei **{n}** músicas da fila."
VOICE_NOT_IN_QUEUE = '🎙️ Não achei **{alvo}** na fila 🤔 Diz "Arrodes, o que tem na fila?" que eu mostro.'
VOICE_NO_POSITION = "🎙️ A fila só tem **{total}** música(s) — não tem a {pos}ª 🤷"
VOICE_QUEUE_EMPTY = "🎙️ A fila tá vazia painho 🤷"
VOICE_NEXT_UP = "🎙️ A próxima é **{track}**"
VOICE_PREVIOUS = "🎙️ ⏮️ Voltei pra **{track}**"
VOICE_NO_PREVIOUS = "🎙️ Não tem música anterior pra voltar 🤷"
VOICE_SEEK_DONE = "🎙️ ⏩ Fui pra **{pos}** de {dur}!"
VOICE_SEEK_FAILED = "🎙️ Não consegui ir pra **{pos}** 😖 A música segue de onde tava."
VOICE_SEEK_BEYOND = "🎙️ Essa música só tem **{dur}** 🤷 Deixei ela onde tava."
VOICE_VOLUME_SET = "🎙️ 🔊 Volume em **{volume}%**."
VOICE_LOOP_OFF = "🎙️ Loop desligado, segue o baile ➡️"
VOICE_QUEUE_CLEARED = "🎙️ 🧹 Fila limpa! O que tá tocando continua."
VOICE_NOW_PLAYING = "🎙️ Tá tocando: **{track}** 🎧"
VOICE_UNKNOWN = '🎙️ Entendi(eu acho) *"{heard}"*, mas não conheço esse comando que tu fez 🤔'
