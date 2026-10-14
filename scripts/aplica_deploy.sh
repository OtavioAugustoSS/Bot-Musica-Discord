#!/usr/bin/env bash
# Aplica um pacote de deploy NA VM: extrai, dependências, units, diagnóstico,
# reinicia e espera a sonda do boot. Sonda falhou = volta a versão anterior.
#
#   REMOTE_DIR=/home/ubuntu/arrodes SERVICE=arrodes bash aplica_deploy.sh PACOTE.tar.gz
#
# Chamado pelo scripts/deploy.sh (na hora, com o bot ocioso) e pelo
# scripts/deploy_quando_ocioso.sh (o deploy agendado, que espera o bot sair da call).
set -euo pipefail
PACOTE="$1"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/arrodes}"
SERVICE="${SERVICE:-arrodes}"
USUARIO="${USER:-$(id -un)}"  # no deploy agendado (systemd-run) não há $USER
cd "$REMOTE_DIR"
# Um deploy por vez. Em 21/09/2026 dois rodaram JUNTOS (um manual e um que
# esperava o bot ficar ocioso): os dois fizeram "rm -rf src" + "cp" no mesmo
# diretorio e um morreu com "cannot create directory ./src: File exists". O
# codigo ficou integro por sorte (conferido por sha256) - com a trava, o segundo
# sai na hora sem tocar em nada.
exec 9> /tmp/arrodes-deploy.lock
if ! flock -n 9; then
  echo "Outro deploy está em andamento nesta VM — este não fez nada." >&2
  exit 4
fi
UNIT="/etc/systemd/system/$SERVICE.service"
ANTERIOR="$REMOTE_DIR/.deploy-anterior"

echo "==> guardando a versão anterior (código + unit)"
rm -rf "$ANTERIOR"; mkdir -p "$ANTERIOR"
if [[ -d src ]]; then
  tar czf "$ANTERIOR/codigo.tar.gz" src requirements.txt
  cp "$UNIT" "$ANTERIOR/unit.service" 2>/dev/null || true
else
  # VM recém-provisionada: o tar acima morreria com "src: Cannot stat"
  echo "    primeiro deploy desta VM — não há versão anterior"
fi

volta_a_anterior() {
  if [[ ! -f "$ANTERIOR/codigo.tar.gz" ]]; then
    echo "==> primeiro deploy: não há versão anterior para voltar" >&2
    echo "    serviço: $(systemctl is-active "$SERVICE" || true)" >&2
    return 0
  fi
  echo "==> VOLTANDO à versão anterior" >&2
  rm -rf src
  tar xzf "$ANTERIOR/codigo.tar.gz" -C "$REMOTE_DIR"
  if [[ -f "$ANTERIOR/unit.service" ]] && ! cmp -s "$ANTERIOR/unit.service" "$UNIT"; then
    # a unit nova espera READY=1 (Type=notify); o código antigo não manda
    sudo -n install -m 644 "$ANTERIOR/unit.service" "$UNIT"
    sudo -n systemctl daemon-reload
  fi
  sudo -n systemctl restart "$SERVICE" || true
  sleep 8
  echo "    serviço: $(systemctl is-active "$SERVICE" || true)" >&2
}

echo "==> extraindo"
# primeiro num canto: pacote corrompido não pode deixar a VM sem src/
PALCO="$(mktemp -d)"
tar xzf "$PACOTE" -C "$PALCO"
test -f "$PALCO/src/main.py"
rm -rf src  # arquivo apagado no repositório não pode continuar vivo na VM
cp -a "$PALCO/." "$REMOTE_DIR/"
rm -rf "$PALCO" "$PACOTE"

echo "==> dependências"
SOMA="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [[ "$(cat .venv/.requirements.sha256 2>/dev/null || true)" == "$SOMA" ]]; then
  echo "    requirements.txt não mudou — pip pulado"
else
  .venv/bin/pip install -q -r requirements.txt
  echo "$SOMA" > .venv/.requirements.sha256
fi

echo "==> runtime de JS"
if command -v deno >/dev/null || command -v node >/dev/null; then
  echo "    ok: $(command -v deno || command -v node)"
else
  echo "    AUSENTE — sem deno/node o yt-dlp perde os melhores clients."
  echo "    Instale com: curl -fsSL https://deno.land/install.sh | sh"
fi

echo "==> ajustes da VM (idempotentes)"
bash scripts/ajustes_vm.sh "$REMOTE_DIR"

echo "==> unit do systemd"
troca() { sed -e "s|@USER@|$USUARIO|g" -e "s|@DIR@|$REMOTE_DIR|g" -e "s|@SERVICE@|$SERVICE|g" "$1"; }
troca scripts/arrodes.service > /tmp/arrodes.unit
if cmp -s /tmp/arrodes.unit "$UNIT"; then
  echo "    sem mudança"
