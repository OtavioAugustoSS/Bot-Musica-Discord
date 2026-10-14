#!/usr/bin/env bash
# Deploy AGENDADO: espera o bot sair da call e aí aplica o pacote.
#
# O scripts/deploy.sh sobe isto como serviço transitório do systemd
# (arrodes-deploy-pendente) quando o bot está em uso: o código novo já fica na
# VM e entra no ar sozinho assim que ninguém estiver ouvindo — sem depender do PC
# de quem mandou. Pedido do Otavio em 28/09/2026: "quando eu tirar ele da call e
# ele voltar, já vai estar atualizado".
#
#   journalctl -u arrodes-deploy-pendente -f     # acompanhar
#   sudo systemctl stop arrodes-deploy-pendente  # desistir
set -uo pipefail
PENDENTE="$1"  # diretório com pacote.tar.gz e os scripts deste deploy
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/arrodes}"
SERVICE="${SERVICE:-arrodes}"
INTERVALO_S=15
DESISTE_EM_S=$((48 * 3600))

# Estado, não eventos: a última "bot na voz:" DESTA execução do serviço (o
# InvocationID; um --since de horas deixaria de fora quem entrou antes) e FFmpeg
# vivo. Bot sem linha nenhuma nesta execução = subiu e ninguém chamou.
ocioso() {
  local id ultima
  id="$(systemctl show -p InvocationID --value "$SERVICE")"
  # -r -n 1: lê do fim e para na primeira (0,025s). O grep no journal inteiro
  # custava 1,1s de CPU a cada 15s — 2,5min por hora numa VM de ~0,4 núcleo
  ultima="$(journalctl -u "$SERVICE" _SYSTEMD_INVOCATION_ID="$id" -r -n 1 -g "bot na voz:" -o cat 2>/dev/null || true)"
  case "$ultima" in
    ""|*"→ fora") ;;
    *) return 1 ;;
  esac
  ! pgrep -x ffmpeg >/dev/null
}

echo "deploy pendente: esperando o bot ficar ocioso (confere a cada ${INTERVALO_S}s)"
inicio="$(date +%s)"
seguidas=0
while :; do
  if ocioso; then
    seguidas=$((seguidas + 1))
  else
    seguidas=0
  fi
  # duas conferências seguidas: um "sai e volta" rápido não pega o reinício no meio
  if [[ "$seguidas" -ge 2 ]]; then
    break
  fi
  if (( $(date +%s) - inicio > DESISTE_EM_S )); then
    echo "deploy pendente: 48h sem o bot ficar ocioso — desisti (o pacote segue em $PENDENTE)" >&2
    exit 1
  fi
  sleep "$INTERVALO_S"
done

echo "deploy pendente: bot ocioso — aplicando"
REMOTE_DIR="$REMOTE_DIR" SERVICE="$SERVICE" bash "$PENDENTE/aplica_deploy.sh" "$PENDENTE/pacote.tar.gz"
codigo=$?
echo "deploy pendente: terminou com código $codigo"
rm -rf "$PENDENTE"
exit "$codigo"
