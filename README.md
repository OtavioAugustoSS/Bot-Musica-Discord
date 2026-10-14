# 🎵 Arrodes

Bot de música para Discord com comandos de barra e **comandos de voz** na
call: "Arrodes, toca …".

Painel de botões, importação de playlists do Spotify e do YouTube, busca
interativa, vote-skip, seek e volume, favoritos, ranking e autoplay (rádio
infinita).

O áudio vem do YouTube (`yt-dlp` + FFmpeg). O Spotify entra só como fonte de
**metadados**: você cola um link de playlist e o bot importa a fila inteira
com títulos e capas certos, resolvendo o áudio no YouTube na hora de tocar.

## Comandos

| Comando | O que faz |
| --- | --- |
| `/play <busca>` | Toca por nome (dropdown com 5 opções), link de vídeo ou **playlist do YouTube**, ou link do Spotify (faixa/álbum/playlist). `modo:substituir` troca a fila sem sair da call |
| `/pause` · `/resume` | Pausa / retoma |
| `/skip` | Pula (quem pediu, admin ou sozinho pula direto; os outros votam — maioria dos ouvintes) |
| `/stop` | Para tudo, limpa a fila e sai do canal |
| `/seek <posição>` | Vai para um ponto da música: `1:30` · `90` · `+30` · `-10` |
| `/volume [nível]` | 0 a 150 (100 = original), salvo por servidor; sem número mostra o atual |
| `/queue` | Fila por páginas, com duração, tempo total e menu para tirar música; a rádio do autoplay aparece em bloco próprio |
| `/shuffle` · `/loop` | Embaralha · alterna loop (off → faixa → fila) |
| `/remove <posição>` | Remove uma música da fila |
| `/clear` | Esvazia a fila sem parar a música atual |
| `/autoplay` | Liga/desliga a rádio infinita (relacionadas via YouTube Music) — só entra quando a fila acaba |
| `/fav` · `/favs` · `/playfavs` | Favorita a atual · lista/toca um favorito · toca a playlist de favoritos inteira |
| `/history` · `/replay` | Últimas 10 que você pediu · toca de novo a última |
| `/top` | Ranking do servidor: músicas e DJs mais tocados (semana, mês, sempre) |
| `/ouvir` | Modo de voz: o bot escuta a call e obedece "Arrodes, toca…", "pula", "pausa", "limpa a fila", "sai da call"… |
| `/diag` | Diagnóstico da extração (yt-dlp, runtime JS, cookies, perfil ativo, teste real) |
| `/help` | Guia por assunto dentro do Discord (`assunto:` abre direto num deles) |

Com mais gente na call, `/stop`, `/clear`, `/shuffle` e tirar música dos
outros ficam com quem tem **Gerenciar servidor**. Sozinho com o bot, pode tudo.

Enquanto toca, o bot mantém um **painel fixo** no chat, estilo Spotify, com
capa, prévia da fila e botões: 🔀 ⏸/▶ ⏭ 🔁 ⭐ 🎲 🚪. Ele desce para o fim do
chat sozinho quando a conversa anda, e mostra "preparando…" enquanto extrai
uma faixa fria.

## Modo de voz

`/ouvir` liga (e desliga) a escuta. O caminho de cada fala:

1. **Transcrição** pelo Whisper da Groq: `whisper-large-v3-turbo` (~0,4s), com
   o `whisper-large-v3` assumindo quando a cota do turbo (do dia ou do minuto)
   acaba. Sem Groq, ou com ela fora, cai no `faster-whisper` local.
2. **Wake word** (`src/core/intents.py`), procurada nas primeiras palavras: as
   grafias que o Whisper inventa ("A Rodes", "Herodes", "Rhodes", "Harrods"…)
   ficam numa lista, o resto passa por um fuzzy de 0,82, e palavras reais
   parecidas ("arrotes", "arrozes") ficam numa blocklist.
3. **2ª escuta.** O nome vai no prompt do Whisper para sair escrito certo — e
   por isso o Whisper às vezes o "ouve" onde ninguém disse. Toda chamada é
   conferida retranscrevendo o mesmo áudio sem o nome no prompt; se o nome (ou
   o mesmo comando) não aparece de novo, a frase é ignorada.
