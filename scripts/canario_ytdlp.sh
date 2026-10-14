#!/usr/bin/env bash
# Canário do yt-dlp — o auto-conserto do Arrodes contra mudanças do YouTube.
#
# Roda NA VM, pelo systemd: todo dia (arrodes-canario.timer) e quando o bot
# pede (arrodes-canario.path: o bot cria data/canario-pedido ao contar sintomas
# demais em 1h — services/alerts.Sintomas).
#
# O que faz, em ordem:
#   1. roda o doctor com o yt-dlp da PRODUÇÃO (extrai E toca pelo FFmpeg). Passou:
#      fim, sem barulho. Falhou: espera 60s e tenta de novo (YouTube oscila).
#   2. produção quebrada duas vezes: instala o NIGHTLY do yt-dlp num diretório à
#      parte (data/canario-ytdlp, `pip --target`: só o yt-dlp e as dependências
#      dele, ~30 MB — o resto vem da .venv de sempre) e roda o mesmo doctor com ele.
#   3. nightly também falha, ou não há versão nova: só AVISA o dono.
#   4. nightly passa: PROMOVE — instala essa versão na .venv, limpa o cache do
#      yt-dlp, reinicia o bot e espera a sonda do boot. Sonda falhou: VOLTA à
#      versão anterior e reinicia de novo. Avisa o dono do que aconteceu.
#
# Promoção automática SÓ com a produção quebrada e o canário aprovado (decisão do
# Otavio, 20/09/2026). Produção saudável nunca é mexida.
#
# O aviso ao dono é um .txt em data/avisos/: o bot entrega (alerts.entrega_avisos).
#
#   CANARIO_SIMULA=1   faz tudo menos promover/reiniciar (diz o que faria)
#   CANARIO_FORCA=1    trata a produção como quebrada (para ensaiar o caminho todo)
set -uo pipefail

DIR="${ARRODES_DIR:-$HOME/arrodes}"
SERVICE="${ARRODES_SERVICE:-arrodes}"
SIMULA="${CANARIO_SIMULA:-0}"
FORCA="${CANARIO_FORCA:-0}"
cd "$DIR" || exit 1

PY=".venv/bin/python"
ALVO="$DIR/data/canario-ytdlp"
AVISOS="$DIR/data/avisos"
PEDIDO="$DIR/data/canario-pedido"
ULTIMO="$DIR/data/canario-ultimo"  # quando foi a última rodada A PEDIDO
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

diz() { echo "[canario] $*"; }

avisa() {  # avisa <texto> — vira DM do bot ao dono
  mkdir -p "$AVISOS"
  printf '%s\n' "$1" > "$AVISOS/$(date +%Y%m%d-%H%M%S)-canario.txt"
  diz "aviso ao dono: $1"
}

# quem chamou? o pedido do bot some já: senão o .path dispara em laço
a_pedido=0
if [[ -e "$PEDIDO" ]]; then
  a_pedido=1
  diz "a pedido do bot: $(head -c 100 "$PEDIDO" | tr '\n' ' ')"
  rm -f "$PEDIDO"
  # os sintomas podem acender de hora em hora por dias: uma rodada a pedido a
  # cada 6h basta (a diária roda sempre) — senão o dono recebe o mesmo "está
  # quebrado e o nightly também falha" várias vezes por noite
  agora="$(date +%s)"
  antes="$(cat "$ULTIMO" 2>/dev/null || echo 0)"
  if [[ "$FORCA" != 1 && $(( agora - antes )) -lt 21600 ]]; then
    diz "já rodei a pedido há $(( (agora - antes) / 60 ))min — esta fica"
    exit 0
  fi
  echo "$agora" > "$ULTIMO"
fi

# Na rodada DIÁRIA o bot tem de estar ocioso: o doctor são ~3 extrações (~15s de
# CPU) numa VM que engasga o áudio acima de ~20%. A pedido roda sempre — se o bot
# pediu é porque a música já não está saindo. Por ESTADO, como no deploy.sh.
if [[ "$a_pedido" == 0 && "$FORCA" != 1 ]]; then
  ultima="$(journalctl -u "$SERVICE" --since "-12h" -o cat | grep -a "bot na voz:" | tail -1 || true)"
  ffmpegs="$(pgrep -c ffmpeg || true)"
  case "$ultima" in
    ""|*"→ fora") ;;
    *) diz "bot numa call — fica para a próxima rodada"; exit 0 ;;
  esac
  if [[ "${ffmpegs:-0}" != 0 ]]; then
    diz "FFmpeg vivo (${ffmpegs}) — fica para a próxima rodada"; exit 0
  fi
fi

doctor() {  # doctor <arquivo de saída> [PYTHONPATH extra] → código de saída do doctor
  local saida="$1" extra="${2:-}"
  if [[ -n "$extra" ]]; then
    PYTHONPATH="$extra" nice -n 10 "$PY" src/doctor.py > "$saida" 2>&1
  else
    nice -n 10 "$PY" src/doctor.py > "$saida" 2>&1
  fi
}

motivo() {  # as linhas de FALHA do doctor, numa só
  grep -a "FALHA" "$1" | head -3 | sed 's/^\[FALHA\] *//' | tr '\n' ';' | cut -c1-300
}

