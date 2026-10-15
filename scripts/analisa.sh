#!/usr/bin/env bash
# O ciclo de melhoria contínua do Arrodes num comando: baixa o journal da VM e
# roda a análise (chamadas de voz perdidas e como o nome saiu escrito, 2ª escuta,
# atraso dos comandos, áudio realinhado, faixas, erros, saúde).
#
#   ARRODES_HOST=arrodes bash scripts/analisa.sh               # desde o último reinício
#   ARRODES_HOST=arrodes bash scripts/analisa.sh "7 days ago"  # janela maior (atravessa deploys)
#
# O journal fica em logs/journal-AAAAMMDD-HHMM.log (fora do git): é o "antes"
# para comparar com o "depois" de uma mudança. Privacidade: sem VOICE_DEBUG o
# journal só tem o começo das frases que não chamaram o bot, nunca o resto.
set -euo pipefail

HOST="${ARRODES_HOST:-}"
SERVICE="${ARRODES_SERVICE:-arrodes}"
SSH_KEY="${ARRODES_KEY:-}"

if [[ -z "$HOST" ]]; then
  echo "Falta ARRODES_HOST. Ex.: ARRODES_HOST=arrodes $0  (alias do ~/.ssh/config ou ubuntu@IP)" >&2
  exit 2
fi

SSH=(ssh)
if [[ -n "$SSH_KEY" ]]; then SSH+=(-i "$SSH_KEY"); fi

cd "$(dirname "$0")/.."
mkdir -p logs
SAIDA="logs/journal-$(date +%Y%m%d-%H%M).log"

if [[ $# -gt 0 ]]; then
  FILTRO="--since '$1'"
else
  # só a execução atual: o journal de antes do último deploy é de outro código
  FILTRO="_SYSTEMD_INVOCATION_ID=\$(systemctl show -p InvocationID --value $SERVICE)"
fi

# a saída do ssh vai para ARQUIVO: canalizada direto para outro programa ela
# trava no Git Bash do Windows
"${SSH[@]}" "$HOST" "journalctl -u $SERVICE $FILTRO -o short-iso --no-pager" > "$SAIDA"
echo "==> $(wc -l < "$SAIDA") linhas em $SAIDA"

PY=".venv/bin/python"; [[ -x "$PY" ]] || PY=".venv/Scripts/python.exe"
"$PY" scripts/analise_journal.py "$SAIDA"