elif sudo -n install -m 644 /tmp/arrodes.unit "$UNIT" && sudo -n systemctl daemon-reload; then
  echo "    instalada: $(systemd-analyze verify "$UNIT" 2>&1 | head -3 | tr '\n' ' ')"
else
  echo "    NÃO consegui instalar a unit (sudo pediu senha?) — seguindo com a antiga" >&2
fi
rm -f /tmp/arrodes.unit
# o provisionar_vm.sh deixa a unit desabilitada: sem enable, um reboot da VM
# deixaria o bot fora do ar até alguém reparar
if [[ "$(systemctl is-enabled "$SERVICE" 2>/dev/null || true)" != enabled ]]; then
  if sudo -n systemctl enable "$SERVICE" >/dev/null 2>&1; then
    echo "    habilitada no boot"
  else
    echo "    NÃO consegui habilitar no boot (sudo pediu senha?)" >&2
  fi
fi

echo "==> canário do yt-dlp (timer diário + gatilho do bot)"
mudou=0
for nome in arrodes-canario.service arrodes-canario.timer arrodes-canario.path; do
  troca "scripts/$nome" > "/tmp/$nome"
  if ! cmp -s "/tmp/$nome" "/etc/systemd/system/$nome"; then
    sudo -n install -m 644 "/tmp/$nome" "/etc/systemd/system/$nome" && mudou=1
  fi
  rm -f "/tmp/$nome"
done
if [[ "$mudou" == 1 ]]; then sudo -n systemctl daemon-reload; fi
# desligado com as units no lugar: o migrar_vm.sh instala sem ligar, e o enable
# do cutover pode ter falhado
desligado=0
for nome in arrodes-canario.timer arrodes-canario.path; do
  systemctl is-enabled --quiet "$nome" 2>/dev/null || desligado=1
done
if [[ "$mudou" == 1 || "$desligado" == 1 ]]; then
  if sudo -n systemctl enable --now arrodes-canario.timer arrodes-canario.path >/dev/null 2>&1; then
    echo "    instalado: timer $(systemctl is-active arrodes-canario.timer), gatilho $(systemctl is-active arrodes-canario.path)"
  else
    echo "    NÃO consegui ligar o canário (sudo pediu senha?)" >&2
  fi
else
  echo "    sem mudança"
fi

echo "==> diagnóstico da extração (antes de reiniciar)"
.venv/bin/python src/doctor.py || echo "    (o bot sobe assim mesmo — a cadeia de perfis pode se virar)"

echo "==> reiniciando $SERVICE"
# -n: sem senha interativa. O heredoc do ssh nao tem tty, entao um prompt
# de sudo travaria o deploy ate o timeout em vez de falhar na hora.
REINICIO="$(date +%s)"
if ! sudo -n systemctl restart "$SERVICE"; then
  echo "o restart falhou (sudo pediu senha, ou a unit nova não subiu)" >&2
  volta_a_anterior
  exit 1
fi
sleep 6
systemctl is-active "$SERVICE" || true

echo "==> log da subida"
journalctl -u "$SERVICE" --since "@$REINICIO" --no-pager -o cat | tail -25

# O doctor roda ANTES do restart; nada confirmava que o bot que subiu
# extrai de verdade. A sonda do boot (main._check_extraction) responde isso
# em ~10s — o deploy so termina bem depois de ela passar.
echo "==> esperando a sonda de extração do bot (até 90s)"
for _ in $(seq 1 45); do
  # `|| true`: com pipefail+errexit, um grep sem resultado (a sonda ainda
  # não rodou) derrubava o script inteiro em vez de esperar
  sonda="$(journalctl -u "$SERVICE" --since "@$REINICIO" --no-pager -o cat | grep -a "extração de áudio" | tail -1 || true)"
  if [[ "$sonda" == *"ok pelo perfil"* ]]; then
    echo "    $sonda"
    exit 0
  fi
  if [[ "$sonda" == *"NÃO está funcionando"* ]]; then
    # extração morta pode ser o YouTube, não este deploy: a versão anterior
    # estaria igualmente muda. Voltar só esconderia o código novo.
    echo "    $sonda" >&2
    echo "DEPLOY COM O BOT MUDO (o YouTube mudou?): rode .venv/bin/python src/doctor.py" >&2
    exit 1
  fi
  sleep 2
done
# nem "ok" nem "NÃO está funcionando" em 90s: o processo novo não chegou a
# rodar a sonda — aí sim é o deploy. Volta.
echo "    a sonda não respondeu em 90s — o código novo não subiu direito" >&2
journalctl -u "$SERVICE" --since "@$REINICIO" --no-pager -o cat | tail -15 >&2
volta_a_anterior
exit 1