versao() {  # versao [PYTHONPATH extra]
  if [[ -n "${1:-}" ]]; then
    PYTHONPATH="$1" "$PY" -c "import yt_dlp.version as v; print(v.__version__)" 2>/dev/null
  else
    "$PY" -c "import yt_dlp.version as v; print(v.__version__)" 2>/dev/null
  fi
}

PROD="$(versao)"
diz "produção: yt-dlp ${PROD:-?}"

# ── 1. a produção funciona? ──────────────────────────────────────────────────
rc=0
doctor "$TMP/prod1.txt" || rc=$?
if [[ "$rc" == 0 && "$FORCA" != 1 ]]; then
  diz "produção ok (extrai e toca) — nada a fazer"
  exit 0
fi
if [[ "$FORCA" != 1 ]]; then
  diz "produção FALHOU (código $rc): $(motivo "$TMP/prod1.txt") — de novo em 60s"
  sleep 60
  rc=0
  doctor "$TMP/prod2.txt" || rc=$?
  if [[ "$rc" == 0 ]]; then
    diz "na segunda tentativa passou — era oscilação do YouTube"
    exit 0
  fi
  PORQUE="$(motivo "$TMP/prod2.txt")"
else
  PORQUE="(ensaio: CANARIO_FORCA=1)"
fi
diz "produção quebrada: $PORQUE"

# ── 2. o nightly resolve? ────────────────────────────────────────────────────
diz "instalando o nightly do yt-dlp em $ALVO"
if ! "$PY" -m pip install --quiet --disable-pip-version-check --upgrade --pre \
      --target "$ALVO" "yt-dlp[default]" > "$TMP/pip.txt" 2>&1; then
  avisa "🐤 Canário do yt-dlp: a produção (yt-dlp $PROD) está QUEBRADA — $PORQUE — e não consegui nem baixar o nightly: $(tail -1 "$TMP/pip.txt" | cut -c1-200)"
  exit 1
fi
NOVA="$(versao "$ALVO")"
diz "canário: yt-dlp ${NOVA:-?}"
if [[ -z "$NOVA" || "$NOVA" == "$PROD" ]]; then
  avisa "🐤 Canário do yt-dlp: a produção (yt-dlp $PROD) está QUEBRADA — $PORQUE — e não existe versão mais nova para tentar. Confere os cookies (data/cookies.txt) e o doctor: \`.venv/bin/python src/doctor.py\`."
  exit 1
fi

rc=0
doctor "$TMP/canario.txt" "$ALVO" || rc=$?
if [[ "$rc" != 0 ]]; then
  avisa "🐤 Canário do yt-dlp: a produção ($PROD) está QUEBRADA — $PORQUE — e o nightly ($NOVA) TAMBÉM falha: $(motivo "$TMP/canario.txt"). Não mexi em nada."
  exit 1
fi
diz "o nightly $NOVA extrai e toca"

if [[ "$SIMULA" == 1 ]]; then
  diz "SIMULAÇÃO: aqui eu promoveria $PROD → $NOVA e reiniciaria o $SERVICE"
  exit 0
fi

# ── 3. promove, com volta ────────────────────────────────────────────────────
instala() {  # instala <versão> na .venv de produção
  "$PY" -m pip install --quiet --disable-pip-version-check --pre "yt-dlp[default]==$1" > "$TMP/pip-prod.txt" 2>&1
}

sobe_e_confere() {  # reinicia e espera a sonda do boot: 0 = extração ok
  local desde
  desde="$(date +%s)"
  sudo -n systemctl restart "$SERVICE" || return 1
  for _ in $(seq 1 45); do
    sleep 2
    sonda="$(journalctl -u "$SERVICE" --since "@$desde" --no-pager -o cat \
      | grep -a -m1 -E "extração de áudio ok|extração de áudio NÃO está funcionando" || true)"
    case "$sonda" in
      *"extração de áudio ok"*) return 0 ;;
      *"NÃO está funcionando"*) return 1 ;;
    esac
  done
  return 1
}

diz "PROMOVENDO $PROD → $NOVA"
if ! instala "$NOVA"; then
  avisa "🐤 Canário do yt-dlp: a produção ($PROD) está QUEBRADA e o nightly ($NOVA) passa, mas o pip falhou ao instalar na .venv: $(tail -1 "$TMP/pip-prod.txt" | cut -c1-200). Não reiniciei."
  exit 1
fi
rm -rf "$HOME/.cache/yt-dlp"
if sobe_e_confere; then
  avisa "🐤 Canário do yt-dlp: a produção estava QUEBRADA — $PORQUE. Promovi o yt-dlp $PROD → $NOVA, reiniciei o bot e a sonda de extração passou. Está tocando de novo."
  exit 0
fi

diz "a sonda falhou com $NOVA — VOLTANDO para $PROD"
instala "$PROD" || diz "pip falhou ao voltar para $PROD: $(tail -1 "$TMP/pip-prod.txt")"
rm -rf "$HOME/.cache/yt-dlp"
sobe_e_confere || true
avisa "🐤 Canário do yt-dlp: a produção está QUEBRADA — $PORQUE. Tentei $PROD → $NOVA (o doctor passava), mas a sonda do bot falhou com a versão nova: voltei para $PROD. Precisa de olho humano: \`.venv/bin/python src/doctor.py\`."
exit 1
