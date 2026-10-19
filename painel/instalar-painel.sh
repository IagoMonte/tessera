#!/usr/bin/env bash
# Instala o painel como serviço systemd em /opt/tty-orquestrador.
#   sudo ./painel/instalar-painel.sh [--usuario USER]     (rode de dentro de uma cópia do projeto)
#   sudo ./painel/instalar-painel.sh --remover
# O arquivo painel/.env (TS_API_KEY, TG_TOKEN, TG_CHAT) deve existir na cópia ou já em /opt.
set -euo pipefail
ORIGEM=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/tty-orquestrador
UNIT=tty-orquestrador-painel.service
USUARIO=${SUDO_USER:-ubuntu}; REMOVER=0
while [ $# -gt 0 ]; do
  case $1 in --usuario) USUARIO=$2; shift ;; --remover) REMOVER=1 ;; *) echo "opção desconhecida: $1" >&2; exit 2 ;; esac
  shift
done
[ "$EUID" -eq 0 ] || { echo "rode como root (sudo)" >&2; exit 1; }

if [ "$REMOVER" = 1 ]; then
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT"; systemctl daemon-reload
  echo "• serviço removido; $DEST preservado (dados e .env). Apague à mão se quiser."
  exit 0
fi

id "$USUARIO" >/dev/null 2>&1 || { echo "usuário inexistente: $USUARIO" >&2; exit 1; }
mkdir -p "$DEST"
for d in painel bootstrap facilitadores notificador projetos agente; do
  mkdir -p "$DEST/$d"
  # preserva dados e .env já existentes no destino
  tar -C "$ORIGEM" --exclude=dados --exclude=.env --exclude=__pycache__ --exclude='*.log' -cf - "$d" | tar -C "$DEST" -xf -
done
[ -f "$ORIGEM/painel/.env" ] && [ "$ORIGEM" != "$DEST" ] && install -m 600 "$ORIGEM/painel/.env" "$DEST/painel/.env"
[ -f "$DEST/painel/.env" ] || echo "aviso: sem $DEST/painel/.env (convites e Telegram ficam desligados)" >&2
mkdir -p "$DEST/painel/dados"
chown -R "$USUARIO": "$DEST"
chmod 600 "$DEST/painel/.env" 2>/dev/null || true

cat > "/etc/systemd/system/$UNIT" <<UNIT_EOF
[Unit]
Description=Painel do tty-orquestrador
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$DEST/painel
ExecStart=/usr/bin/python3 $DEST/painel/painel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
sleep 1
systemctl is-active "$UNIT" >/dev/null || { journalctl -u "$UNIT" -n 20 --no-pager; exit 1; }
echo "• painel ativo ($UNIT). Veja: journalctl -u $UNIT"
