"""Interpretação dos comandos falados: "Arrodes, toca tal música".

Lógica pura (texto → intenção), sem dependência de Discord ou áudio.

"Arrodes" não é palavra do português, e os modelos de STT o transcrevem com
o nome conhecido mais perto — "Herodes", "A Rodes", "Harrods" — e embolam
verbos ("toca" → "com toque"). Por isso: lista de variações + fuzzy
exigente; a wake word é procurada nas primeiras palavras da frase; ecos
repetidos são removidos; e os verbos têm tolerância de posição e de grafia.

Palavras de verdade perto do nome (arrotes, arrozes, arrodeia) não chamam o
bot: com o nome no prompt da Groq o Whisper tende a escrevê-lo certo, e regra
de prefixo ou fuzzy frouxo só servem para a conversa da call disparar o bot.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

WAKE_WORD = "arrodes"  # minúsculo: a comparação é depois do _normalize
# grafias do nome + vizinhas prováveis. "herodes" (0,71), "arodis", "erodes" e
# "rhodes" (0,77) não passam no fuzzy: só entram por aqui
_WAKE_VARIANTS = {
    "arrodes", "arodes", "arrodis", "arodis", "harrodes", "harodes", "herodes",
    "harrods", "rhodes", "aroldes", "haroldes", "erodes", "arrods", "arrode",
}
# palavras pt de verdade acima do _WAKE_THRESHOLD (arrotes, arrozes, arrobes,
# arrojes e carrões, 0,86; ardes, 0,83; arredondes, 0,82) e, por garantia, o
# verbo "arrodear" (0,80), a um erro de grafia do limiar
_WAKE_BLOCKLIST = {
    "arrotes", "arrozes", "arrobes", "arrojes", "carroes", "ardes", "arredondes",
    "arrodeia", "arrodeio", "arrodear", "arrodeou", "arrodeando",
}
# "A Rodes" funciona porque o "a" sai como muleta e "rodes" dá 0,833: acima
# disso ele quebra. Abaixo, passa conversa: a 0,75 já entrariam "arredores"
# (0,75) e "a rodas" colado (0,77)
_WAKE_THRESHOLD = 0.82
# 2ª escuta (sem o nome no prompt): aqui vale o nome MAL escrito, que é como o
# Whisper o escreve sem o prompt. A 0,7 confirmavam "acordes", "rode o vídeo",
# "a rodas", "rodeios" e "arredores".
_NOME_NO_AUDIO_MIN = 0.8
# pedaços do nome numa palavra só ("Herodes" é 0,71). Nada de "arrod"/"rrod":
# com as vizinhas coladas, "jogar rodada" vira "jogarrodada"
_NOME_NO_AUDIO_PEDACOS = ("rodes", "rodis", "herod")
_NOME_NO_AUDIO_PALAVRAS = 4
_FUZZY_THRESHOLD = 0.78
_WAKE_SCAN_WORDS = 3  # wake word procurada nas primeiras N palavras

_FILLERS = {
    "o", "a", "e", "ai", "com", "ei", "hey", "oi", "meu", "fala", "opa",
    "alo", "ola", "ok", "eae", "eai", "entao", "ta",
}

_PLAY_VERBS = (
    "toca", "toque", "tocar", "coloca", "coloque", "colocar", "bota",
    "bote", "botar", "poe", "manda", "mande", "joga", "jogue",
)
_SKIP_WORDS = ("pula", "pule", "pular", "proxima", "proximo", "passa", "passe", "next", "skip", "skipa", "skipar")
# "troca"/"muda" sem alvo = skip; com "pra/para/por <alvo>" = tocar o alvo
_SWAP_WORDS = ("troca", "troque", "muda", "mude")
# só valem como skip em frase curta ("outra", "avança aí")
_SKIP_SHORT_WORDS = ("outra", "avanca", "avance")
_RESUME_WORDS = (
    "volta", "volte", "continua", "continue", "continuar", "despausa",
    "despause", "retoma", "retome", "solta", "segue",
)
_SHUFFLE_WORDS = ("embaralha", "embaralhar", "aleatorio", "aleatoria", "mistura", "misturar", "shuffle")
_PAUSE_PHRASES = (
    "para", "pare", "para a musica", "pare a musica", "para musica",
    "espera", "espera ai", "perai", "segura", "segura ai", "calma",
    "calma ai", "quieto", "silencio",
)
_LEAVE_PHRASES = (
    "sai da call", "sai da chamada", "sai do canal", "sai dai", "sai fora",
    "cai fora", "para tudo", "vai embora", "some daqui",
)
_LEAVE_WORDS = ("desliga", "desconecta", "tchau", "xau", "vaza", "fui", "encerra", "embora")
# "desliga A RÁDIO" / "liga a rádio" é autoplay, não sair — sem isto,
# "desliga" na primeira posição caía em _LEAVE_WORDS (12/09: o bot saiu da
# call quando o Otavio pediu para desligar o autoplay)
_TOGGLE_WORDS = (
    "liga", "ligue", "ligar", "desliga", "desligue", "desligar", "ativa", "ative",
    "desativa", "desative", "tira", "tire", "para", "pare", "bota", "bote",
)
_LOOP_OFF_PHRASES = (
    "tira o loop", "tira loop", "tira do loop", "sem loop", "desliga o loop",
    "para de repetir", "chega de repetir", "para o loop",
    "tira o repeat", "desliga o repeat", "sem repeat", "para o repeat",
)
_LOOP_WORDS = ("repete", "repita", "repetir")
_CLEAR_PHRASES = (
    "limpa a fila", "limpa fila", "limpar a fila", "apaga a fila",
    "apaga tudo", "limpa tudo", "esvazia a fila", "zera a fila",
    "tira tudo", "remove tudo", "tira todas", "remove todas", "exclui tudo", "retira tudo",
)
_NOW_PLAYING_PHRASES = (
    "que musica", "qual musica", "ta tocando", "esta tocando", "que som",
    "nome dessa", "nome desta", "nome da musica",
)
# Seek e volume por voz. Os verbos são os mesmos dos comandos antigos ("volta" =
# despausar, "pula"/"avança aí" = próxima faixa), então seek só dispara com
# NÚMERO + unidade de tempo e volume só com a palavra "volume"/"som". Match
# EXATO, sem fuzzy: "pausa" e "passa" são vizinhos (0,80) e "pausa 10 segundos"
# viraria um avanço.
_SEEK_FRENTE = (
    "avanca", "avance", "avancar", "adianta", "adiante", "adiantar",
    "pula", "pule", "pular", "passa", "passe",
)
_SEEK_TRAS = (
    "volta", "volte", "voltar", "retrocede", "retroceda", "retroceder",
    "recua", "recue", "rebobina", "rebobine",
)
_SEEK_IR = ("vai", "va", "ir", "bota", "bote", "coloca", "coloque", "poe", "leva", "leve")
_SEEK_RECOMECA = ("recomeca", "recomece", "recomecar", "reinicia", "reinicie", "reiniciar")
# "vai PARA 2 minutos" é posição; "avança 2 minutos" é deslocamento
_PARA_ONDE = ("para", "pra", "pro", "ao", "aos", "ate")
_VOL_SOBE = ("aumenta", "aumente", "aumentar", "sobe", "suba", "subir", "levanta", "levante")
_VOL_DESCE = (
    "abaixa", "abaixe", "abaixar", "baixa", "baixe", "baixar",
    "diminui", "diminua", "diminuir", "reduz", "reduza", "reduzir",
)
_VOL_POE = (
    "volume", "bota", "bote", "coloca", "coloque", "poe", "deixa", "deixe",
    "muda", "mude", "ajusta", "ajuste",
)
_PASSO_VOLUME = 20  # "aumenta o volume" sem número
_NUMEROS = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19,
}
_DEZENAS = {
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
    "setenta": 70, "oitenta": 80, "noventa": 90,
}
_UNIDADES_S = {
    "segundo": 1, "segundos": 1, "seg": 1, "segs": 1,
    "minuto": 60, "minutos": 60, "min": 60, "mins": 60,
}
_POSICAO_RELOGIO = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2})$")
_POSICAO_UNIDADES = re.compile(r"^(?:(\d+)h)?(?:(\d+)m(?:in)?)?(?:(\d+)s?)?$")
# "volume máximo" foi para o LLM no teste de 20/09 (0,8s a mais e depende dele)
_VOLUME_PALAVRAS = {
    "maximo": "100", "talo": "100", "normal": "100", "original": "100", "cheio": "100",
    "metade": "50", "meio": "50", "minimo": "10", "baixinho": "10", "mudo": "0", "zero": "0",
}

# ── a fila por voz ───────────────────────────────────────────────────────────
# As posições são as do painel: 1 = a próxima a tocar. O alvo sai como "#N"
# ("#-1" = a última, "#1-3" = da 1 à 3) ou como o NOME dito — quem casa o nome
# com a fila é quem executa (acha_na_fila), porque só ele vê a fila.
_ORDINAIS = {
    "primeira": 1, "primeiro": 1, "segunda": 2, "segundo": 2, "terceira": 3, "terceiro": 3,
    "quarta": 4, "quarto": 4, "quinta": 5, "quinto": 5, "sexta": 6, "sexto": 6,
    "setima": 7, "setimo": 7, "oitava": 8, "oitavo": 8, "nona": 9, "nono": 9,
    "decima": 10, "decimo": 10, "vigesima": 20, "vigesimo": 20,
}
_DO_FIM = {
    "ultima": -1, "ultimo": -1, "penultima": -2, "penultimo": -2,
    "antepenultima": -3, "antepenultimo": -3,
}
_ARTIGOS = ("a", "o", "as", "os", "aquela", "aquele", "essa", "esse")
_ROTULOS_DE_FAIXA = ("musica", "musicas", "faixa", "faixas", "som", "sons", "cancao")
# depois da posição só pode vir isto — senão "toca a quinta sinfonia de
# beethoven" viraria "a quinta da fila"
_SOBRA_DE_FILA = frozenset({
    "da", "na", "do", "no", "de", "fila", "lista", "playlist", "agora", "ai", "ja", "por",
    "favor", "pra", "para", "tocar", "que", "ta", "esta", "musica", "faixa",
})
_FILA_SUFIXOS = (
    ("que", "ta", "na", "fila"), ("que", "esta", "na", "fila"), ("que", "tem", "na", "fila"),
    ("da", "fila"), ("na", "fila"), ("da", "lista"), ("na", "lista"),
    ("da", "playlist"), ("na", "playlist"),
)
_MARCAS_DE_PROXIMA = (
    ("como", "a", "proxima"), ("como", "proxima"), ("como", "a", "seguinte"), ("como", "seguinte"),
    ("de", "proxima"), ("pra", "proxima"), ("para", "proxima"),
    # o Whisper ouve "como a" como "com a" (27/09: "coloca X com a próxima música")
    ("com", "a", "proxima"), ("com", "o", "proximo"), ("como", "o", "proximo"),
    # 06/10: "tocar com uma próxima música..." foi para a busca e tocou outra
    ("com", "uma", "proxima"), ("como", "uma", "proxima"),
    ("em", "seguida"), ("logo", "depois"), ("depois", "dessa"), ("depois", "desta"),
    ("depois", "disso"), ("depois",),
)
# as mesmas marcas ditas ANTES do nome: "coloca como próxima música X". Em 27/09
# a frase inteira ia para a busca — "toca como próxima música…" tocou "A próxima
# vítima", do Wando
_PROXIMA_NA_FRENTE = (
    ("como", "a", "proxima"), ("como", "proxima"), ("com", "a", "proxima"),
    ("com", "uma", "proxima"), ("como", "uma", "proxima"),
    # 25/09: "coloca a próxima música como amigo da minha saudade"
    ("a", "proxima", "musica", "como"), ("a", "proxima", "como"), ("proxima", "musica", "como"),
)
# "coloca na fila X": o "na fila" também ia para a busca
_NA_FILA_NA_FRENTE = (("na", "fila"), ("na", "lista"), ("na", "playlist"), ("pra", "fila"), ("a", "fila"))
# "coloca X NA FILA" é ADICIONAR — antes virava tocar X da fila, cortando a atual.
# Só com verbo de pôr: "toca X que tá na fila" segue sendo tocar da fila.
_VERBOS_DE_POR = (
    "coloca", "coloque", "colocar", "bota", "bote", "botar", "poe", "manda", "mande", "joga", "jogue",
)
_NO_FIM_DA_FILA = (
    ("no", "final", "da", "fila"), ("no", "fim", "da", "fila"), ("no", "final", "da", "lista"),
    ("no", "final"), ("no", "fim"), ("na", "fila"), ("na", "lista"), ("na", "playlist"),
)
# "na frente", "no começo": só valem com "da fila" na frase ou com o alvo sendo uma
# POSIÇÃO ("bota a terceira na frente") — senão "toca Bola pra Frente" virava
# "bota 'bola' como próxima"
_MARCAS_DE_FRENTE = (
    ("em", "primeiro", "lugar"), ("em", "primeiro"), ("na", "frente"), ("pra", "frente"),
    ("no", "comeco"), ("pro", "comeco"), ("no", "inicio"), ("pro", "inicio"),
    ("no", "topo"), ("pro", "topo"),
)
# "quero ouvir X", "eu queria escutar X", "quero que toque X" = "toca X"
_QUERER = ("quero", "queria", "queremos", "gostaria", "preciso")
_OUVIR = ("ouvir", "escutar", "curtir")
_QUE_TOQUE = ("toque", "toca", "toques", "bote", "coloque")
_PULAR_VERBOS = ("pula", "pule", "pular", "passa", "passe", "salta", "salte", "skipa")
_AVANCAR_VERBOS = ("avanca", "avance", "avancar", "adianta", "adiante")
_IR_PARA_VERBOS = _PULAR_VERBOS + _AVANCAR_VERBOS + ("vai", "va", "ir")
_PARA_A_FAIXA = ("pra", "para", "pro", "ate", "ao")
_TIRAR_VERBOS = (
    "tira", "tire", "tirar", "remove", "remova", "remover", "apaga", "apague", "apagar",
    "deleta", "delete", "deletar", "exclui", "exclua", "retira", "retire",
)
# "tira a RÁDIO", "tira o LOOP", "tira o VOLUME" não são tirar da fila
_NAO_E_FILA = frozenset({"radio", "autoplay", "loop", "volume", "som"})
_PERGUNTAS = (
    "mostra", "mostre", "mostrar", "lista", "liste", "ve", "ver", "qual", "quais", "que",
    "quantas", "quantos", "cade", "como", "tem", "fila", "diz", "fala", "me",
)
# sem rótulo ("músicas"), número pequeno depois de "pula" é faixa; grande é
# segundo — ninguém pula 30 músicas por voz, e "avança 30" pulava a FAIXA
_PULAR_FAIXAS_ATE = 9
# o nome dito COLADO contra o da faixa colado (ver acha_na_fila)
_COLADO_MINIMO = 0.85
_COLADO_TAMANHO = 8
_NOME_IGNORA = frozenset({
    "a", "o", "as", "os", "da", "do", "de", "das", "dos", "e", "musica", "faixa", "aquela",
    "aquele", "essa", "esse", "que", "ta", "na", "fila", "feat", "ft",
})
_ENTRE_PARENTESES = re.compile(r"[\(\[].*?[\)\]]")  # "(2013 Remaster)", "[feat. X]"
_VOLUME_PEDIDO = re.compile(r"^([+-]?)(\d{1,3})%?$")
# acima disto o filtro `volume` do FFmpeg já clipa a maioria das músicas, e um
# limitador custaria a CPU que o Opus direto devolveu
VOLUME_MAX = 150
# o seek para antes do fim: passar do fim é o que o /skip faz, e um ffmpeg
# aberto já em EOF o player leria como "morreu na largada"
_FOLGA_DO_FIM_S = 5
# O que o Whisper devolve sobre ruído (respiração, clique, tosse). No journal
# de 30 dias, "Tchau." sozinho apareceu 36 vezes sem ninguém chamar o bot; no
# Groq, um estalo sintético vira "E aí". Texto igual ao de quem fala de verdade.
_ESSA_AQUI = (
    ["essa"], ["essa", "musica"], ["esta"], ["esta", "musica"], ["essa", "ai"], ["isso"], ["isso", "ai"],
)
_DA_PLAY = frozenset({
    "da", "de", "o", "a", "um", "no", "volta", "liga", "ligou", "aperta", "solta", "ai", "teu", "seu",
})
_ESPERA_WORDS = ("calma", "espera", "perai", "segura")
_SUJEITOS = frozenset({"vou", "vai", "vamos", "eu", "tu", "ele", "ela", "nos", "voce", "vc", "a", "gente"})
_STT_NOISE = {"", "tchau", "tchau tchau", "obrigado", "obrigada", "valeu", "e ai"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]+", " ", text).strip()
    # o Whisper escreve "auto play"/"auto-play": uma palavra só para o parser
    return re.sub(r"\bauto\s+play\b", "autoplay", text)


# pedir música em voz alta tem verbo: "toca X", "põe X", "quero ouvir X". Sem
# nenhum destes, um "play" do LLM era conversa (26/09: "Brincadeira, agora sim"
# → tocou "Brincadeira"; "Arrodes, Eita" → tocou "Eita")
_PEDIDO_WORDS = frozenset(_PLAY_VERBS) | {
    "tocca", "toka", "poem", "ponha", "ponhe", "boto", "coloco", "toco",
    "ouvir", "escutar", "play", "adiciona", "adicione", "solta", "solte",
    "roda", "rode", "troca", "troque", "muda", "mude",
    "adicionar", "acrescenta", "acrescente", "acrescentar", "inclui", "inclua", "incluir",
    "ponha", "reproduz", "reproduza", "reproduzir", "coloque",
}


# ── muletas e sinônimos (journal de 21 a 28/09) ─────────────────────────────
# O jeito de falar muda, o pedido é o mesmo: "adicionar", "retirar", "pode
# pular", "vou adicionar a fila…" falharam de verdade. E "pode" é 0,86 de "poe"
# no fuzzy: "pode pular" virava tocar uma música chamada "pular".
_SINONIMOS = {
    **dict.fromkeys((
        "adiciona", "adicione", "adicionar", "acrescenta", "acrescente", "acrescentar",
        "inclui", "inclua", "incluir", "ponha", "ponhe", "poem", "reproduz", "reproduza",
        "reproduzir", "coloque",
    ), "coloca"),  # "roda" ficou de fora: "roda de conversa" viraria pedido
    "retirar": "retira", "excluir": "exclui", "voltou": "volta", "despausar": "despausa",
}
_MULETAS = (
    ("por", "favor"), ("voce", "pode"), ("voce", "poderia"), ("voce", "consegue"), ("vc", "pode"),
    ("da", "pra"), ("tem", "como"), ("pode",), ("poderia",), ("consegue",), ("bora",),
)
# depois do verbo de tocar: "bota PRA TOCAR X", "toca PRA MIM X", "toca AÍ X"
_ENCHIMENTO_DO_TOCAR = (
    ("pra", "tocar"), ("para", "tocar"), ("pra", "mim"), ("para", "mim"), ("pra", "gente"),
    ("pra", "nos"), ("ai",), ("aqui",),
)


def _abre_com(words: list[str], sequencias) -> int:
    """Quantas palavras a primeira sequência que abre `words` ocupa (0 = nenhuma)."""
    for seq in sequencias:
        if len(words) > len(seq) and tuple(words[: len(seq)]) == seq:
            return len(seq)
    return 0


def _sem_muletas(rest: list[str]) -> list[str]:
    """ "por favor, você pode adicionar aí X" → "coloca X"."""
    while True:
        n = _abre_com(rest, _MULETAS)
        if n == 0 and rest[:1] and rest[0] in ("vou", "vai", "vamos") and len(rest) > 1:
            seguinte = _SINONIMOS.get(rest[1], rest[1])
            if seguinte in _PLAY_VERBS or seguinte in _TIRAR_VERBOS:  # "vai PRA quinta" fica
                n = 1
        if n == 0:
            break
        rest = rest[n:]
    # o nome repetido no fim sai só como o prompt o escreve: "Herodes", "Rhodes"
    # e "Harrods" também são o que a pessoa pediu ("toca Rhodes")
    while len(rest) > 1 and (rest[-2:] == ["por", "favor"] or rest[-1] == WAKE_WORD):
        rest = rest[:-2] if rest[-2:] == ["por", "favor"] else rest[:-1]
    if not rest:
        return rest
    rest = [_SINONIMOS.get(rest[0], rest[0])] + rest[1:]
    if rest[0] in _PLAY_VERBS:
        while True:
            n = _abre_com(rest[1:], _ENCHIMENTO_DO_TOCAR)
            if n == 0:
                break
            rest = rest[:1] + rest[1 + n :]
    return rest


def tem_pedido_de_tocar(text: str) -> bool:
    """A frase tem um verbo de pedir música? Exato: fuzzy aqui pegaria "boa"→"bota"."""
    return not _PEDIDO_WORDS.isdisjoint(_normalize(text).split())


def first_word(text: str) -> str:
    """Primeira palavra normalizada — o que a pessoa tentou usar como wake
    word. É só isso que vai para o log quando nada é reconhecido (nunca o
    conteúdo da fala)."""
    words = _normalize(text).split()
    return words[0] if words else ""


# no log, a grafia que mais lembrou o nome só aparece acima disto: abaixo é uma
# palavra qualquer da conversa, e conversa não vai para o journal
_LOG_CANDIDATO_MIN = 0.6


def _sem_muletas_na_frente(text: str) -> list[str]:
    """As palavras como o parser as vê: sem o eco do prompt e sem as muletas da frente."""
    words = _sem_eco_do_prompt(_normalize(text).split())
    while words and words[0] in _FILLERS:
        words.pop(0)
    return words


def candidato_ao_nome(text: str) -> tuple[str, float]:
    """A grafia que mais lembra o nome entre as palavras onde o parser o procura
    (as primeiras e os pares colados) e a nota dela contra WAKE_WORD."""
    words = _sem_muletas_na_frente(text)
    melhor, nota = "", 0.0
    for i in range(min(_WAKE_SCAN_WORDS, len(words))):
        candidatos = [words[i]] + ([words[i] + words[i + 1]] if i + 1 < len(words) else [])
        for candidato in candidatos:
            ratio = difflib.SequenceMatcher(None, candidato, WAKE_WORD).ratio()
            if ratio > nota:
                melhor, nota = candidato, ratio
    return melhor, nota


def inicio_para_log(text: str) -> str:
    """O começo de uma frase sem wake word, para o journal: a 1ª palavra (depois
    das muletas) e a grafia que mais lembrou o nome, com a nota — "rode→'rode'
    0.73", "hero→'herodes' 0.71". É o que mostra como o Whisper escreveu o nome
    nas chamadas perdidas. Duas palavras já seriam a frase inteira de um "tchau
    pessoal": o resto só entra como grafia, e só se lembrar o nome."""
    words = _sem_muletas_na_frente(text)
    if not words:
        return "-"
    melhor, nota = candidato_ao_nome(text)
    if nota < _LOG_CANDIDATO_MIN:
        return f"{words[0]} {nota:.2f}"
    return f"{words[0]}→'{melhor}' {nota:.2f}"


