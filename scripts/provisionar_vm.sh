#!/usr/bin/env bash
# Prepara a VM do Arrodes, zerada: Oracle Always Free VM.Standard.E2.1.Micro
# (x86, 1/8 OCPU com burst, 954 MB), Ubuntu 24.04, usuário ubuntu.
#
#   ssh arrodes 'bash -s' < scripts/provisionar_vm.sh
#
# "arrodes" é o alias do ~/.ssh/config (HostName = IP da VM, User ubuntu,
# IdentityFile = chave da instância). Uma vez, ANTES do primeiro deploy:
#
#   1. ssh arrodes 'bash -s' < scripts/provisionar_vm.sh
#   2. scp .env arrodes:/home/ubuntu/arrodes/.env   (cookies e token do Spotify vão em data/)
#   3. ARRODES_HOST=arrodes bash scripts/deploy.sh
#
# Idempotente: rodar de novo só confere. NÃO copia .env nem data/ e NÃO sobe o
# serviço — quem sobe é o deploy.sh, depois de o .env estar no lugar. Dois bots
# com o mesmo token ao mesmo tempo brigam pela sessão do gateway; trocar de VM
# com o bot no ar é o scripts/migrar_vm.sh.
#
# Supõe sudo SEM SENHA: a imagem Ubuntu da Oracle já dá NOPASSWD:ALL ao ubuntu
# (cloud-init), e o deploy usa bem mais que o restart — install da unit,
# daemon-reload, enable, systemd-run do deploy agendado, journald e sysctl.
#
#   ARRODES_DIR=/home/ubuntu/arrodes  ARRODES_SERVICE=arrodes  ARRODES_SWAP_MB=2048
set -euo pipefail

DIR="${ARRODES_DIR:-$HOME/arrodes}"
SERVICE="${ARRODES_SERVICE:-arrodes}"
SWAP_MB="${ARRODES_SWAP_MB:-2048}"
USUARIO="$(id -un)"

if ! sudo -n true 2>/dev/null; then
  echo "sudo pediu senha: este script e o deploy.sh precisam de sudo sem senha" >&2
  exit 1
fi

# swap antes de tudo: com 954 MB o pip do primeiro deploy (numpy,
# faster-whisper, curl_cffi) e um /play com o deno no meio não cabem sem ele
echo "==> swap (${SWAP_MB} MB em /swapfile)"
ativos="$(swapon --show=NAME --noheadings 2>/dev/null || true)"
if grep -qx /swapfile <<<"$ativos"; then
  echo "    já ativo"
elif [[ -n "$ativos" ]]; then
  echo "    a VM já tem swap ($(tr '\n' ' ' <<<"$ativos")) — /swapfile não é criado"
else
  if [[ -f /swapfile ]]; then sudo chmod 600 /swapfile; fi
  if ! { [[ -f /swapfile ]] && sudo swapon /swapfile 2>/dev/null; }; then
    # sem arquivo, ou um que sobrou de uma rodada interrompida: refaz do zero
    sudo rm -f /swapfile
    sudo fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null \
      || sudo dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
  fi
  echo "    ativo: $(free -m | awk '/Swap:/{print $2}') MB"
fi
if [[ -f /swapfile ]] && ! grep -qE '^[[:space:]]*/swapfile[[:space:]]' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "    /etc/fstab: volta sozinho no boot"
fi

echo "==> pacotes do sistema"
# VM recém-criada: o unattended-upgrades costuma estar com o lock do dpkg. O
# Lock::Timeout só vale para ele: o `update` pega o /var/lib/apt/lists/lock, que
# o apt-daily segura no boot, e falha na hora — esse vai no laço
apt_get() {
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    apt-get -o DPkg::Lock::Timeout=600 -y -qq "$@"
}
for i in $(seq 1 60); do
  apt_get update && break
  if [[ $i == 60 ]]; then echo "apt-get update falhou por 10 min" >&2; exit 1; fi
  echo "    apt ocupado (o apt-daily do boot?) — de novo em 10s"
  sleep 10
done
apt_get install python3-venv python3-pip ffmpeg libopus0 unzip curl git >/dev/null
python3 --version
ffmpeg -version | sed -n 1p

echo "==> deno (runtime de JS do yt-dlp)"
if ! command -v deno >/dev/null; then
  if [[ ! -x "$HOME/.deno/bin/deno" ]]; then
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$HOME/.deno" sh -s -- --yes >/dev/null
  fi
  # o bot roda pelo systemd, sem o PATH do .bashrc
  sudo ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
fi
deno --version | sed -n 1p

echo "==> venv em $DIR"
mkdir -p "$DIR/data"
[[ -x "$DIR/.venv/bin/pip" ]] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip

echo "==> serviço systemd ($SERVICE)"
UNIT_PATH="/etc/systemd/system/$SERVICE.service"
if [[ -f "$UNIT_PATH" ]]; then
  # o deploy troca esta pela unit completa (scripts/arrodes.service): rodar de
  # novo não pode desfazer isso
  echo "    já existe — fica como está"
else
  sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Arrodes - bot de musica do Discord
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python src/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  echo "    mínima e desabilitada: o deploy.sh instala a completa e habilita no boot"
fi

echo "==> sudo sem senha para o serviço (o canário e o deploy usam sudo -n)"
# redundante com o NOPASSWD:ALL da imagem; segura o restart se ele um dia sair.
# Arquivo inválido em sudoers.d derruba o sudo inteiro: o visudo confere antes.
regra="$(mktemp)"
echo "$USUARIO ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart $SERVICE, /usr/bin/systemctl stop $SERVICE, /usr/bin/systemctl start $SERVICE" > "$regra"
if sudo visudo -cqf "$regra" >/dev/null; then
  sudo install -m 440 -o root -g root "$regra" "/etc/sudoers.d/$SERVICE"
else
  echo "    regra inválida para o visudo — /etc/sudoers.d/$SERVICE não mexido" >&2
fi
rm -f "$regra"

echo "==> pronto: $(nproc) vCPU, $(free -m | awk '/Mem:/{print $2}') MB RAM, $(free -m | awk '/Swap:/{print $2}') MB swap, $(uname -m)"
echo "    próximo: scp .env <host>:$DIR/.env  e  ARRODES_HOST=<host> bash scripts/deploy.sh"
