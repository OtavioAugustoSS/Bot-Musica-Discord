#!/usr/bin/env bash
# Ajustes da VM do Arrodes — idempotente, chamado pelo deploy.sh a cada deploy.
#
#   bash scripts/ajustes_vm.sh /home/ubuntu/arrodes
#
# Saiu da inspeção de 20/09/2026: .env legível por qualquer usuário da máquina,
# journald sem teto (290 MB) e swappiness 60 com o swap num disco de REDE.
set -euo pipefail
DIR="${1:-$PWD}"

# segredos: só o dono lê (estavam 664)
for segredo in "$DIR/.env" "$DIR/data/cookies.txt" "$DIR/data/spotify-token.json"; do
  [[ -f "$segredo" ]] && chmod 600 "$segredo"
done
echo "    segredos: $(stat -c '%a %n' "$DIR/.env" 2>/dev/null || echo 'sem .env')"

instala_se_mudou() {  # instala_se_mudou <destino> <conteúdo> → 0 se mudou
  local destino="$1" conteudo="$2"
  if [[ -f "$destino" ]] && [[ "$(cat "$destino")" == "$conteudo" ]]; then
    return 1
  fi
  sudo -n mkdir -p "$(dirname "$destino")"
  printf '%s\n' "$conteudo" | sudo -n tee "$destino" >/dev/null
}

# journald: teto de disco. Era ilimitado (290 MB em 50 dias); 100 MB guardam
# meses do bot e o rate limit segura uma rajada de log sem derrubar a VM.
if instala_se_mudou /etc/systemd/journald.conf.d/arrodes.conf "[Journal]
SystemMaxUse=100M
SystemMaxFileSize=10M
RateLimitIntervalSec=30s
RateLimitBurst=1000"; then
  sudo -n systemctl restart systemd-journald
  echo "    journald: teto de 100 MB aplicado"
else
  echo "    journald: sem mudança"
fi

# swap: o swapfile mora no volume de boot, que na OCI é disco de REDE — um
# swap-in no meio da música estoura o prazo de 20ms do áudio. swappiness 10 =
# o kernel só troca página do bot quando não tem mesmo outra saída.
if instala_se_mudou /etc/sysctl.d/60-arrodes.conf "vm.swappiness = 10"; then
  sudo -n sysctl -q -p /etc/sysctl.d/60-arrodes.conf
  echo "    swappiness: $(cat /proc/sys/vm/swappiness)"
else
  echo "    swappiness: sem mudança ($(cat /proc/sys/vm/swappiness))"
fi