def is_stt_noise(text: str) -> bool:
    """A transcrição INTEIRA é uma das frases que o Whisper inventa sobre ruído?"""
    return " ".join(_normalize(text).split()) in _STT_NOISE


def _is_wake_word(word: str) -> bool:
    if word in _WAKE_BLOCKLIST:
        return False
    if word in _WAKE_VARIANTS:
        return True
    return difflib.SequenceMatcher(None, word, WAKE_WORD).ratio() >= _WAKE_THRESHOLD


def _lembra_o_nome(word: str, *, colada: bool = False) -> bool:
    if word in _WAKE_BLOCKLIST:
        return False
    if word in _WAKE_VARIANTS:  # o que chama na 1ª escuta confirma na 2ª
        return True
    # pedaço só numa palavra inteira: colada com a vizinha, "quero descansar"
    # e "pro desafio" também têm "rodes"
    if not colada and any(pedaco in word for pedaco in _NOME_NO_AUDIO_PEDACOS):
        return True
    return difflib.SequenceMatcher(None, word, WAKE_WORD).ratio() >= _NOME_NO_AUDIO_MIN


def nome_no_audio(text: str) -> bool:
    """A 2ª escuta (feita SEM o nome no prompt) ouviu algo parecido com o nome?

    Com "Arrodes," abrindo o prompt, o Whisper abre a transcrição com o nome
    sobre qualquer coisa: "É isso." pode sair "Arrodes.", e letra de música
    vazando no microfone de alguém, "Arrodes, I'm going to ask you".
    """
    words = _normalize(text).split()[:_NOME_NO_AUDIO_PALAVRAS]
    for i, word in enumerate(words):
        if _lembra_o_nome(word):
            return True
        if i + 1 < len(words):
            par = word + words[i + 1]
            if par in _WAKE_VARIANTS or (
                _par_do_tamanho_do_nome(word, words[i + 1]) and _lembra_o_nome(par, colada=True)
            ):
                return True
    return False


