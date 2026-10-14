#!/usr/bin/env bash
# Migra o Arrodes da VM atual para uma nova (outra instância, shape ou região).
#
#   ANTIGA=arrodes NOVA=arrodes-nova ./scripts/migrar_vm.sh preparar
#   ANTIGA=arrodes NOVA=arrodes-nova ./scripts/migrar_vm.sh cutover
#
# ANTIGA/NOVA são hosts do ~/.ssh/config (ou ubuntu@ip); ANTIGA vale "arrodes"
# se omitida. Roda do Git Bash.
#
#   preparar: provisiona a nova (swap, pacotes, deno, venv), copia .env,
#             cookies e token do Spotify, e instala o código, as dependências
#             e as units (a do canário desligada) SEM subir o serviço. A
#             antiga continua tocando.
#   cutover:  para o bot na antiga, copia o banco e o cache de streams
#             (consistentes, com o bot parado), sobe o serviço na nova e
#             espera a sonda de extração. Só então liga o canário na nova e
#             desabilita o serviço e o canário na antiga — dois bots com o
#             mesmo token brigam pelo gateway.
#
#   ARRODES_DIR=/home/ubuntu/arrodes  ARRODES_SERVICE=arrodes  (iguais nas duas)
set -euo pipefail

ANTIGA="${ANTIGA:-arrodes}"
NOVA="${NOVA:?defina NOVA=host-ou-ubuntu@ip da VM nova}"
DIR="${ARRODES_DIR:-/home/ubuntu/arrodes}"
SERVICE="${ARRODES_SERVICE:-arrodes}"
ETAPA="${1:-}"
cd "$(dirname "$0")/.."

if [[ "$ANTIGA" == "$NOVA" ]]; then
  echo "ANTIGA e NOVA são o mesmo host ($NOVA)" >&2
  exit 2
fi

