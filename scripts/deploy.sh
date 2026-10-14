#!/usr/bin/env bash
# Deploy do Arrodes na VM Oracle.
#
#   ARRODES_HOST=arrodes bash scripts/deploy.sh
#
# ARRODES_HOST é o alias do ~/.ssh/config (ou ubuntu@IP). Roda do Git Bash no
# Windows ou de qualquer Linux. Usa tar (não Compress-Archive: ele grava
# caminhos com "\" e quebra do lado Linux). VM nova: antes do primeiro deploy,
# scripts/provisionar_vm.sh e o .env copiado (o passo a passo está nele).
#
# Em ordem: testes locais → envia o pacote → bot ocioso? aplica NA HORA
# (scripts/aplica_deploy.sh: versão anterior guardada, dependências, units,
# diagnóstico, reinício, sonda do boot; sonda falhou = volta sozinho). Bot em
# uso? AGENDA: o pacote fica na VM e o serviço arrodes-deploy-pendente aplica
# assim que o bot sair da call (scripts/deploy_quando_ocioso.sh) — sem depender
# deste PC. Um deploy agendado novo substitui o anterior.
#
#   ARRODES_FORCAR=1      reinicia mesmo com o bot numa call
#   ARRODES_SEM_TESTES=1  pula a suíte (emergência)
set -euo pipefail

HOST="${ARRODES_HOST:-}"
REMOTE_DIR="${ARRODES_DIR:-/home/ubuntu/arrodes}"
SERVICE="${ARRODES_SERVICE:-arrodes}"
SSH_KEY="${ARRODES_KEY:-}"
FORCAR="${ARRODES_FORCAR:-0}"

if [[ -z "$HOST" ]]; then
  echo "Falta ARRODES_HOST. Ex.: ARRODES_HOST=arrodes $0  (alias do ~/.ssh/config ou ubuntu@IP)" >&2
  exit 2
fi

SSH=(ssh); SCP=(scp)
if [[ -n "$SSH_KEY" ]]; then SSH+=(-i "$SSH_KEY"); SCP+=(-i "$SSH_KEY"); fi

cd "$(dirname "$0")/.."

# Código quebrado não sai daqui. Até 20/09/2026 o deploy não rodava teste
# nenhum: a suíte era um combinado, não uma trava.
if [[ "${ARRODES_SEM_TESTES:-0}" != "1" ]]; then
  PY=".venv/bin/python"; [[ -x "$PY" ]] || PY=".venv/Scripts/python.exe"
  echo "==> testes (pytest + ruff)"
  "$PY" -m pytest -q -x 2>&1 | tail -3
  "$PY" -m ruff check --select F821,F822,F823,E9,F401,F841 src
fi

PACOTE="$(mktemp -t arrodes-XXXXXX).tar.gz"
trap 'rm -f "$PACOTE"' EXIT

echo "==> empacotando (sem .venv, data/, .git)"
tar czf "$PACOTE" \
  --exclude='__pycache__' --exclude='*.pyc' \
  src requirements.txt requirements-dev.txt README.md .env.example \
  scripts/arrodes.service scripts/ajustes_vm.sh scripts/canario_ytdlp.sh \
  scripts/arrodes-canario.service scripts/arrodes-canario.timer scripts/arrodes-canario.path \
  scripts/aplica_deploy.sh scripts/deploy_quando_ocioso.sh

echo "==> enviando para $HOST"
"${SCP[@]}" -q "$PACOTE" "$HOST:/tmp/arrodes-deploy.tar.gz"

# ATENÇÃO: nada aqui toca .env, data/bot.db nem data/cookies.txt — são
# estado da VM. O tar só carrega código.
set +e  # o lado remoto sai com 10 quando AGENDA: é sucesso, não erro
"${SSH[@]}" "$HOST" REMOTE_DIR="$REMOTE_DIR" SERVICE="$SERVICE" FORCAR="$FORCAR" DESTINO="$HOST" 'bash -s' <<'REMOTO'
set -euo pipefail
PENDENTE="$REMOTE_DIR/.deploy-pendente"
AGENDADO="arrodes-deploy-pendente"