# o nome sozinho e a frase que só o LLM leria são os falsos típicos do prompt
# (ele põe o nome na frente de qualquer coisa): para eles, só o nome no áudio
# confirma a chamada
_SEM_CONFIRMACAO_PELO_COMANDO = frozenset({"unknown"})


def mesmo_comando(segunda: str, acao: str) -> bool:
    """A 2ª escuta pede a MESMA ação específica que a 1ª leu?

    Sem o nome no prompt o Whisper pode escrever "Arrodes" como uma palavra
    qualquer ("Arroz", "Haroldo") — barrada, a pessoa tinha de repetir
    "Arrodes, próxima." segundos depois. Conversa e música vazando não
    viram, por acaso, o mesmo "próxima"/"embaralha a fila" nas duas escutas.
    """
    if acao in _SEM_CONFIRMACAO_PELO_COMANDO or not segunda:
        return False
    # "hello" é o nome sozinho OU um pedido sem o nome da música ("toca como
    # próxima…"): só o segundo, com o verbo de pedir nas duas escutas, confirma
    if acao == "hello" and not tem_pedido_de_tocar(segunda):
        return False
    lido = parse_command(f"{WAKE_WORD}, {segunda}")
    if lido is not None and lido[0] == acao:
        return True
    # "Aroldo, com o próximo": o nome mal ouvido na frente quebrava o parse e a
    # chamada de verdade era barrada. Sem a 1ª palavra, o resto é o comando.
    palavras = segunda.split(maxsplit=1)
    if len(palavras) < 2:
        return False
    lido = parse_command(f"{WAKE_WORD}, {palavras[1]}")
    return lido is not None and lido[0] == acao