preparar() {
  echo "==> provisionando $NOVA"
  ssh "$NOVA" ARRODES_DIR="$DIR" ARRODES_SERVICE="$SERVICE" 'bash -s' < scripts/provisionar_vm.sh

  echo "==> copiando .env, cookies e token do Spotify ($ANTIGA → PC → $NOVA)"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  scp -q "$ANTIGA:$DIR/.env" "$tmp/.env"
  scp -q "$ANTIGA:$DIR/data/cookies.txt" "$tmp/cookies.txt" 2>/dev/null || echo "    (sem cookies.txt na antiga)"
  scp -q "$ANTIGA:$DIR/data/spotify-token.json" "$tmp/spotify-token.json" 2>/dev/null || echo "    (sem spotify-token.json na antiga)"
  ssh "$NOVA" "mkdir -p $DIR/data"
  scp -q "$tmp/.env" "$NOVA:$DIR/.env"
  [[ -f "$tmp/cookies.txt" ]] && scp -q "$tmp/cookies.txt" "$NOVA:$DIR/data/cookies.txt"
  [[ -f "$tmp/spotify-token.json" ]] && scp -q "$tmp/spotify-token.json" "$NOVA:$DIR/data/spotify-token.json"
  ssh "$NOVA" "chmod 600 $DIR/.env $DIR/data/spotify-token.json 2>/dev/null || true"

  echo "==> código, dependências e units na $NOVA (sem subir o serviço)"
  pacote="$(mktemp -t arrodes-XXXXXX).tar.gz"
  tar czf "$pacote" --exclude='__pycache__' --exclude='*.pyc' \
    src requirements.txt requirements-dev.txt README.md .env.example scripts
  scp -q "$pacote" "$NOVA:/tmp/arrodes-deploy.tar.gz"
  rm -f "$pacote"
  # a unit completa (watchdog, MemoryMax) já no cutover; sem ela a nova subiria
  # com a mínima do provisionar_vm.sh até o próximo deploy. O canário vai junto,
  # DESLIGADO: ligado antes do cutover, ele reiniciaria o bot aqui com a antiga
  # ainda tocando — o cutover o liga
  ssh "$NOVA" DIR="$DIR" SERVICE="$SERVICE" 'bash -s' <<'REMOTO'
set -euo pipefail
cd "$DIR"
tar xzf /tmp/arrodes-deploy.tar.gz && rm -f /tmp/arrodes-deploy.tar.gz
.venv/bin/pip install -q -r requirements.txt
sha256sum requirements.txt | cut -d' ' -f1 > .venv/.requirements.sha256
bash scripts/ajustes_vm.sh "$DIR"
troca() { sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$DIR|g" -e "s|@SERVICE@|$SERVICE|g" "$1"; }
troca scripts/arrodes.service > /tmp/arrodes.unit
sudo -n install -m 644 /tmp/arrodes.unit "/etc/systemd/system/$SERVICE.service"
rm -f /tmp/arrodes.unit
for nome in arrodes-canario.service arrodes-canario.timer arrodes-canario.path; do
  troca "scripts/$nome" > "/tmp/$nome"
  sudo -n install -m 644 "/tmp/$nome" "/etc/systemd/system/$nome"
  rm -f "/tmp/$nome"
done
sudo -n systemctl daemon-reload
REMOTO

  echo "==> diagnóstico da extração na $NOVA (perfis + FFmpeg, sem tocar no serviço)"
  ssh "$NOVA" "cd $DIR && .venv/bin/python src/doctor.py" || echo "    (doctor reclamou — veja acima antes do cutover)"
  echo
  echo "Preparado. Quando quiser trocar de VM:  ANTIGA=$ANTIGA NOVA=$NOVA ./scripts/migrar_vm.sh cutover"
}

cutover() {
  echo "==> parando o bot na $ANTIGA (desligamento limpo: salva o cache de streams)"
  ssh "$ANTIGA" "sudo -n systemctl stop $SERVICE"
  sleep 3

  echo "==> copiando banco e cache ($ANTIGA → PC → $NOVA)"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  for f in bot.db stream-cache.json; do
    scp -q "$ANTIGA:$DIR/data/$f" "$tmp/$f" 2>/dev/null || echo "    (sem $f na antiga)"
  done
  for f in bot.db stream-cache.json; do
    [[ -f "$tmp/$f" ]] && scp -q "$tmp/$f" "$NOVA:$DIR/data/$f"
  done

  echo "==> subindo o serviço na $NOVA"
  reinicio="$(ssh "$NOVA" 'date +%s')"
  # `|| true`: com set -e, um restart que falha (Type=notify: o bot morreu antes
  # do READY=1) abortava aqui, sem a volta abaixo — o bot fora do ar nas duas.
  # Quem decide é a sonda
  ssh "$NOVA" "sudo -n systemctl enable $SERVICE >/dev/null 2>&1; sudo -n systemctl restart $SERVICE" || true
  echo "==> esperando a sonda de extração (até 120s)"
  ok=0
  for _ in $(seq 1 60); do
    sonda="$(ssh "$NOVA" "journalctl -u $SERVICE --since @$reinicio --no-pager -o cat | grep -a 'extração de áudio' | tail -1 || true")"
    if [[ "$sonda" == *"ok pelo perfil"* ]]; then echo "    $sonda"; ok=1; break; fi
    if [[ "$sonda" == *"NÃO está funcionando"* ]]; then echo "    $sonda" >&2; break; fi
    sleep 2
  done
  if [[ $ok -ne 1 ]]; then
    echo "A NOVA NÃO PASSOU NA SONDA. Voltando o bot na $ANTIGA." >&2
    # disable, não só stop: o enable acima faria um reboot da nova subir um
    # segundo bot junto com a antiga
    ssh "$NOVA" "sudo -n systemctl disable --now $SERVICE >/dev/null 2>&1 || true" || true
    ssh "$ANTIGA" "sudo -n systemctl start $SERVICE"
    exit 1
  fi

  echo "==> ligando o canário do yt-dlp na $NOVA"
  ssh "$NOVA" "sudo -n systemctl enable --now arrodes-canario.timer arrodes-canario.path >/dev/null 2>&1" \
    || echo "    (não consegui ligar o canário — o próximo deploy.sh liga)" >&2

  echo "==> desabilitando o serviço e o canário na $ANTIGA (não podem voltar sozinhos)"
  # o canário da antiga seguiria rodando todo dia, e o restart dele sobe um bot
  # parado e desabilitado: dois bots no mesmo token
  ssh "$ANTIGA" "sudo -n systemctl disable --now $SERVICE >/dev/null 2>&1 || true"
  ssh "$ANTIGA" "sudo -n systemctl disable --now arrodes-canario.timer arrodes-canario.path >/dev/null 2>&1 || true"
  echo
  echo "Cutover feito: o bot roda na $NOVA. A $ANTIGA fica parada; apague a instância no painel quando estiver satisfeito."
  echo "Deploys daqui em diante: ARRODES_HOST=$NOVA bash scripts/deploy.sh"
  echo "(ou aponte o alias $ANTIGA do ~/.ssh/config para o IP da nova)"
}

case "$ETAPA" in
  preparar) preparar ;;
  cutover) cutover ;;
  *) echo "uso: ANTIGA=host NOVA=host $0 preparar|cutover" >&2; exit 2 ;;
esac
