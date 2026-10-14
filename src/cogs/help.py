"""Comando /help: o guia do bot, por assunto, com um menu para navegar.

Era um embed só, escrito em agosto/2026 — em 21/09 o Otavio abriu o /help no meio
de um teste e ele não tinha /seek, /volume, /diag nem NENHUM dos comandos de voz de
fila. Agora cada assunto é uma página (a de voz não cabia mais num campo de 1024
caracteres), e tests/test_help.py amarra o guia ao bot: todo comando de barra
registrado tem de aparecer aqui, e todo exemplo falado entre aspas retas tem de ser
uma frase que a gramática (core/intents.py) entende. Frase livre, que depende da
IA, vai em itálico com aspas curvas.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import discord
from discord.ext import commands

_COR = 0x1DB954

# chave → página. A ordem é a do menu.
ASSUNTOS: dict[str, dict] = {
    "inicio": {
        "rotulo": "🏠 Início",
        "resumo": "por onde começar",
        "titulo": "🎵 Arrodes — o DJ da call",
        "descricao": (
            "Toco do YouTube, importo playlist do Spotify, obedeço por voz e não"
            " deixo a música parar."
        ),
        "campos": [
            (
                "🚀 Começa por aqui",
                "`/play` `<nome ou link>` — entro na tua call e toco\n"
                '`/ouvir` — liga o modo de voz: **"Arrodes, toca <música>"**\n'
                "`/queue` mostra o que vem aí · `/skip` pula · `/stop` para tudo e sai\n"
                "O resto tu controla pelos botões do painel que fica no chat 😉\n"
                "`/help` `assunto:` abre este guia direto num assunto",
            ),
            (
                "📚 Assuntos — escolhe no menu aí embaixo 👇",
                "▶️ **Tocar** — nome, link do YouTube, playlist do Spotify\n"
                "🎚️ **Controle** — pausa, pula, vai pro minuto X, volume, loop\n"
                "📜 **Fila** — ver, tirar, embaralhar, limpar\n"
                "🎙️ **Voz** — a cola de tudo que dá pra FALAR\n"
                "⭐ **Favoritos** — favoritos, histórico e ranking\n"
                "🎲 **Rádio** — o autoplay que não deixa a call em silêncio\n"
                "🎛️ **Painel** — os botões\n"
                "🛠️ **Deu ruim?** — não tocou, não ouviu, não deixou",
            ),
        ],
        "rodape": "Atalho: /help assunto:🎙️ Voz abre direto a cola dos comandos falados",
    },
    "tocar": {
        "rotulo": "▶️ Tocar",
        "resumo": "nome, YouTube, Spotify",
        "titulo": "▶️ Tocar",
        "descricao": "Tudo começa no `/play` — tu precisa estar num canal de voz.",
        "campos": [
            (
                "`/play` `busca:`",
                "• **nome da música** — mostro 5 opções pra tu escolher (diz o artista"
                " junto que eu acerto de primeira)\n"
                "• **link do YouTube** — vídeo ou playlist inteira (até 100)\n"
                "• **link do Spotify** — faixa, álbum ou playlist pública (até 100)",
            ),
            (
                "`/play` `modo:substituir`",
                "Troca a fila INTEIRA pela nova, sem me derrubar da call (nem desligar"
                " a escuta). Sem isso, o que tu pede entra no fim da fila.",
            ),
            (
                "Outros jeitos de pôr música",
                "`/replay` — de novo a última que tu pediu\n"
                "`/playfavs` — tua playlist de favoritos inteira\n"
                '`/ouvir` — por voz: "Arrodes, toca <música>" (entra como a PRÓXIMA)',
            ),
            (
                "O que eu não consigo",
                "Mix/rádio do YouTube e Daily Mix/Descobertas do Spotify (são montados"
                " só pra ti — copia as músicas pra uma playlist tua), playlist privada"
                " e link de outros sites (Deezer, Apple Music…): manda o **nome**.",
            ),
        ],
    },
    "controle": {
        "rotulo": "🎚️ Controle",
        "resumo": "pausa, pula, minuto X, volume, loop",
        "titulo": "🎚️ Controle da música",
        "descricao": "Vale pra quem está na call comigo.",
        "campos": [
            (
                "O básico",
                "`/pause` · `/resume` — pausa e retoma\n"
                "`/skip` — pula. Quem pediu a música (ou quem gerencia o servidor) pula"
                " direto; os outros abrem votação, e a maioria dos ouvintes decide\n"
                "`/stop` — para tudo, limpa a fila e eu saio da call",
            ),
            (
                "`/seek` `posicao:` — ir pra um ponto da música",
                "`1:30` vai pro minuto 1:30 · `90` pro segundo 90 · `+30` avança 30s ·"
                " `-10` volta 10s\nPonto que a música não tem eu recuso, em vez de acabar com ela.",
            ),
            (
                "`/volume` `nivel:` — de 0 a 150",
                "`100` é o volume original da música. Fica salvo pro servidor. Sem"
                " número, mostro o atual. A troca leva ~3s pra entrar.",
            ),
            (
                "`/loop` `modo:`",
                "`desligado` · `faixa` (repete a música atual) · `fila` (a fila gira: o"
                " que toca volta pro fim). Sem escolher, alterna entre os três.",
            ),
        ],
    },
    "fila": {
        "rotulo": "📜 Fila",
        "resumo": "ver, tirar, embaralhar, limpar",
        "titulo": "📜 Fila",
        "descricao": (
            "A ordem: o `/play` entra no **fim**; pedido por **voz** entra como a"
            " **próxima**; a rádio 🎲 só toca quando a fila acaba."
        ),
        "campos": [
            (
                "Ver",
                "`/queue` — a fila paginada, com a duração de cada uma, o tempo total e"
                " um menu pra tirar música. A rádio do autoplay aparece separada.",
            ),
            (
                "Mexer",
                "`/remove` `posicao:` — tira pelo número que aparece no `/queue`\n"
                "`/shuffle` — embaralha (os pedidos por voz continuam na frente)\n"
                "`/clear` — esvazia a fila; o que tá tocando continua",
            ),
            (
                "Quem pode",
                "A tua própria música tu tira quando quiser. Com mais gente ouvindo,"
                " `/stop`, `/clear`, `/shuffle` e tirar música dos OUTROS é só pra quem"
                " gerencia o servidor. Sozinho comigo na call, pode tudo.",
            ),
            (
                "Por voz dá pra fazer mais",
                '"toca a terceira", "pula 3", "pula pra quinta", "bota a última como'
                ' próxima", "volta uma música"… — tá tudo no assunto 🎙️ **Voz**.',
            ),
        ],
    },
    "voz": {
        "rotulo": "🎙️ Voz",
        "resumo": "a cola do que dá pra falar",
        "titulo": "🎙️ Modo de voz — a cola",
        "descricao": (
            "`/ouvir` liga (e desliga) a escuta. Começa a frase com **Arrodes**, espera"
            " o estalo de “te ouvi” e fala normal — a resposta sai no chat."
        ),
        "campos": [
            (
                "▶️ Pedir música",
                '"toca <música>" — entra como **a próxima**, sem cortar ninguém\n'
                '"toca <música> agora" — corta e toca na hora\n'
                '"quero ouvir <música>" · "adiciona <música>" — vale igual\n'
                '"coloca <música> no final da fila" — espera a vez, no fim\n'
                '"bota <música> como próxima" · "toca minhas favoritas"\n'
                "**Pediu e não tocou? Pede de novo** que eu corto pra ela.",
            ),
            (
                "📜 Fila",
                '"o que tem na fila?" · "qual a próxima?"\n'
                '"toca a terceira" · "toca a última" · "toca <nome> da fila" — puxo pra'
                " agora e ninguém se perde\n"
                '"pula" · "pula 3" · "pula pra quinta" — as de antes são descartadas\n'
                '"bota a quinta como próxima" · "tira a segunda" · "tira <nome> da'
                ' fila" · "tira as próximas 2"\n'
                '"volta uma música" · "embaralha" · "limpa a fila"',
            ),
            (
                "🎚️ Controle e som",
                '"pausa" · "continua" · "repete essa" · "tira o loop"\n'
                '"avança 30 segundos" · "volta 10 segundos" · "minuto 2" · "segundo 30"'
                ' · "volta pro começo"\n'
                '"volume 50" · "aumenta o volume" · "abaixa o volume" · "volume máximo"\n'
                '"que música é essa?" · "favorita essa" · "liga a rádio" · "sai da call"',
            ),
            (
                "💡 Dicas",
                "• Diz o **artista** junto: título em inglês o transcritor escreve do"
                " jeito que ouve (*“sou emae”* não vira So Am I; *“so am i da Ava Max”* vira).\n"
                "• Frase livre também vai, a IA interpreta: *“bota algo mais animado”*,"
                " *“aquela do Frank Ocean”*.\n"
                "• Errou no meio? *“minuto 3… não, segundo 30”* — vale o que vem depois do “não”.\n"
                "• Falar só *“Arrodes”* me faz mandar a cola no chat.\n"
                "• Com a escuta ligada, a fala da call é transcrita por um serviço"
                " externo (Groq) pra achar o “Arrodes”. `/ouvir` de novo desliga.",
            ),
        ],
    },
    "favoritos": {
        "rotulo": "⭐ Favoritos",
        "resumo": "favoritos, histórico, ranking",
        "titulo": "⭐ Favoritos, histórico e ranking",
        "descricao": "Os favoritos e o histórico são teus, e separados por servidor.",
        "campos": [
            (
                "Favoritos",
                "`/fav` — favorita a que tá tocando (ou o botão ⭐ do painel, ou"
                ' "Arrodes, favorita essa")\n'
                "`/favs` — tua lista: escolhe uma pra tocar (`remover:True` apaga em vez de tocar)\n"
                "`/playfavs` — toca todos (`embaralhar:True`, `modo:substituir`)",
            ),
            (
                "Histórico",
                "`/history` — as últimas 10 que tu pediu, e quando\n"
                "`/replay` — toca de novo a última",
            ),
            (
                "Ranking",
                "`/top` `periodo:` — as músicas mais tocadas e os DJs do servidor"
                " (`semana`, `mês` ou `sempre`)",
            ),
        ],
    },
    "radio": {
        "rotulo": "🎲 Rádio",
        "resumo": "autoplay: música sem parar",
        "titulo": "🎲 Rádio (autoplay)",
        "descricao": "`/autoplay` liga e desliga — ou o botão 🎲 do painel.",
        "campos": [
            (
                "Como funciona",
                "Quando a fila acaba, eu sigo sozinho com músicas parecidas com as que"
                " tocaram — e já deixo a próxima pronta, pra trocar sem silêncio.",
            ),
            (
                "Sem furar ninguém",
                "A rádio vive numa fila à parte (aparece separada no `/queue`): qualquer"
                " pedido passa na frente dela. Desligou, as músicas que ela tinha"
                " separado somem junto.",
            ),
        ],
    },
    "painel": {
        "rotulo": "🎛️ Painel",
        "resumo": "os botões do chat",
        "titulo": "🎛️ Painel",
        "descricao": (
            "Enquanto toca, mantenho no chat um painel com a música atual, quem pediu,"
            " as próximas e os botões — ele desce pro fim do chat a cada pedido."
        ),
        "campos": [
            (
                "Botões",
                "🔀 embaralhar · ⏸️/▶️ pausar e retomar · ⏭️ pular · 🔁 loop (desligado →"
                " faixa → fila)\n⭐ favoritar · 🎲 rádio · 🚪 sair da call",
            ),
            (
                "Cores",
                "Azul = ligado. Apagado = não se aplica agora (nada na fila pra"
                " embaralhar, por exemplo).",
            ),
        ],
    },
    "problemas": {
        "rotulo": "🛠️ Deu ruim?",
        "resumo": "não tocou, não ouviu, não deixou",
        "titulo": "🛠️ Deu ruim?",
        "descricao": "`/diag` faz um teste de verdade no YouTube e diz qual parte caiu.",
        "campos": [
            (
                "Não toca, ou pula tudo",
                "Roda `/diag`. Quase sempre é o YouTube recusando o áudio (yt-dlp velho"
                " ou cookies vencidos) — o dono do bot recebe o aviso sozinho.",
            ),
            (
                "Não achou a música certa",
                "Diz o **artista** junto, ou usa `/play`, que mostra 5 opções. Link do"
                " YouTube sempre toca exatamente aquele vídeo.",
            ),
            (
                "Não me ouve",
                "O `/ouvir` tá ligado? Começa por **Arrodes**, espera o estalo e fala"
                " a frase de uma vez (até ~6s). Mais dicas no assunto 🎙️ **Voz**.",
            ),
            (
                "“Só quem gerencia o servidor pode”",
                "Com mais gente ouvindo, parar, limpar e embaralhar é de quem gerencia o"
                " servidor. Pular a música dos outros vai por votação.",
            ),
            (
                "Saí da call sozinho",
                "Fiquei 2 minutos sem ninguém comigo — saio pra não gastar banda. É só"
                " chamar de novo com `/play`.",
            ),
        ],
    },
}

_POR_ROTULO = {pagina["rotulo"]: chave for chave, pagina in ASSUNTOS.items()}


def monta_embed(chave: str) -> discord.Embed:
    pagina = ASSUNTOS.get(chave) or ASSUNTOS["inicio"]
    embed = discord.Embed(title=pagina["titulo"], description=pagina["descricao"], color=_COR)
    for nome, valor in pagina["campos"]:
        embed.add_field(name=nome, value=valor, inline=False)
    embed.set_footer(text=pagina.get("rodape") or "Troca de assunto no menu aí embaixo 👇")
    return embed


class HelpView(discord.ui.View):
    """O menu de assuntos: troca a página na mesma mensagem (só quem abriu vê)."""

    def __init__(self, chave: str = "inicio") -> None:
        super().__init__(timeout=300, disable_on_timeout=True)
        self._menu = discord.ui.Select(placeholder="📚 Escolhe o assunto…")
        self._menu.callback = self._escolheu
        self.add_item(self._menu)
        self._marca(chave)

    def _marca(self, chave: str) -> None:
        self._menu.options = [
            discord.SelectOption(
                label=pagina["rotulo"], description=pagina["resumo"], value=outra, default=outra == chave
            )
            for outra, pagina in ASSUNTOS.items()
        ]

    async def _escolheu(self, interaction: discord.Interaction) -> None:
        chave = self._menu.values[0]
        self._marca(chave)
        await interaction.response.edit_message(embed=monta_embed(chave), view=self)


class Help(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="help", description="O guia do Arrodes, por assunto — comandos, voz, fila, painel")
    async def help(
        self,
        ctx: discord.ApplicationContext,
        assunto: discord.Option(
            str,
            "Abre direto num assunto (sem escolher, começa pelo início)",
            choices=[pagina["rotulo"] for pagina in ASSUNTOS.values()],
            required=False,
        ),
    ) -> None:
        chave = _POR_ROTULO.get(assunto, "inicio")
        await ctx.respond(embed=monta_embed(chave), view=HelpView(chave), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Help(bot))