def _fuzzy_in(word: str, options: tuple[str, ...]) -> bool:
    if word in options:
        return True
    if len(word) < 3:
        # 26/09: "Arrodes, Pé, Pé, Pé, Quase." — "pe" é 0,8 de "poe" e virou
        # play de "pe pe quase". Palavra de 2 letras só exata.
        return False
    return any(
        difflib.SequenceMatcher(None, word, option).ratio() >= _FUZZY_THRESHOLD
        for option in options
    )


def _par_do_tamanho_do_nome(a: str, b: str) -> bool:
    """O par colado tem a forma do nome partido em dois ("Arro des", "A rodes")?

    Colada, a conversa forma o nome no fuzzy: "carro de" e "arroz de" dão 0,86,
    "carro desse" 0,82. O nome partido não acaba em preposição de duas letras
    nem passa dele em mais de uma letra — a não ser que a 2ª palavra já seja o
    nome ("Couto Iba-Tá, arrodes": é o par "ta arrodes" que o acha).
    """
    return len(b) >= 3 and (len(a + b) <= len(WAKE_WORD) + 1 or _is_wake_word(b))


def _is_wake_pair(a: str, b: str) -> bool:
    """Wake word transcrita como duas palavras ("Arro des", "Hero des").

    SEM regra de prefixo: colando as vizinhas, a conversa forma o começo do
    nome à toa ("jogar rodada" → "jogarrodada") — só variação exata ou fuzzy
    contra a palavra inteira, e o fuzzy só num par com a forma do nome.
    """
    combined = a + b
    if combined in _WAKE_BLOCKLIST:
        return False
    if combined in _WAKE_VARIANTS:
        return True
    if not _par_do_tamanho_do_nome(a, b):
        return False
    return difflib.SequenceMatcher(None, combined, WAKE_WORD).ratio() >= _WAKE_THRESHOLD


def chamou_de_frente(text: str) -> bool:
    """A frase COMEÇA pela wake word? ("Arrodes, ..." sim; "não, do Herodes que..." não)

    A wake word é procurada nas 3 primeiras palavras porque o STT às vezes põe
    lixo na frente — mas "Herodes" e "Rhodes" (grafias do nome na transcrição)
    também são palavras que as pessoas dizem conversando.
    """
    words = _sem_muletas_na_frente(text)
    if not words:
        return False
    return _is_wake_word(words[0]) or (len(words) > 1 and _is_wake_pair(words[0], words[1]))


def _find_wake(words: list[str]) -> list[str] | None:
    """Resto da frase após a wake word (procurada no começo), ou None."""
    limit = min(_WAKE_SCAN_WORDS, len(words))
    for i in range(limit):
        if _is_wake_word(words[i]):
            return words[i + 1 :]
        if i + 1 < len(words) and _is_wake_pair(words[i], words[i + 1]):
            return words[i + 2 :]
    return None


def _le_numero(words: list[str], i: int) -> tuple[int, int] | None:
    """Número em words[i:] — dígitos ou por extenso até 199 — e quantas palavras ocupa."""
    if i >= len(words):
        return None
    word = words[i]
    if word.isdigit():
        return int(word), 1
    if word == "cem":
        return 100, 1
    if word == "duzentos":  # 25/09: "volume duzentos" (o teto do volume corta)
        return 200, 1
    if word == "cento":
        resto = _le_numero(words, i + 2) if words[i + 1 : i + 2] == ["e"] else None
        if resto is not None and resto[0] < 100 and not words[i + 2].isdigit():
            return 100 + resto[0], 2 + resto[1]
        return 100, 1
    if word in _DEZENAS:
        unidade = _NUMEROS.get(words[i + 2], 0) if words[i + 1 : i + 2] == ["e"] and i + 2 < len(words) else 0
        if 0 < unidade < 10:
            return _DEZENAS[word] + unidade, 3
        return _DEZENAS[word], 1
    if word in _NUMEROS:
        return _NUMEROS[word], 1
    return None


def _le_duracao(words: list[str], i: int) -> tuple[int, int] | None:
    """Duração falada em words[i:] → (segundos, palavras usadas).

    "30 segundos", "dez segundos", "meio minuto", "um minuto e meio",
    "2 minutos e 15 segundos", "1 minuto e 30". Número SEM unidade não é duração.
    """
    if words[i : i + 2] in (["meio", "minuto"], ["meio", "min"]):
        return 30, 2
    numero = _le_numero(words, i)
    if numero is None:
        return None
    valor, usados = numero
    j = i + usados
    unidade = _UNIDADES_S.get(words[j]) if j < len(words) else None
    if unidade is None:
        return None
    total, j = valor * unidade, j + 1
    if unidade == 60:
        k = j + 1 if words[j : j + 1] == ["e"] else j
        if k > j and words[k : k + 1] in (["meio"], ["meia"]):
            return total + 30, k + 1 - i
        resto = _le_numero(words, k)
        if resto is not None and resto[0] < 60:
            fim = k + resto[1]
            com_unidade = fim < len(words) and _UNIDADES_S.get(words[fim]) == 1
            if k > j or com_unidade or fim == len(words):  # "1 minuto e 30", "1 minuto 30"
                return total + resto[0], fim + com_unidade - i
    return total, j - i