4. **Comando.** A gramática fixa entende os pedidos comuns; frase livre ("bota
   algo mais animado", "aquela do Frank Ocean") vai para um LLM da Groq:
   `openai/gpt-oss-20b` → `qwen/qwen3.8-27b` → `openai/gpt-oss-120b`. A Groq
   aposenta modelos sem aviso: 404 num deles pula para o próximo, e
   `GROQ_LLM_MODEL` no `.env` passa na frente da lista.

Quem diz só "Arrodes" tem alguns segundos para completar o comando. Pedido
por voz entra como a **próxima** da fila. Exemplos: "Arrodes, toca Djavan",
"Arrodes, pula 3", "Arrodes, volume 50", "Arrodes, avança 30 segundos",
"Arrodes, toca a terceira da fila". A cola completa está no `/help` → 🎙️ Voz.

Ajustes por `.env` (os defaults funcionam bem):

| Variável | Padrão | O que faz |
| --- | --- | --- |
| `VOICE_GAP_S` | `0.8` | Silêncio (s) até fechar a frase |
| `VOICE_MIN_RMS` | `250` | Energia mínima para um trecho contar como fala |
| `VOICE_TETO_S` | `6` | Trecho mais longo enviado à Groq (comando nenhum passa disso) |
| `GROQ_STT_RPM` | `20` | Teto por minuto do Whisper; perto dele os trechos longos ficam de fora |
| `VOICE_STT_PROMPT` | vocabulário | Bias do Whisper; vazio desliga |
| `VOICE_SOM_ESCUTA` | ligado | Estalo de "te ouvi" quando alguém chama; `0` desliga |
| `VOICE_WHISPER_LOCAL` | ligado | Fallback local quando a Groq falha; `0` desliga (use em VM pequena) |
| `WHISPER_MODEL` | `tiny` | Modelo do `faster-whisper` local |
| `VOICE_DEBUG` · `VOICE_COLETA` | desligados | Gravam voz em disco — veja abaixo |

> **Privacidade.** Com `/ouvir` ligado, toda fala da call vai para a Groq — é
> assim que o bot acha o "Arrodes". O log guarda só a frase dirigida ao bot e
> a primeira palavra das outras. `VOICE_DEBUG=1` grava WAVs em
> `data/voice-debug/` e o texto transcrito no log; `VOICE_COLETA=1` guarda em
> `data/wake-amostras/` as chamadas confirmadas. As duas põem a voz das
> pessoas em disco: deixe desligadas em produção, a menos que esteja coletando
> com o consentimento de quem está na call, e apague depois.

A recepção de voz do py-cord 2.8.1 está quebrada upstream (issue #3139,
DAVE/E2EE); `src/core/pycord_voice_patch.py` corrige o que falta. Remova o
patch quando o py-cord fechar o issue.

## Setup do zero

### 1. App no Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application** (o nome dela é o nome do bot).
2. **Bot**: **Reset Token** e guarde (vai no `DISCORD_TOKEN`). **Public Bot**
   ligado, para outras pessoas poderem convidar. Os três **Privileged Gateway
   Intents** desligados: o bot usa `Intents.default()` e só slash commands,
   não lê mensagens.
3. **General Information** → copie o **Application ID** e monte o convite:

   ```
   https://discord.com/oauth2/authorize?client_id=SEU_CLIENT_ID&scope=bot+applications.commands&permissions=36719616
   ```

   `36719616` = View Channels, Send Messages, Embed Links, Connect, Speak e
   Use Voice Activity. Nada de administrador.

### 2. Conta na Groq

[console.groq.com](https://console.groq.com) → **API Keys** → `GROQ_API_KEY`.
Use uma conta **sua**: os limites são por organização — 2000 requisições por
dia **por modelo** de Whisper e 20 por minuto — e dividir a chave com outro bot
divide a cota. Com `/ouvir` ligado toda fala da call vira requisição (quase
nenhuma é comando): numa call animada o dia do turbo acaba em poucas horas e o
bot passa para o `large-v3`.

Sem Groq o bot toca normalmente; a voz fica com o Whisper local (lento e fraco
com português + inglês misturado) e sem o LLM.

### 3. App no Spotify (opcional)

[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) →
**Create app**, Redirect URI `http://127.0.0.1:8888/callback`, API **Web API**
→ Client ID e Client Secret no `.env`. Sem elas os links do Spotify não
funcionam (o resto sim).

Com elas: faixa, álbum e playlist pública (até 100 faixas, pela prévia
pública). Playlist **privada** pede login de usuário: `python
src/spotify_login.py` uma vez no PC (abre o navegador) cria
`data/spotify-token.json`, que renova sozinho — em produção, copie o arquivo
para a VM.

### 4. Cookies do YouTube (conta descartável)

Em IP de datacenter o YouTube exige login ("Sign in to confirm you're not a
bot"); em casa normalmente não precisa. O login vai num `data/cookies.txt`
(formato Netscape):

1. Crie uma conta Google **só para isso** — nunca a pessoal: conta usada por
   bot pode ser bloqueada.
2. Abra uma janela anônima e entre no YouTube com ela.
3. Na mesma aba, abra `https://www.youtube.com/robots.txt` — assim nenhuma
   página do YouTube gira os cookies depois da exportação.
4. Exporte os cookies de `youtube.com` em formato Netscape (extensão como
   *Get cookies.txt LOCALLY*) e salve como `data/cookies.txt`.
5. Feche a janela anônima **sem sair da conta** (sair invalida a sessão).

O bot relê o arquivo quando ele muda — trocar não exige reinício. `python
src/doctor.py` (ou `/diag`) mostra se os cookies têm login e qual perfil
extrai. Quando a conta é invalidada, o yt-dlp reescreve o arquivo sem a sessão
e o bot avisa o dono: repita o procedimento.

### 5. `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| Variável | Para quê |
| --- | --- |
| `DISCORD_TOKEN` | Obrigatório |
| `GROQ_API_KEY` | Transcrição na nuvem e o LLM do modo de voz |
| `SPOTIFY_CLIENT_ID` · `SPOTIFY_CLIENT_SECRET` | Links do Spotify |
| `GUILD_ID` | Dev: ID do seu servidor — os comandos aparecem na hora, mas **só nele**. Produção: vazio |
| `ALERT_CHANNEL_ID` | Canal dos avisos ao dono quando a DM é recusada |

O resto tem default e está comentado no `.env.example` e em `src/config.py`.

### 6. Rodar local

Pré-requisitos: **Python 3.12+** (o numpy 2.5 exige), **FFmpeg** e um runtime
de JS para o yt-dlp (**deno** ou **node**) no PATH — sem runtime o áudio cai
para o formato 18 (mp4 com vídeo junto).

- Windows: `winget install Gyan.FFmpeg` e `winget install DenoLand.Deno`
- Ubuntu: `sudo apt install ffmpeg` + `curl -fsSL https://deno.land/install.sh | sh`
- macOS: `brew install ffmpeg deno`

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt       # Windows: .venv\Scripts\pip
.venv/bin/python src/main.py                    # Windows: .venv\Scripts\python src\main.py
```

Convide o bot pela URL do passo 1, entre numa call e mande `/play`.

## Produção: VM Always Free da Oracle

A **VM.Standard.E2.1.Micro** (x86, 1/8 de OCPU com burst, 1 GB de RAM, 10 TB
de saída por mês) aguenta o bot 24/7 de graça: com 2 GB de swap e a unit do
systemd segurando memória e prioridade do áudio, sobra folga. Lá a extração
custa ~8s por faixa (é CPU: o desafio JS do player do YouTube); o bot compensa
com prefetch das próximas faixas, cache de URLs que sobrevive a reinícios e
um pool de extração com prioridade (quem vai tocar agora passa na frente).

### Por que não Render/Koyeb grátis

- **0,1 de CPU.** Menos que o custo de tocar: extrair uma faixa já leva ~8s de
  CPU cheia, e o áudio precisa de um pacote a cada 20ms. A música engasga.
- **IP compartilhado.** O YouTube bloqueia IPs de datacenter compartilhados, e
  o Discord (via Cloudflare) limita por IP — com centenas de apps no mesmo IP,
  o rate limit chega antes do seu primeiro comando.
- **Disco efêmero.** Favoritos, histórico, ranking (`data/bot.db`), cookies e
  cache somem a cada deploy ou reinício.
- **Banda.** O Render grátis dá 5 GB/mês de saída; o áudio para o Discord gasta
  ~58 MB por hora — menos de 3h de música por dia.

### 1. Criar a instância

1. Conta em [cloud.oracle.com](https://cloud.oracle.com) (Always Free; o cartão
   é só verificação). Os recursos grátis ficam na região escolhida no cadastro:
   escolha a mais perto de você.
2. **Compute → Instances → Create instance**: imagem **Canonical Ubuntu
   24.04**, shape **VM.Standard.E2.1.Micro** (*Always Free-eligible*), IP
   público ligado.
3. **Add SSH keys** → cole a sua chave pública. Sem uma, gere com
   `ssh-keygen -t ed25519 -f ~/.ssh/arrodes` e envie o `~/.ssh/arrodes.pub`.
4. Anote o IP público. Nenhuma porta além do SSH: o bot só faz conexões de
   saída.

### 2. Alias ssh

No `~/.ssh/config` (todos os scripts usam o alias `arrodes`):

```
Host arrodes
    HostName SEU.IP.PUBLICO
    User ubuntu
    IdentityFile ~/.ssh/arrodes
```

Teste com `ssh arrodes`.

### 3. Provisionar

```bash
ssh arrodes 'bash -s' < scripts/provisionar_vm.sh
```

Instala os pacotes (Python, FFmpeg, libopus), o deno, a venv em
`~/arrodes`, 2 GB de swap e a unit systemd `arrodes`. Pode rodar de novo. Não
sobe o bot: falta o `.env`.

### 4. `.env` e cookies

```bash
scp .env arrodes:~/arrodes/.env
scp data/cookies.txt arrodes:~/arrodes/data/cookies.txt
scp data/spotify-token.json arrodes:~/arrodes/data/   # só se fez o spotify_login
```

No `.env` da VM: `GUILD_ID` vazio (comandos globais) e `VOICE_WHISPER_LOCAL=0`
(nessa máquina o Whisper local leva 83–131s por frase). Opcional:
`AUDIO_OPUS_DIRETO=1` manda o Opus do YouTube direto ao Discord — a CPU do
áudio cai de 118 para 17 ms por segundo, e o volume passa a ser o original.

### 5. Deploy

Do Git Bash (Windows) ou de qualquer shell Linux/macOS, na raiz do repositório:

```bash
ARRODES_HOST=arrodes bash scripts/deploy.sh
```

Antes de enviar, roda pytest + ruff local (a venv precisa do
`requirements-dev.txt`). Na VM: guarda a versão anterior, instala as
dependências, a unit e o canário do yt-dlp, roda `src/doctor.py`, reinicia e
**só termina bem quando a sonda de extração do bot passa** — se o código novo
não sobe, volta sozinho à versão anterior. `.env`, `data/bot.db` e
`data/cookies.txt` são estado da VM e nunca são tocados.

Bot numa call: o deploy fica **agendado** na VM e entra sozinho quando ele sai.
`ARRODES_FORCAR=1` reinicia assim mesmo; `ARRODES_SEM_TESTES=1` pula a suíte
(emergência). Opcionais: `ARRODES_DIR` (padrão `/home/ubuntu/arrodes`),
`ARRODES_SERVICE` (padrão `arrodes`), `ARRODES_KEY` (chave ssh fora do alias).

No primeiro deploy, ligue o serviço no boot da VM:

```bash
ssh arrodes sudo systemctl enable arrodes
ssh arrodes journalctl -u arrodes -f      # acompanhar
```

### Comandos globais

Com `GUILD_ID` vazio os comandos valem em todos os servidores; a primeira
propagação demora até 1h. Se você usou `GUILD_ID` no dev, os comandos daquele
servidor continuam registrados e aparecem **duplicados** — apague-os:

```bash
ssh arrodes 'cd ~/arrodes && .venv/bin/python src/migrar_para_global.py --apagar'
```

(`--listar` mostra antes o que vai sair.)

## Calibrar a wake word

"Arrodes" não é palavra do português, e cada voz e microfone fazem o Whisper
escrever de um jeito. Para afinar a detecção:

1. Rode **local** com `VOICE_DEBUG=1` no `.env`, entre numa call e ligue o
   `/ouvir`.
2. Diga "Arrodes" umas 20 vezes, com pausas — sozinho e com comando ("Arrodes,
   pula"). Diga também negativos, que **não** devem chamar o bot: "arroz", "a
   rede", "acordes". O `VOICE_DEBUG` guarda só os 10 trechos mais recentes:
   fale em lotes de até 10 e rode o passo 3 depois de cada lote.
3. `python scripts/calibra_wake.py` — repassa os WAVs de `data/voice-debug/`
   pelos dois modelos de Whisper da Groq, com e sem o nome no prompt, e sugere
   variantes (`_WAKE_VARIANTS`) e blocklist (`_WAKE_BLOCKLIST`) para
   `src/core/intents.py`.
4. Aplique o que fizer sentido, rode os testes e **apague `data/voice-debug/`**.

Não suba o `_WAKE_THRESHOLD` acima de 0,833: "A Rodes" vira "rodes" quando o
"a" cai, e "rodes" mede exatamente isso.

Em produção, sem gravar ninguém, o journal já mostra quem chamou e não foi
atendido:

```bash
ssh arrodes 'journalctl -u arrodes --since "7 days ago" -o short-iso --no-pager' > journal.log
python scripts/analise_journal.py journal.log
```

O bloco "sem wake word" é o histograma das primeiras palavras das falas que
não chamaram o bot: uma grafia nova do nome que se repete ali é candidata a
variante.

## Avisos ao dono

O bot manda DM ao dono do app (vem da API do Discord, nada a configurar)
quando: reinicia sem desligamento limpo (com os últimos erros), a sonda de
extração do boot falha, os cookies do YouTube perdem o login, ou sai um
yt-dlp novo. Cada aviso sai no máximo uma vez por janela. Se a sua conta
não aceita DM do bot (403), o aviso vai para o canal `ALERT_CHANNEL_ID` do
`.env` — ou, sem ele, para o canal de sistema do servidor — mencionando você.

Na VM, o **canário do yt-dlp** roda todo dia (e quando o bot conta sintomas
demais): se a produção quebrou e o nightly do yt-dlp passa no diagnóstico, ele
promove a versão nova, reinicia, confere a sonda e avisa o dono do que fez.

## Quando o áudio para de tocar ("Falha ao tocar … — pulando")

O YouTube quebra o `yt-dlp` de tempos em tempos, e o sintoma é sempre o mesmo:
toda faixa da fila é pulada. Antes de mexer em qualquer coisa, rode o
diagnóstico — ele testa cada perfil de extração no IP da máquina e passa a
URL do vencedor pelo FFmpeg (extrair não é o mesmo que tocar):

```bash
ssh arrodes 'cd ~/arrodes && .venv/bin/python src/doctor.py'   # o resumo também sai no /diag
```

O bot mantém uma **cadeia de perfis** (`src/services/youtube.py`): tenta o que
funcionou por último e, se falhar, percorre os outros — uma quebra costuma se
resolver sozinha. Se **nenhum** perfil passa, a ordem de investigação é:

1. **yt-dlp velho** — `.venv/bin/pip install -U 'yt-dlp[default]'`, depois fixe
   a versão nova em `requirements.txt`. É a causa mais comum.
2. **Sem runtime de JS** — o `doctor` avisa.
3. **Cookies** — a *simples existência* de `data/cookies.txt` troca os clients
   do yt-dlp pelos autenticados. Foi isso que derrubou o bot em agosto/2026
   (`tv_downgraded` respondendo "The page needs to be reloaded", yt-dlp#17389).
   Renove os cookies (conta descartável) **ou** remova o arquivo e teste sem ele.

## Testes

```bash
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest
```

A suíte roda sem rede, sem Discord e sem FFmpeg. `tests/test_static.py` roda
o ruff (nomes indefinidos); o GitHub Actions (`.github/workflows/tests.yml`)
repete tudo a cada push.

## Estrutura

```
src/
├── main.py                 # entrypoint, sonda do boot, avisos ao dono
├── config.py               # .env (cada variável comentada)
├── messages.py             # TODOS os textos do bot
├── cogs/                   # comandos: music, favorites, stats, voice, diag, help, lifecycle, registro
├── core/                   # player, áudio, fila (pedidos + rádio), painel, autoplay, escuta de voz, intents
├── services/               # youtube (yt-dlp/ytmusicapi), spotify, speech (Groq/Whisper), llm, alerts
├── db/                     # SQLite (favoritos, histórico, ranking)
├── doctor.py               # diagnóstico da extração, perfil por perfil
├── migrar_para_global.py   # apaga os comandos de guild ao virar globais
└── spotify_login.py        # login único do Spotify (playlists privadas)
scripts/
├── provisionar_vm.sh       # prepara a VM Ubuntu zerada
├── deploy.sh               # testes → pacote → VM, com sonda e volta automática
├── arrodes.service         # unit systemd (watchdog, tetos de memória, prioridade do áudio)
├── canario_ytdlp.sh        # auto-conserto do yt-dlp (+ arrodes-canario.{service,timer,path})
├── calibra_wake.py         # calibração da wake word com os WAVs do VOICE_DEBUG
├── analise_journal.py      # estudo do journal: voz, erros, saúde, faixas
└── migrar_vm.sh            # muda o bot de uma VM para outra sem dois no ar
```