# VM nova: sem o provisionar_vm.sh não há .venv; sem .env o bot nem sobe
falta() { rm -f /tmp/arrodes-deploy.tar.gz; printf '%s\n' "$@" >&2; exit 3; }
[[ -x "$REMOTE_DIR/.venv/bin/pip" ]] || falta "VM não provisionada (sem $REMOTE_DIR/.venv). Rode antes:" \
  "  ssh $DESTINO 'bash -s' < scripts/provisionar_vm.sh"
[[ -f "$REMOTE_DIR/.env" ]] || falta "Falta $REMOTE_DIR/.env (DISCORD_TOKEN e cia.). Copie com:" \
  "  scp .env $DESTINO:$REMOTE_DIR/.env"

# um agendado anterior sai: este pacote é mais novo e o substitui
sudo -n systemctl stop "$AGENDADO" 2>/dev/null || true
sudo -n systemctl reset-failed "$AGENDADO" 2>/dev/null || true
rm -rf "$PENDENTE"; mkdir -p "$PENDENTE"
mv /tmp/arrodes-deploy.tar.gz "$PENDENTE/pacote.tar.gz"
tar xzf "$PENDENTE/pacote.tar.gz" -C "$PENDENTE" --strip-components=1 \
  scripts/aplica_deploy.sh scripts/deploy_quando_ocioso.sh

# Reiniciar corta a música de quem está ouvindo. A checagem é por ESTADO: a
# última "bot na voz:" DESTA execução do serviço (InvocationID — um --since de
# 12h deixava de fora quem entrou há mais tempo) e FFmpeg vivo. Contar eventos
# deu falso positivo três vezes em 20/09.
id="$(systemctl show -p InvocationID --value "$SERVICE")"
ultima="$(journalctl -u "$SERVICE" _SYSTEMD_INVOCATION_ID="$id" -r -n 1 -g "bot na voz:" -o cat 2>/dev/null || true)"
case "$ultima" in
  ""|*"→ fora") na_call=0 ;;
  *) na_call=1 ;;
esac
ffmpegs="$(pgrep -c -x ffmpeg || true)"
echo "==> alguém usando o bot? na_call=$na_call ffmpeg=${ffmpegs:-0}"

if [[ "$na_call" == 0 && "${ffmpegs:-0}" == 0 ]] || [[ "$FORCAR" == 1 ]]; then
  codigo=0
  REMOTE_DIR="$REMOTE_DIR" SERVICE="$SERVICE" bash "$PENDENTE/aplica_deploy.sh" "$PENDENTE/pacote.tar.gz" || codigo=$?
  rm -rf "$PENDENTE"
  exit "$codigo"
fi

sudo -n systemd-run --unit="$AGENDADO" --uid="$(id -un)" --gid="$(id -gn)" \
  --setenv=REMOTE_DIR="$REMOTE_DIR" --setenv=SERVICE="$SERVICE" --setenv=USER="$(id -un)" \
  --setenv=HOME="$HOME" --setenv=PATH="$PATH" \
  --working-directory="$REMOTE_DIR" \
  bash "$PENDENTE/deploy_quando_ocioso.sh" "$PENDENTE" >/dev/null
echo "==> o bot está em uso: deploy AGENDADO — entra no ar sozinho quando ele sair da call"
echo "    acompanhar: journalctl -u $AGENDADO -f"
exit 10
REMOTO
codigo=$?
set -e

if [[ "$codigo" == 10 ]]; then
  echo
  echo "Agendado. Tire o bot da call (ou espere todos saírem) e ele volta atualizado."
  exit 0
fi
if [[ "$codigo" == 0 ]]; then
  echo
  echo "Pronto. No Discord, confirme com /diag e um /play."
fi
exit "$codigo"