def _seek_falado(rest: list[str]) -> str | None:
    """ "avança 30 segundos" → "+30"; "volta 10 segundos" → "-10"; "vai pro
    minuto 2" → "120"; "volta pro começo" → "0". None = não é seek."""
    first = rest[0]
    if first in _SEEK_RECOMECA:
        return "0"
    frente, tras = first in _SEEK_FRENTE, first in _SEEK_TRAS
    # "minuto 5" sozinho foi para o LLM no teste de 20/09 (1s a mais); "segundo 30
    # dessa música", no de 21/09
    if not (frente or tras or first in _SEEK_IR or first in ("minuto", "segundo")):
        return None
    if (tras or first in ("vai", "va")) and len(rest) <= 5 and ("comeco" in rest or "inicio" in rest):
        return "0"
    # "minuto 2", "minuto 1 e 30" — mas não o "minuto" de "avança UM minuto"
    for i in range(0 if first == "minuto" else 1, len(rest)):
        if rest[i] != "minuto" or (i > 0 and _le_numero(rest, i - 1) is not None):
            continue
        numero = _le_numero(rest, i + 1)
        if numero is None:
            continue
        total, k = numero[0] * 60, i + 1 + numero[1]
        if rest[k : k + 1] == ["e"]:
            k += 1
        resto = _le_numero(rest, k)
        if resto is not None and resto[0] < 60:
            total += resto[0]
        return str(total)
    # "segundo 30", "vai pro segundo 45" — mas não o "segundo" de "avança UM segundo"
    for i in range(0 if first == "segundo" else 1, len(rest)):
        if rest[i] != "segundo" or (i > 0 and _le_numero(rest, i - 1) is not None):
            continue
        numero = _le_numero(rest, i + 1)
        if numero is not None:
            return str(numero[0])
    for i in range(1, len(rest)):
        duracao = _le_duracao(rest, i)
        if duracao is None:
            continue
        antes = rest[i - 1] if rest[i - 1] not in ("o", "os") else rest[i - 2]
        if i >= 2 and antes in _PARA_ONDE:
            return str(duracao[0])
        if frente or (not tras and "frente" in rest):
            return f"+{duracao[0]}"
        if tras or "tras" in rest or "atras" in rest:
            return f"-{duracao[0]}"
        return None
    return None


def _depois_do_nao(rest: list[str]) -> tuple[str, str] | None:
    """Quem se corrige no meio da frase quer o que disse DEPOIS do "não".

    Teste de 21/09: "minuto trinta, minuto, não, segundo trinta, essa música" virou
    seek para o minuto 30 de uma faixa de 4:19. Só vale entre seek e volume — o
    começo da frase tem de ser um deles, e o que vem depois do "não" também
    ("toca não quero dinheiro" segue sendo tocar). A cauda pode herdar o verbo:
    "avança 30 segundos, não, 10 segundos".
    """
    if "nao" not in rest[1:]:
        return None
    corte = len(rest) - 1 - rest[::-1].index("nao")
    comeco, cauda = rest[:corte], rest[corte + 1 :]
    if not cauda or (_volume_falado(comeco) is None and _seek_falado(comeco) is None):
        return None
    for palavras in (cauda, [rest[0]] + cauda):
        volume = _volume_falado(palavras)
        if volume is not None:
            return ("volume", volume)
        posicao = _seek_falado(palavras)
        if posicao is not None:
            return ("seek", posicao)
    return None


def _volume_falado(rest: list[str]) -> str | None:
    """ "volume 50" → "50"; "aumenta o volume" → "+20"; "abaixa o som em 10" →
    "-10". None = não é volume ("toca volume morto" segue sendo tocar)."""
    first = rest[0]
    if len(rest) <= 3 and rest[:2] == ["mais", "alto"]:
        return f"+{_PASSO_VOLUME}"
    if len(rest) <= 3 and rest[:2] == ["mais", "baixo"]:
        return f"-{_PASSO_VOLUME}"
    sobe, desce = first in _VOL_SOBE, first in _VOL_DESCE
    tem_alvo = "volume" in rest or ((sobe or desce) and "som" in rest)
    # "abaixa um pouco", "aumenta mais um pouquinho aí": sem alvo, mas só volume faz sentido
    so_intensidade = all(w in ("um", "pouco", "pouquinho", "mais", "ai", "isso", "ele") for w in rest[1:])
    if not tem_alvo and not ((sobe or desce) and (len(rest) <= 2 or so_intensidade)):
        return None
    if not (sobe or desce or first in _VOL_POE):
        return None
    numero, onde = None, 0
    for i in range(1, len(rest)):
        numero = _le_numero(rest, i)
        if numero is not None and rest[i] not in ("um", "uma"):  # "abaixa UM pouco"
            onde = i
            break
        numero = None
    if not (sobe or desce):
        if numero is not None:
            return str(numero[0])
        # "volume máximo", "volume na metade": sem número, mas não precisa de LLM
        return next((_VOLUME_PALAVRAS[w] for w in rest if w in _VOLUME_PALAVRAS), None)
    sinal = "+" if sobe else "-"
    if numero is None:
        return f"{sinal}{_PASSO_VOLUME}"
    if rest[onde - 1] in _PARA_ONDE:  # "aumenta o volume PARA 80" é destino
        return str(numero[0])
    return f"{sinal}{numero[0]}"


def _le_posicao(words: list[str], i: int, *, numero_solto: bool) -> tuple[int, int] | None:
    """Posição na fila em words[i:] → (posição, palavras usadas). "terceira",
    "décima segunda", "última", "número 5", "música 4"; número sem rótulo só
    quando `numero_solto` ("toca 1979" é busca; "toca a 3" é a terceira)."""
    if i >= len(words):
        return None
    word = words[i]
    if word in _DO_FIM:
        return _DO_FIM[word], 1
    if word in _ORDINAIS:
        dezena = _ORDINAIS[word]
        unidade = _ORDINAIS.get(words[i + 1], 0) if i + 1 < len(words) else 0
        if dezena in (10, 20) and 0 < unidade < 10:
            return dezena + unidade, 2
        return dezena, 1
    j, rotulo = i, False
    if words[j] in _ROTULOS_DE_FAIXA or words[j] == "posicao":
        j, rotulo = j + 1, True
        if words[j : j + 2] in (["de", "numero"], ["da", "posicao"]):  # "a música DE número um"
            j += 1
    if j < len(words) and words[j] in ("numero", "n"):
        j, rotulo = j + 1, True
    numero = _le_numero(words, j)
    if numero is not None and numero[0] > 0 and (rotulo or numero_solto):
        return numero[0], j + numero[1] - i
    return None


def _alvo_da_fila(words: list[str], *, numero_solto: bool, nome_ok: bool) -> str | None:
    """As palavras que dizem QUAL faixa → "#N" ou o nome. None = não é alvo de fila."""
    for sufixo in _FILA_SUFIXOS:
        if tuple(words[-len(sufixo):]) == sufixo:
            words, nome_ok = words[: -len(sufixo)], True
            break
    i = 0
    while i < len(words) and words[i] in _ARTIGOS:
        i += 1
    posicao = _le_posicao(words, i, numero_solto=numero_solto or i > 0)
    if posicao is not None and all(w in _SOBRA_DE_FILA for w in words[i + posicao[1] :]):
        return f"#{posicao[0]}"
    if not nome_ok:
        return None
    nome = [w for w in words[i:]]
    while nome and (nome[0] in _ARTIGOS or nome[0] in _ROTULOS_DE_FAIXA or nome[0] in ("do", "da", "de", "dos", "das")):
        nome.pop(0)
    return " ".join(nome) or None


def _pergunta_da_fila(rest: list[str]) -> tuple[str, str] | None:
    """ "qual a próxima?" → next_up; "o que tem na fila?" → queue."""
    first = rest[0]
    if first not in _PERGUNTAS:
        return None
    if first in ("qual", "que") and (
        "proxima" in rest or "seguinte" in rest or ("vem" in rest and "depois" in rest)
    ):
        return ("next_up", "")
    if "fila" in rest or "proximas" in rest:
        return ("queue", "")
    return None


_AGORA = ("agora", "ja")


def _tira_o_agora(words: list[str]) -> tuple[list[str], bool]:
    """ "… agora", "… já", "… agora mesmo" no fim — ou "agora …", "já …" no começo
    ("toca AGORA in the pool", teste de 21/09) → (o resto, True)."""
    sobra = words[:-1] if words[-2:] == ["agora", "mesmo"] else words
    if sobra and sobra[-1] in _AGORA:
        return sobra[:-1], True
    if len(words) > 1 and words[0] in _AGORA:
        return words[1:], True
    return words, False


def sem_o_agora(texto: str) -> str:
    """O alvo do play_now sem o "agora": "agora in the pool" → "in the pool"."""
    return " ".join(_tira_o_agora(texto.split())[0])


def agora_e_do_nome(titulo: str, dito: str) -> bool:
    """O "agora" que a pessoa disse é o do NOME da música achada? ("toca agora só
    falta você" → "Agora Só Falta Você", Rita Lee.) Aí não é pedido de corte."""
    palavras = _normalize(_ENTRE_PARENTESES.sub(" ", titulo)).split()
    ditas = dito.split()
    if not palavras or not ditas:
        return False
    if ditas[0] in _AGORA:
        return palavras[0] == ditas[0]
    return palavras[-1] in _AGORA + ("mesmo",)


def _sem_eco_do_prompt(words: list[str]) -> list[str]:
    """O Whisper às vezes começa a transcrição ECOANDO o fim do prompt ("Músicas:
    a, b."). Sai algo como 'Músicas Preta. Arrodes, toca mágica.', que só
    funcionava porque a wake word caía na 3ª palavra. Com o eco na frente, a
    frase começa de verdade na wake word."""
    if words and words[0] == "musicas":
        for i, word in enumerate(words):
            if _is_wake_word(word) or (i + 1 < len(words) and _is_wake_pair(word, words[i + 1])):
                return words[i:]
    return words


def _quero_ouvir(rest: list[str]) -> list[str]:
    """ "quero escutar a ferida curada" → "toca a ferida curada".

    No teste de 21/09 a frase foi para o LLM: 0,9s a mais, e ele devolveu o nome
    REESCRITO ('A Ferida, Curada - Cristiano Zeneta'). Sem dizer o quê ("quero
    ouvir"), a frase segue como veio.
    """
    i = 1 if rest[0] == "eu" else 0
    if i >= len(rest) or rest[i] not in _QUERER:
        return rest
    i += 1
    if i < len(rest) and rest[i] == "de":
        i += 1
    if i < len(rest) and rest[i] in _OUVIR:
        i += 1
    elif i + 1 < len(rest) and rest[i] == "que" and rest[i + 1] in _QUE_TOQUE:
        i += 2
    else:
        return rest
    return ["toca"] + rest[i:] if i < len(rest) else rest


def _sem_prefixo(words: list[str], prefixos: tuple[tuple[str, ...], ...]) -> list[str] | None:
    """O que vem depois do primeiro prefixo que abre `words`; None = nenhum abre."""
    for prefixo in prefixos:
        if tuple(words[: len(prefixo)]) == prefixo:
            return words[len(prefixo):]
    return None


def _marca_de_proxima(alvo_words: list[str]) -> list[str] | None:
    """Tira do fim de `alvo_words` a marca de "como próxima" e devolve o alvo que
    sobrou; None = a frase não tem a marca.

    No teste de 21/09, "coloca a realidade ou fantasia como próxima DA FILA" virou
    play_queue: a marca só era procurada no fim da frase, e o fim era "da fila" —
    o bot CORTOU a música que tocava em vez de pôr a outra na frente.
    """
    cauda, da_fila = alvo_words, False
    # "como a próxima A TOCAR" (26/09: "coloca a segunda música da fila como a
    # próxima a tocar" foi para a busca do YouTube)
    if cauda[-2:] in (["a", "tocar"], ["pra", "tocar"], ["para", "tocar"]):
        cauda = cauda[:-2]
    for sufixo in _FILA_SUFIXOS:
        if len(cauda) > len(sufixo) and tuple(cauda[-len(sufixo):]) == sufixo:
            cauda, da_fila = cauda[: -len(sufixo)], True
            break
    # "como a próxima MÚSICA"
    sem_rotulo = cauda[:-1] if cauda and cauda[-1] in _ROTULOS_DE_FAIXA else cauda
    for candidata in (sem_rotulo, cauda):
        for marca in _MARCAS_DE_PROXIMA + _MARCAS_DE_FRENTE:
            tamanho = len(marca)
            if len(candidata) <= tamanho or tuple(candidata[-tamanho:]) != marca:
                continue
            alvo = candidata[:-tamanho]
            if marca in _MARCAS_DE_FRENTE and not da_fila:
                posicao = _alvo_da_fila(alvo, numero_solto=False, nome_ok=False)
                if posicao is None:
                    continue
            return alvo
    return None


def _fila_falada(rest: list[str]) -> tuple[str, str] | None:
    """Os comandos que mexem na fila. None = não é um deles (segue o parser antigo)."""
    first = rest[0]
    if any(w.startswith("favorit") for w in rest) or _NAO_E_FILA.intersection(rest):
        return None

    # "a próxima é a sexta" (o "a" da frente já saiu como filler)
    if first in ("proxima", "proximo") and len(rest) >= 3 and rest[1] in ("e", "vai", "sera"):
        alvo = _alvo_da_fila([w for w in rest[2:] if w != "ser"], numero_solto=True, nome_ok=True)
        return ("play_next", alvo) if alvo else None

    # "música anterior", "toca a anterior", "volta pra anterior"
    if "anterior" in rest and len(rest) <= 5:
        return ("previous", "")

    # ── tocar / pôr como próxima ────────────────────────────────────────────
    depois = first == "depois"
    corpo = rest[1:] if depois else rest
    if corpo and corpo[0] in _PLAY_VERBS:
        # "toca pyramids da fila AGORA": com o "agora" no fim, o "da fila" não
        # era visto e a frase inteira ia para a busca do YouTube
        alvo_words = _tira_o_agora(corpo[1:])[0]
        nucleo = [w for w in alvo_words if w not in _ARTIGOS and w not in _ROTULOS_DE_FAIXA]
        if nucleo in (["proxima"], ["proximo"]):
            return ("skip", "")
        depois_da_marca = _sem_prefixo(alvo_words, _PROXIMA_NA_FRENTE)
        if depois_da_marca is not None:
            nome = _sem_prefixo(depois_da_marca, (("da", "fila"),)) or depois_da_marca
            while nome and nome[0] in _ROTULOS_DE_FAIXA + ("da", "fila", "a", "tocar"):
                nome = nome[1:]
            alvo = _alvo_da_fila(nome, numero_solto=False, nome_ok=True)
            # sem o nome (ele vem no próximo trecho): chamada sem pedido
            return ("play_next", alvo) if alvo else ("hello", "")
        depois_da_fila = _sem_prefixo(alvo_words, _NA_FILA_NA_FRENTE)
        if depois_da_fila is not None:
            query = re.sub(r"^(?:a musica|a|o|uma|um)\s+", "", " ".join(depois_da_fila))
            return ("play", query) if query else ("hello", "")
        if corpo[0] in _VERBOS_DE_POR:
            for sufixo in _NO_FIM_DA_FILA:
                if len(alvo_words) > len(sufixo) and tuple(alvo_words[-len(sufixo):]) == sufixo:
                    nome = alvo_words[: -len(sufixo)]
                    sem_marca = _marca_de_proxima(nome)
                    if sem_marca is not None:
                        alvo = _alvo_da_fila(sem_marca, numero_solto=False, nome_ok=True)
                        return ("play_next", alvo) if alvo else ("hello", "")
                    query = re.sub(r"^(?:a musica|a|o|uma|um)\s+", "", " ".join(nome))
                    # "no final/fim da fila" é esperar a vez; "na fila" é o pedido
                    # de sempre, que entra como a próxima (pedido do Otavio, 28/09)
                    no_fim = "final" in sufixo or "fim" in sufixo
                    return ("play_end" if no_fim else "play", query)
        proxima = depois
        sem_marca = _marca_de_proxima(alvo_words)
        if sem_marca is not None:
            alvo_words, proxima = sem_marca, True
        if alvo_words[-2:] in (["pra", "tocar"], ["para", "tocar"]):
            alvo_words = alvo_words[:-2]
        alvo = _alvo_da_fila(alvo_words, numero_solto=False, nome_ok=proxima)
        if alvo is None:
            return None
        return ("play_next" if proxima else "play_queue", alvo)

    resto = [w for w in rest[1:] if w not in ("umas", "uns", "mais", "so")]
    sem_artigo = [w for w in resto if w not in _ARTIGOS and w not in _ROTULOS_DE_FAIXA]

    # ── "as próximas 3" / "as 3 próximas" / "a próxima": saem da fila ───────
    if first in _PULAR_VERBOS or first in _TIRAR_VERBOS:
        numero = None
        # "retira a próxima música DA FILA", "exclua as três próximas DA FILA" (25/09)
        for sufixo in _FILA_SUFIXOS:
            if len(sem_artigo) > len(sufixo) and tuple(sem_artigo[-len(sufixo):]) == sufixo:
                sem_artigo = sem_artigo[: -len(sufixo)]
                break
        if sem_artigo[:1] == ["proximas"]:
            numero = _le_numero(sem_artigo, 1)
            sobra = sem_artigo[1 + numero[1] :] if numero else ["?"]
        elif sem_artigo[-1:] == ["proximas"]:
            numero = _le_numero(sem_artigo, 0)
            sobra = sem_artigo[numero[1] : -1] if numero else ["?"]
        if numero is not None and numero[0] > 0 and not sobra:
            return ("remove", f"#1-{numero[0]}")
        if first in _TIRAR_VERBOS and sem_artigo in (["proxima"], ["proximo"]):
            return ("remove", "#1")

    # ── "pula pra quinta", "vai pra música do frank ocean" ──────────────────
    if first in _IR_PARA_VERBOS and len(rest) >= 3 and rest[1] in _PARA_A_FAIXA:
        destino = rest[2:]
        nucleo = [w for w in destino if w not in _ARTIGOS and w not in _ROTULOS_DE_FAIXA]
        if nucleo in (["proxima"], ["proximo"]):
            return ("skip", "")
        if {"comeco", "inicio", "minuto", "frente", "tras", "final", "fim"}.intersection(destino):
            return None  # é posição DENTRO da música: assunto do seek
        alvo = _alvo_da_fila(destino, numero_solto=True, nome_ok=True)
        return ("jump", alvo) if alvo else None

    # ── "tira a terceira", "tira hotel da fila", "pula a terceira" ──────────
    if first in _TIRAR_VERBOS:
        alvo = _alvo_da_fila(resto, numero_solto=False, nome_ok=True)
        return ("remove", alvo) if alvo else None
    if first in _PULAR_VERBOS and resto[:1] and resto[0] in _ARTIGOS:
        alvo = _alvo_da_fila(resto, numero_solto=False, nome_ok=False)
        if alvo is not None and not alvo.startswith("#-"):
            return ("remove", alvo)

    # ── número solto: faixas ou segundos? ───────────────────────────────────
    numero = _le_numero(sem_artigo, 0)
    so_o_numero = numero is not None and all(w in _SOBRA_DE_FILA for w in sem_artigo[numero[1] :])
    com_rotulo = any(w in _ROTULOS_DE_FAIXA for w in resto)
    if so_o_numero and numero[0] > 0 and not (resto[:1] and resto[0] in ("a", "o", "as", "os") and not com_rotulo):
        quantas = numero[0]
        if first in _SEEK_TRAS:
            if com_rotulo or resto == ["uma"]:
                return ("previous", "" if quantas == 1 else str(quantas))
            return ("seek", f"-{quantas}")  # "volta 10": segundos
        if first in _PULAR_VERBOS or first in _AVANCAR_VERBOS:
            if com_rotulo or (first in _PULAR_VERBOS and quantas <= _PULAR_FAIXAS_ATE):
                return ("skip", "" if quantas == 1 else str(quantas))
            return ("seek", f"+{quantas}")  # "avança 30": segundos
    return None


def _palavras_do_nome(texto: str) -> list[str]:
    return [w for w in _normalize(texto).split() if w not in _NOME_IGNORA]


def _parecidas(a: str, b: str, minimo: float = 0.8) -> bool:
    # os dois "quick" são tetos baratos do ratio(): descartam quase tudo antes da
    # conta cara — a fila pode ter 500 faixas e isto roda no event loop
    comparador = difflib.SequenceMatcher(None, a, b)
    return (
        comparador.real_quick_ratio() >= minimo
        and comparador.quick_ratio() >= minimo
        and comparador.ratio() >= minimo
    )


def _palavra_em(word: str, alvo: list[str]) -> bool:
    if word in alvo:
        return True
    if len(word) < 4:
        return False
    # o Whisper aportuguesa título em inglês ("pirâmides" para "Pyramids") e cola
    # nome de dupla: "Zé Neto" chegou como "Zeneta" (teste de 21/09) — por isso
    # a palavra dita é comparada também com as vizinhas coladas ("zeneto")
    coladas = [a + b for a, b in zip(alvo, alvo[1:])]
    return any(_parecidas(word, outra) for outra in alvo + coladas)


def acha_na_fila(tracks, dito: str, *, exigente: bool = False):
    """A faixa da fila que a pessoa nomeou → (posição 1-based, faixa, nota) ou None.

    Conta quantas palavras ditas aparecem no título/artista (tolerando a grafia
    do Whisper) e desempata pela semelhança com o título — "hotel" é Hotel e não
    Hotel California; só o artista devolve a primeira dele.

    `exigente`: para o "toca X" SEM "da fila" na frase, em que o normal é buscar
    no YouTube — só casa se TODAS as palavras baterem e, com uma palavra só, se
    ela for praticamente o título ("love" não vira "Love Story").
    """
    consulta = _palavras_do_nome(dito)
    if not consulta:
        return None
    dito_limpo = " ".join(consulta)
    # título em inglês que o Whisper soletrou: "So Am I" chegou como "Soul A M I"
    # e "sou M.I." (teste de 21/09). Palavra por palavra não casa; tudo COLADO é
    # quase igual ("soumiavamax" × "soamiavamax"). Curto demais não vale.
    dito_colado = "".join(consulta)

    def colado(outro: list[str]) -> float:
        comparador = difflib.SequenceMatcher(None, dito_colado, "".join(outro))
        if comparador.real_quick_ratio() < _COLADO_MINIMO or comparador.quick_ratio() < _COLADO_MINIMO:
            return 0.0
        return comparador.ratio()

    def parecido(outro: list[str]) -> float:
        return difflib.SequenceMatcher(None, dito_limpo, " ".join(outro)).ratio() if outro else 0.0

    melhor = None
    for posicao, track in enumerate(tracks, 1):
        titulo = _palavras_do_nome(_ENTRE_PARENTESES.sub(" ", track.title))
        artista = _palavras_do_nome(track.artist)
        alvo = titulo + artista
        if not alvo:
            continue
        cobertura = sum(1 for w in consulta if _palavra_em(w, alvo)) / len(consulta)
        junto = 0.0
        if len(dito_colado) >= _COLADO_TAMANHO:
            junto = max(colado(titulo), colado(alvo), colado(artista + titulo))
            junto = junto if junto >= _COLADO_MINIMO else 0.0
        if exigente and not junto and not (
            cobertura == 1.0 and (len(consulta) >= 2 or parecido(titulo) >= 0.6)
        ):
            continue
        semelhanca = max(parecido(titulo), parecido(artista), parecido(alvo), parecido(artista + titulo))
        nota = max(0.75 * cobertura + 0.25 * semelhanca, junto)
        if melhor is None or nota > melhor[2] + 1e-9:
            melhor = (posicao, track, nota)
    if melhor is None or melhor[2] < (0.85 if exigente else 0.6):
        return None
    return melhor


def _le_relogio(texto: str) -> int | None:
    if not texto:
        return None
    relogio = _POSICAO_RELOGIO.match(texto)
    if relogio:
        horas, minutos, segundos = relogio[1], int(relogio[2]), int(relogio[3])
        if segundos >= 60 or (horas is not None and minutos >= 60):
            return None
        return int(horas or 0) * 3600 + minutos * 60 + segundos
    unidades = _POSICAO_UNIDADES.match(texto)
    if unidades and any(unidades.groups()):
        horas, minutos, segundos = (int(g or 0) for g in unidades.groups())
        return horas * 3600 + minutos * 60 + segundos
    return None


def parse_posicao(texto: str, atual_s: float, duracao_s: float) -> int | None:
    """Argumento do /seek → segundo de destino, ou None se não der para ler.

    Absolutos: "1:30", "1:02:03", "90", "2m", "1m30s", "45s". Com "+" ou "-" na
    frente, os mesmos valem a partir da posição atual. O destino fica entre 0 e
    5s antes do fim; duração desconhecida (ao vivo) = sem teto.
    """
    texto = texto.strip().lower().replace(" ", "")
    sinal = {"+": 1, "-": -1}.get(texto[:1], 0)
    valor = _le_relogio(texto[1:] if sinal else texto)
    if valor is None:
        return None
    alvo = atual_s + sinal * valor if sinal else valor
    if duracao_s > 0:
        alvo = min(alvo, duracao_s - _FOLGA_DO_FIM_S)
    return int(max(0, alvo))


def parse_volume(texto: str, atual: int) -> int | None:
    """Argumento do volume → % de destino entre 0 e VOLUME_MAX, ou None.

    "50" e "80%" são destino; "+20" e "-20" são passo a partir do volume atual.
    """
    pedido = _VOLUME_PEDIDO.match(texto.strip().replace(" ", ""))
    if pedido is None:
        return None
    sinal, valor = pedido[1], int(pedido[2])
    alvo = atual + valor if sinal == "+" else atual - valor if sinal == "-" else valor
    return max(0, min(VOLUME_MAX, alvo))


def parse_command(text: str) -> tuple[str, str] | None:
    """Texto transcrito → (ação, argumento), ou None se não chamou o bot.

    Ações: play, skip, pause, resume, shuffle, autoplay, leave, loop,
    loop_off, clear_queue, now_playing, seek, volume, hello, unknown — e as de
    fila: skip com quantidade, jump, play_queue, play_next, remove, queue,
    next_up, previous. "toca X" (play) entra como a próxima; "toca X agora"
    (play_now) corta para ela.
    O argumento do seek é o do /seek ("+30", "-10", "120"); o do volume é
    "50" (destino) ou "+20"/"-20" (passo); o das ações de fila é "#N" (posição
    do painel; "#-1" = a última; "#1-3" = da 1 à 3) ou o nome dito.
    """
    words = _sem_muletas_na_frente(text)
    if not words:
        return None

    rest = _find_wake(words)
    if rest is None:
        return None

    # o modelo às vezes ecoa a wake word ("Arrodes, Arrodes, Arrodes, pula")
    while rest and _is_wake_word(rest[0]):
        rest = rest[1:]
    while rest and rest[0] in _FILLERS:
        rest = rest[1:]

    if not rest:
        return ("hello", "")
    rest = _sem_muletas(rest)
    if not rest:
        return ("hello", "")
    rest = _quero_ouvir(rest)
    phrase = " ".join(rest)
    first = rest[0]

    # frases compostas primeiro — contêm palavras que sozinhas significam
    # outra coisa ("PARA de repetir" ≠ pause; "apaga TUDO" ≠ "para tudo")
    if any(p in phrase for p in _CLEAR_PHRASES):
        return ("clear_queue", "")
    if any(p in phrase for p in _LOOP_OFF_PHRASES):
        return ("loop_off", "")
    if first in _LOOP_WORDS or "loop" in rest or "repeat" in rest:
        return ("loop", "")
    # antes do "que música é essa": "que música vem depois?" é sobre a fila
    pergunta = _pergunta_da_fila(rest)
    if pergunta is not None:
        return pergunta
    if any(p in phrase for p in _NOW_PLAYING_PHRASES):
        return ("now_playing", "")
    if any(w.startswith("favorit") for w in rest):
        # "bota NOS favoritos" = salvar; "toca AS favoritas" = tocar a playlist
        if "nos" in rest or "aos" in rest:
            return ("favorite", "")
        if any(_fuzzy_in(w, _PLAY_VERBS) for w in rest[:2]):
            return ("play_favs", "")
        return ("favorite", "")
    if len(rest) <= 3 and first in ("salva", "salvar", "curti", "amei", "gostei"):
        return ("favorite", "")

    # ANTES de skip/resume: "pula 15 segundos" e "volta 10 segundos" têm os
    # verbos deles — o que separa é o número com unidade
    corrigido = _depois_do_nao(rest)
    if corrigido is not None:
        return corrigido
    volume = _volume_falado(rest)
    if volume is not None:
        return ("volume", volume)
    posicao = _seek_falado(rest)
    if posicao is not None:
        return ("seek", posicao)
    # a fila: "pula 3", "toca a terceira", "pula pra quinta", "tira a última",
    # "volta uma música" — também antes de skip/resume/tocar, pelos mesmos verbos
    # "tira essa (música)": é pular a que toca, não tirar alguém da fila
    if first in _TIRAR_VERBOS and rest[1:] in _ESSA_AQUI:
        return ("skip", "")
    da_fila = _fila_falada(rest)
    if da_fila is not None:
        return da_fila

    # "dá play", "liga, volta a play" (27/09): o play no fim de frase curta é despausar
    if rest[-1] == "play" and len(rest) <= 4 and all(w in _DA_PLAY for w in rest[:-1]):
        return ("resume", "")
    # "play" sozinho = despausar; "play <algo>" = tocar
    if first == "play":
        query = " ".join(rest[1:])
        return ("play", query) if query else ("resume", "")

    # pausa ANTES de skip: "pausa" e "passa" são vizinhos no fuzzy — e por
    # isso os dois lados só aceitam essas duas por match exato
    if first.startswith("pausa") or first == "pause" or phrase in _PAUSE_PHRASES:
        return ("pause", "")
    # "calma, me espera" (27/09), "espera um pouco aí": frase curta sem pedido de música
    if first in _ESPERA_WORDS and len(rest) <= 4 and not any(w in _PLAY_VERBS for w in rest):
        return ("pause", "")
    if _fuzzy_in(first, _SKIP_WORDS):
        return ("skip", "")
    if first in _SWAP_WORDS:
        for prep in ("pra", "para", "por"):
            if prep in rest[1:]:
                query = " ".join(rest[rest.index(prep) + 1 :])
                if query:
                    return ("play", query)
        return ("skip", "")
    if len(rest) <= 2 and _fuzzy_in(first, _SKIP_SHORT_WORDS):
        return ("skip", "")
    if len(rest) <= 3 and _fuzzy_in(first, _RESUME_WORDS):
        return ("resume", "")
    if "autoplay" in rest or first == "radio":
        return ("autoplay", "")
    # "liga/desliga/tira/para a rádio" (rádio por último, frase curta): é a
    # rádio do autoplay — "toca rádio gaúcha" segue para o verbo de tocar
    if first in _TOGGLE_WORDS and len(rest) <= 4 and rest[-1] == "radio":
        return ("autoplay", "")
    # "sai" como primeira palavra cobre "sai", "sai daí", "sai da cal(l)"…
    if any(p in phrase for p in _LEAVE_PHRASES) or first == "sai" or first in _LEAVE_WORDS:
        return ("leave", "")
    if _fuzzy_in(first, _SHUFFLE_WORDS) or (
        first in ("modo", "ativa", "liga", "deixa") and len(rest) <= 3
        and any(w in _SHUFFLE_WORDS for w in rest)
    ):
        return ("shuffle", "")

    # verbo de tocar nas 2 primeiras palavras ("com toque última noite")
    for index in (0, 1):
        if index == 1 and first in _SUJEITOS:
            # "vou JOGAR agora", "vai TOMAR…": a 2ª palavra só vale como verbo de
            # tocar embolado ("com toque X"), não depois de quem conversa
            break
        if index < len(rest) and _fuzzy_in(rest[index], _PLAY_VERBS):
            query = " ".join(rest[index + 1 :])
            query = re.sub(r"^(?:a musica|a|o|uma|um)\s+", "", query)
            # "toca uma música", "coloca a música": o nome vem no trecho seguinte
            if query and any(w not in _ROTULOS_DE_FAIXA for w in query.split()):
                # "toca X agora": o pedido por voz entra como PRÓXIMA; o "agora"
                # é como se pede o corte. Só quando sobra um pedido de verdade —
                # "toca agora", "toca e agora" e "toca até agora" são nomes.
                sem_agora, agora = _tira_o_agora(query.split())
                if agora and (len(sem_agora) >= 2 or (sem_agora and len(sem_agora[0]) >= 4)):
                    if any(w not in _NOME_IGNORA for w in sem_agora):
                        # o alvo vai INTEIRO: "agora" também abre e fecha nome de
                        # música ("Agora Só Falta Você") e só a busca sabe dizer
                        return ("play_now", query)
                return ("play", query)
            return ("hello", "")

    # rede de segurança: palavra de controle exata no meio de lixo
    # transcrito ("Arrodes, ahn, pausa" → pause)
    for word in rest[:3]:
        if word in ("pausa", "pause"):
            return ("pause", "")
        if word in ("pula", "pule", "proxima"):
            return ("skip", "")
        if word in ("despausa", "continua"):
            return ("resume", "")
        if word in ("repete", "repita"):
            return ("loop", "")

    return ("unknown", phrase)
