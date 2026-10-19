#!/usr/bin/env bash
# Prepara uma máquina (ou um usuário dela) para o tty-orquestrador: projetos com TTY web (tmux + ttyd) pela
# Tailscale, agente (métricas e tarefas do painel), notificador e registro. Idempotente: rodar de novo conserta.
# Cria o projeto padrão "shell" (diretório = HOME do usuário); outros: tty-projeto add <nome> --dir D.
#
# COM root (máquina nova ou com sudo): instala pacotes, entra na tailnet, cria serviços de sistema (systemd).
#   curl -fsSL http://PAINEL/bootstrap.sh | sudo TS_AUTHKEY=tskey-... bash -s -- --nome NOME --painel http://PAINEL
# SEM root (usuário comum numa máquina que JÁ está na tailnet, ex.: usuário de site no CloudPanel): tudo no HOME
# do usuário (~/.local, ~/.config/tty-orquestrador); o que faltar (ttyd, jq, tmux) é baixado como binário;
# um supervisor mantém os serviços vivos (systemd --user, senão cron). Não precisa de sudo nem de auth key.
#   curl -fsSL http://PAINEL/bootstrap.sh | bash -s -- --nome NOME --painel http://PAINEL
#
# Desfazer:  ... bash -s -- --remover --nome NOME --painel http://PAINEL   (no mesmo modo em que instalou)
# Opções:
#   --nome NOME      nome da MÁQUINA no painel (obrigatório). Vários usuários do mesmo servidor usam o MESMO nome:
#                    a máquina é uma só e cada usuário vira um projeto dela
#   --painel URL     URL do painel (registro, pacote de scripts, avisos)
#   --usuario USER   (root) usuário dono das sessões tmux; padrão: SUDO_USER
#   --projeto NOME   nome do projeto padrão; padrão: shell (root), shell-USUARIO (sem root) ou o nome da pasta de --dir
#   --dir D          pasta do projeto padrão (padrão: o HOME). Para ADICIONAR pastas a uma máquina já instalada
#                    use tty-projeto add (ou "Novo projeto" na Tessera): este script é para instalar/atualizar a máquina
#   --renomear       aceita trocar o nome de uma máquina que já está instalada com outro nome (cuidado)
#   --porta N        porta da TTY web do projeto padrão (padrão 7681; sem root, outra livre se estiver ocupada)
#   --pacote DIR     usa os scripts desta pasta em vez de baixar do painel
#   --remover        desfaz o que este script criou (não desinstala pacotes nem sai da tailnet)
# Segredos entram por TS_AUTHKEY (variável de ambiente), nunca por argumento.
set -euo pipefail

NOME= PAINEL= USUARIO=${SUDO_USER:-} PORTA=7681 PORTA_EXPLICITA=0 PACOTE= REMOVER=0 PROJ= DIR_PROJ= RENOMEAR=0 TOKEN=${TTY_TOKEN:-}
log() { printf '• %s\n' "$*"; }
# versões antigas colavam o marcador no fim da última linha de um .bashrc sem quebra de linha final
# ("umask 007# >>> tty-orquestrador >>>" = erro de sintaxe): separa antes de remover/acrescentar o bloco
reparar_rc() { [ -f "$1" ] && sed -i "s/^\(.*[^[:space:]]\)\($RC_INI\)\$/\1\n\2/" "$1"; return 0; }
die() { printf 'erro: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case $1 in
    --nome) NOME=$2; shift ;;
    --painel) PAINEL=${2%/}; shift ;;
    --usuario) USUARIO=$2; shift ;;
    --porta) PORTA=$2; PORTA_EXPLICITA=1; shift ;;
    --projeto) PROJ=$2; shift ;;
    --dir) DIR_PROJ=$2; shift ;;
    --renomear) RENOMEAR=1 ;;
    --pacote) PACOTE=$2; shift ;;
    --token) TOKEN=$2; shift ;;
    --remover) REMOVER=1 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) die "opção desconhecida: $1" ;;
  esac
  shift
done

if [ "$EUID" -eq 0 ]; then MODO=root; else MODO=usuario; fi
[[ "$NOME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "--nome obrigatório (letras, números, . _ -)"
[[ "$PORTA" =~ ^[0-9]+$ ]] || die "--porta inválida"
[ -z "$PROJ" ] || [[ "$PROJ" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]] || die "--projeto inválido (letras, números, _ e -, até 32)"
PROJ_EXPLICITO=0; [ -n "$PROJ" ] && PROJ_EXPLICITO=1
nome_da_pasta() { basename "$1" | sed 's/[^A-Za-z0-9_-]/-/g; s/^[^A-Za-z0-9]*//' | cut -c1-32; }
if [ -n "$DIR_PROJ" ]; then
  [ -d "$DIR_PROJ" ] || die "--dir: a pasta não existe: $DIR_PROJ"
  DIR_PROJ=$(cd "$DIR_PROJ" && pwd)
fi
BINS=(tty-notify tty-vigia tty-add tty-push tty-projeto tty-sessao tty-ttyd tty-agente tty-supervisor tty-atualizar)

if [ "$MODO" = root ]; then
  [ -n "$USUARIO" ] && id "$USUARIO" >/dev/null 2>&1 || die "--usuario inválido ou ausente (padrão: SUDO_USER)"
  LIB=/usr/local/lib/tty-orquestrador; BIN=/usr/local/bin; ETC=/etc/tty-orquestrador; CONF=/etc/tty-orquestrador.conf
  TEM_SYSTEMD=0; [ -d /run/systemd/system ] && TEM_SYSTEMD=1
  PROJ=${PROJ:-shell}
  [ -z "$DIR_PROJ" ] || [ "$PROJ_EXPLICITO" = 1 ] || PROJ=$(nome_da_pasta "$DIR_PROJ")
else
  USUARIO=$(id -un)
  PROJ=${PROJ:-$(printf 'shell-%s' "$USUARIO" | cut -c1-32)}
  [ -z "$DIR_PROJ" ] || [ "$PROJ_EXPLICITO" = 1 ] || PROJ=$(nome_da_pasta "$DIR_PROJ")
  [ -z "${HOME:-}" ] && die "HOME não definido"
  LIB=$HOME/.local/lib/tty-orquestrador; BIN=$HOME/.local/bin; ETC=${XDG_CONFIG_HOME:-$HOME/.config}/tty-orquestrador; CONF=$ETC/conf
  STATE=${XDG_STATE_HOME:-$HOME/.local/state}/tty-orquestrador
  RC_INI="# >>> tty-orquestrador >>>"; RC_FIM="# <<< tty-orquestrador <<<"
  UNIT_USER=tty-orquestrador.service
  export PATH="$BIN:$PATH"
  log "modo sem root: usuário $USUARIO, tudo em $HOME"
fi
UNIT_AGENTE=tty-orquestrador-agente.service

# ---------- guardas: não deixar um "adicionar" virar renomear/duplicar a máquina sem querer ----------
if [ "$REMOVER" != 1 ] && [ "$RENOMEAR" != 1 ]; then
  if [ -f "$CONF" ]; then
    atual=$(sed -n 's/^MAQUINA=//p' "$CONF" | head -n1)
    if [ -n "$atual" ] && [ "$atual" != "$NOME" ]; then
      die "esta máquina já está instalada como '$atual' e você pediu '$NOME'.
  - Para ADICIONAR uma pasta como projeto desta máquina: entre na pasta e rode  tty-projeto add  (com sudo se a instalação for de root),
    ou use ⌘K → 'Novo projeto…' (ou o + ao lado da máquina) na Tessera.
  - Para usar o nome certo, repita com --nome $atual.  Para RENOMEAR de propósito, adicione --renomear."
    fi
  fi
  if [ "$MODO" = usuario ] && [ -r "${TTY_ROOT_CONF:-/etc/tty-orquestrador.conf}" ]; then
    raiz=$(sed -n 's/^MAQUINA=//p' "${TTY_ROOT_CONF:-/etc/tty-orquestrador.conf}" | head -n1)
    if [ -n "$raiz" ] && [ "$raiz" != "$NOME" ]; then
      die "este servidor já é a máquina '$raiz' (instalação com root) e você pediu '$NOME'.
  - Para adicionar o SEU usuário a ela, use  --nome $raiz  (ele vira um projeto da mesma máquina).
  - Para adicionar uma pasta: entre nela e rode  tty-projeto add  (ou 'Novo projeto…' na Tessera)."
    fi
  fi
fi

# ---------- o projeto padrão só nasce na PRIMEIRA instalação ----------
# Reinstalar/atualizar não pode ressuscitar um projeto que o usuário removeu (ele tem outros). Cria se: é a primeira
# instalação, ou o usuário pediu (--projeto/--dir), ou o projeto já existe (reparo), ou é a migração do antigo "shell"
# (sem root), ou a instalação está VAZIA (nenhum projeto): sem projeto não há nada para aparecer no painel.
PRIMEIRA=1; [ -f "$CONF" ] && PRIMEIRA=0
CRIAR_PROJ=0
SEM_PROJETOS=0; [ -z "$(ls -A "$ETC/projetos" 2>/dev/null)" ] && SEM_PROJETOS=1
if [ "$PRIMEIRA" = 1 ] || [ "$SEM_PROJETOS" = 1 ] || [ "$PROJ_EXPLICITO" = 1 ] || [ -n "$DIR_PROJ" ] || [ -f "$ETC/projetos/$PROJ.conf" ] \
   || { [ "$MODO" = usuario ] && [ "$PROJ" != shell ] && [ -f "$ETC/projetos/shell.conf" ]; }; then CRIAR_PROJ=1; fi

# ---------- remoção ----------
if [ "$REMOVER" = 1 ]; then
  if [ "$MODO" = root ]; then
    if [ "$TEM_SYSTEMD" = 1 ]; then
      units=("$UNIT_AGENTE" tty-orquestrador-ttyd.service tty-orquestrador-vigia.service)
      for u in /etc/systemd/system/tty-orquestrador-projeto-*.service /etc/systemd/system/tty-orquestrador-vigia-*.service; do
        [ -e "$u" ] && units+=("$(basename "$u")")
      done
      for u in "${units[@]}"; do
        systemctl disable --now "$u" 2>/dev/null || true
        rm -f "/etc/systemd/system/$u"
      done
      systemctl daemon-reload
    fi
    for b in "${BINS[@]}"; do rm -f "$BIN/$b"; done
    rm -rf "$LIB" "$ETC" "$CONF" /etc/profile.d/tty-orquestrador.sh
    log "removido (tmux, ttyd, jq e Tailscale continuam instalados; a máquina continua na tailnet; sessões tmux e diretórios intactos)"
  else
    systemctl --user disable --now "$UNIT_USER" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$UNIT_USER"
    if command -v crontab >/dev/null 2>&1; then
      (crontab -l 2>/dev/null | grep -v 'tty-supervisor' || true) | crontab - 2>/dev/null || true
    fi
    if [ -f "$STATE/supervisor.pid" ]; then kill "$(cat "$STATE/supervisor.pid")" 2>/dev/null || true; sleep 1; fi
    if [ -f "$HOME/.bashrc" ]; then reparar_rc "$HOME/.bashrc"; sed -i "/^$RC_INI\$/,/^$RC_FIM\$/d" "$HOME/.bashrc"; fi
    for b in "${BINS[@]}"; do rm -f "$BIN/$b"; done
    rm -rf "$LIB" "$ETC" "$STATE"
    log "removido (ttyd, jq e tmux baixados em $BIN, se houver, ficam; sessões tmux e diretórios intactos)"
  fi
  if [ -n "$PAINEL" ]; then
    # sem root: só este usuário sai (a máquina fica enquanto houver outros); com root: a máquina inteira
    if [ "$MODO" = root ]; then alvo="$PAINEL/api/maquinas/$NOME"; else alvo="$PAINEL/api/maquinas/$NOME?agente=${TTY_AGENTE_ID:-$USUARIO}"; fi
    curl -fsS -m 5 "${AUTH[@]}" -X DELETE "$alvo" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# ---------- 1. dependências ----------
instalar_pkg() {
  if command -v pacman >/dev/null; then pacman -S --needed --noconfirm "$@"
  elif command -v apt-get >/dev/null; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
  elif command -v dnf >/dev/null; then dnf install -y -q "$@"
  else return 1; fi
}
arq_arch() { case $(uname -m) in x86_64) echo "$1" ;; aarch64|arm64) echo "$2" ;; *) return 1 ;; esac; }
baixar_ttyd() {  # binário estático do ttyd
  local arq; arq=$(arq_arch ttyd.x86_64 ttyd.aarch64) || return 1
  mkdir -p "$BIN"
  curl -fsSL -o "$BIN/ttyd.novo" "https://github.com/tsl0922/ttyd/releases/latest/download/$arq" && chmod +x "$BIN/ttyd.novo" && mv "$BIN/ttyd.novo" "$BIN/ttyd"
}
baixar_jq() {
  local arq; arq=$(arq_arch jq-linux-amd64 jq-linux-arm64) || return 1
  mkdir -p "$BIN"
  curl -fsSL -o "$BIN/jq.novo" "https://github.com/jqlang/jq/releases/latest/download/$arq" && chmod +x "$BIN/jq.novo" && mv "$BIN/jq.novo" "$BIN/jq"
}
baixar_tmux() {  # build estático publicado em github.com/tmux/tmux-builds
  local sufixo url; sufixo=$(arq_arch linux-x86_64 linux-arm64) || return 1
  url=$(curl -fsSL https://api.github.com/repos/tmux/tmux-builds/releases/latest | jq -r --arg s "$sufixo" '[.assets[].browser_download_url | select(endswith($s + ".tar.gz"))][0] // empty') || return 1
  [ -n "$url" ] || return 1
  mkdir -p "$BIN"
  curl -fsSL "$url" | tar -xz -C "$BIN" tmux && chmod +x "$BIN/tmux"
}

command -v curl >/dev/null || die "curl é necessário"
command -v python3 >/dev/null || die "python3 é necessário (o agente é escrito nele)"
if [ "$MODO" = root ]; then
  faltando=()
  for c in tmux jq curl python3; do command -v "$c" >/dev/null || faltando+=("$c"); done
  if [ ${#faltando[@]} -gt 0 ]; then
    log "instalando: ${faltando[*]}"
    instalar_pkg "${faltando[@]}" || die "não consegui instalar ${faltando[*]}"
  fi
  if ! command -v ttyd >/dev/null; then
    log "instalando ttyd"
    instalar_pkg ttyd 2>/dev/null || baixar_ttyd || die "não consegui instalar o ttyd"
  fi
  # o pacote do ttyd em algumas distros (Ubuntu/Debian) cria e ativa um ttyd.service próprio: não é o nosso
  if [ "$TEM_SYSTEMD" = 1 ] && systemctl cat ttyd.service >/dev/null 2>&1; then
    systemctl disable --now ttyd.service >/dev/null 2>&1 && log "desativado o ttyd.service do pacote (usamos um serviço por projeto)"
  fi
else
  command -v jq >/dev/null || { log "baixando jq para $BIN"; baixar_jq || die "não consegui baixar o jq (peça a um administrador para instalar jq, tmux e ttyd)"; }
  command -v ttyd >/dev/null || { log "baixando ttyd para $BIN"; baixar_ttyd || die "não consegui baixar o ttyd (peça a um administrador para instalar)"; }
  command -v tmux >/dev/null || { log "baixando tmux para $BIN"; baixar_tmux || die "não consegui baixar o tmux (peça a um administrador para instalar)"; }
fi

# ---------- 2. Tailscale ----------
if [ "$MODO" = root ]; then
  if ! command -v tailscale >/dev/null; then
    log "instalando Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  if ! tailscale ip -4 >/dev/null 2>&1; then
    [ -n "${TS_AUTHKEY:-}" ] || die "a máquina não está na tailnet e TS_AUTHKEY não foi dado (gere um convite no painel)"
    log "entrando na tailnet como $NOME"
    tailscale up --authkey="$TS_AUTHKEY" --hostname="$NOME"
  fi
  IP=$(tailscale ip -4 | head -n1)
else
  # sem root não dá para instalar nem entrar na tailnet: usa a que já está ativa na máquina
  # (TTY_IP, TTY_TS_IFACE e TTY_TS_CLI existem só para testes)
  IP=${TTY_IP:-$(ip -4 -o addr show dev "${TTY_TS_IFACE:-tailscale0}" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)}
  [ -n "$IP" ] || IP=$("${TTY_TS_CLI:-tailscale}" ip -4 2>/dev/null | head -n1 || true)
  [ -n "$IP" ] || die "esta máquina não está na tailnet (sem interface tailscale0). Peça a um administrador para entrar na tailnet (comando de convite com sudo) e rode isto de novo"
fi
[ -n "$IP" ] || die "sem IP da Tailscale"
log "Tailscale ok ($IP)"
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

# ---------- 3. pacote de scripts ----------
mkdir -p "$(dirname "$LIB")" "$BIN" "$ETC"
rm -rf "$LIB.novo"; mkdir -p "$LIB.novo"
if [ -n "$PACOTE" ]; then
  for d in notificador facilitadores projetos agente; do cp -r "$PACOTE/$d" "$LIB.novo/"; done
else
  [ -n "$PAINEL" ] || die "informe --painel (ou --pacote DIR)"
  curl -fsSL "${AUTH[@]}" "$PAINEL/pacote.tar.gz" | tar -xz -C "$LIB.novo" || die "não consegui baixar o pacote de $PAINEL"
fi
rm -rf "$LIB"; mv "$LIB.novo" "$LIB"
chmod +x "$LIB"/notificador/tty-notify "$LIB"/notificador/vigia.sh "$LIB"/facilitadores/tty-add "$LIB"/facilitadores/tty-push \
  "$LIB"/projetos/tty-projeto "$LIB"/projetos/tty-sessao "$LIB"/projetos/tty-ttyd "$LIB"/projetos/tty-supervisor "$LIB"/projetos/tty-atualizar "$LIB"/agente/agente.py
ln -sf "$LIB/notificador/tty-notify" "$BIN/tty-notify"
ln -sf "$LIB/notificador/vigia.sh" "$BIN/tty-vigia"
ln -sf "$LIB/facilitadores/tty-add" "$BIN/tty-add"
ln -sf "$LIB/facilitadores/tty-push" "$BIN/tty-push"
ln -sf "$LIB/projetos/tty-projeto" "$BIN/tty-projeto"
ln -sf "$LIB/projetos/tty-sessao" "$BIN/tty-sessao"
ln -sf "$LIB/projetos/tty-ttyd" "$BIN/tty-ttyd"
ln -sf "$LIB/projetos/tty-supervisor" "$BIN/tty-supervisor"
ln -sf "$LIB/projetos/tty-atualizar" "$BIN/tty-atualizar"
ln -sf "$LIB/agente/agente.py" "$BIN/tty-agente"

cat > "$CONF" <<EOF
PAINEL_URL=$PAINEL
MAQUINA=$NOME
PORTA=$PORTA
PAINEL_TOKEN=$TOKEN
EOF
chmod 644 "$CONF"

# aviso de comando longo terminado, dentro das sessões tmux
if [ "$MODO" = root ]; then
  cat > /etc/profile.d/tty-orquestrador.sh <<EOF
[ -n "\${TMUX:-}" ] && [ -n "\${PS1:-}" ] && . $LIB/notificador/shell-hook.sh
EOF
else
  touch "$HOME/.bashrc"
  reparar_rc "$HOME/.bashrc"
  sed -i "/^$RC_INI\$/,/^$RC_FIM\$/d" "$HOME/.bashrc"
  # garante a quebra de linha final antes de acrescentar (o marcador nunca pode colar na última linha do usuário)
  if [ -s "$HOME/.bashrc" ] && [ -n "$(tail -c1 "$HOME/.bashrc")" ]; then printf '\n' >> "$HOME/.bashrc"; fi
  cat >> "$HOME/.bashrc" <<EOF
$RC_INI
case ":\$PATH:" in *":$BIN:"*) ;; *) export PATH="$BIN:\$PATH" ;; esac
[ -n "\${TMUX:-}" ] && [ -n "\${PS1:-}" ] && . "$LIB/notificador/shell-hook.sh"
$RC_FIM
EOF
fi
log "scripts instalados em $LIB"

# ---------- 4. projeto padrão, vigia e agente ----------
if [ "$MODO" = root ]; then
  HOME_USUARIO=$(getent passwd "$USUARIO" | cut -d: -f6)
  if [ "$TEM_SYSTEMD" = 1 ]; then
    # migração do desenho anterior (um ttyd e um vigia por máquina)
    for u in tty-orquestrador-ttyd.service tty-orquestrador-vigia.service; do
      if [ -e "/etc/systemd/system/$u" ]; then systemctl disable --now "$u" >/dev/null 2>&1 || true; rm -f "/etc/systemd/system/$u"; fi
    done
    systemctl daemon-reload
    if [ "$CRIAR_PROJ" = 1 ]; then
      [ -f "$ETC/projetos/$PROJ.conf" ] && [ "$PORTA_EXPLICITA" != 1 ] && PORTA=$(sed -n 's/^PORTA=//p' "$ETC/projetos/$PROJ.conf")
      tty-projeto add "$PROJ" --dir "${DIR_PROJ:-$HOME_USUARIO}" --usuario "$USUARIO" --porta "$PORTA"
    else
      log "projeto padrão '$PROJ' NÃO recriado (você já tem outros projetos e ele foi removido). Para criar uma pasta: + na Tessera ou tty-projeto add"
    fi
    cat > "/etc/systemd/system/$UNIT_AGENTE" <<UNIT_EOF
[Unit]
Description=Agente do tty-orquestrador ($NOME)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/tty-agente
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_EOF
    systemctl daemon-reload
    systemctl enable "$UNIT_AGENTE" >/dev/null 2>&1
    systemctl restart "$UNIT_AGENTE"
    log "serviços ativos (projeto $PROJ, vigia de $USUARIO, agente)"
  else
    log "sem systemd: serviços NÃO criados. Rode: tty-projeto add shell (precisa de systemd) ou ttyd manualmente"
  fi
else
  porta_ocupada() { ss -ltn 2>/dev/null | awk 'NR>1{n=split($4,a,":"); print a[n]}' | grep -qx "$1"; }
  if [ "$CRIAR_PROJ" = 1 ]; then
    # migração: versões antigas criavam o projeto "shell"; nomes por usuário evitam colisão num servidor compartilhado
    if [ "$PROJ" != shell ] && [ -f "$ETC/projetos/shell.conf" ] && [ ! -f "$ETC/projetos/$PROJ.conf" ] && [ "$PORTA_EXPLICITA" != 1 ]; then
      PORTA=$(sed -n 's/^PORTA=//p' "$ETC/projetos/shell.conf"); tty-projeto rm shell >/dev/null 2>&1 || true
    fi
    if [ -f "$ETC/projetos/$PROJ.conf" ] && [ "$PORTA_EXPLICITA" != 1 ]; then
      PORTA=$(sed -n 's/^PORTA=//p' "$ETC/projetos/$PROJ.conf")
      tty-projeto add "$PROJ" --dir "${DIR_PROJ:-$HOME}" --porta "$PORTA" >/dev/null
    elif [ "$PORTA_EXPLICITA" = 1 ] || ! porta_ocupada "$PORTA"; then
      tty-projeto add "$PROJ" --dir "${DIR_PROJ:-$HOME}" --porta "$PORTA" >/dev/null
    else  # a porta padrão é de outro usuário desta máquina: deixa o tty-projeto escolher uma livre
      tty-projeto add "$PROJ" --dir "${DIR_PROJ:-$HOME}" >/dev/null
      PORTA=$(sed -n 's/^PORTA=//p' "$ETC/projetos/$PROJ.conf")
    fi
    log "projeto $PROJ: ${DIR_PROJ:-$HOME} na porta $PORTA"
    sed -i "s/^PORTA=.*/PORTA=$PORTA/" "$CONF"
  else
    log "projeto padrão '$PROJ' NÃO recriado (você já tem outros projetos e ele foi removido). Para criar uma pasta: + na Tessera ou tty-projeto add"
  fi

  # mantém o supervisor vivo: systemd --user (com linger) e/ou cron; sem nenhum dos dois, só roda agora
  if [ "${TTY_SEM_PERSISTENCIA:-0}" = 1 ]; then
    log "persistência desligada por TTY_SEM_PERSISTENCIA (só para testes)"
  else
    persistido=0; linger=0
    if systemctl --user show-environment >/dev/null 2>&1; then
      mkdir -p "$HOME/.config/systemd/user"
      cat > "$HOME/.config/systemd/user/$UNIT_USER" <<UNIT_EOF
[Unit]
Description=tty-orquestrador (supervisor de $NOME)

[Service]
ExecStart=$BIN/tty-supervisor
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT_EOF
      systemctl --user daemon-reload && systemctl --user enable "$UNIT_USER" >/dev/null 2>&1 && persistido=1
      loginctl enable-linger "$USUARIO" >/dev/null 2>&1 && linger=1
      [ "$linger" = 1 ] || log "aviso: não consegui ativar o linger do systemd --user (precisa de root); usando também o cron"
    fi
    if [ "$persistido" = 0 ] || [ "$linger" = 0 ]; then
      if command -v crontab >/dev/null 2>&1; then
        { crontab -l 2>/dev/null | grep -v 'tty-supervisor' || true
          echo "@reboot $BIN/tty-supervisor >/dev/null 2>&1   # tty-supervisor"
          echo "*/5 * * * * $BIN/tty-supervisor >/dev/null 2>&1   # tty-supervisor"
        } | crontab - && persistido=1
      fi
    fi
    if [ "$persistido" = 1 ]; then log "supervisor persistente ($([ "$linger" = 1 ] && echo 'systemd --user' || echo 'cron'))"
    else log "aviso: sem systemd --user nem cron: os serviços rodam agora, mas não voltam sozinhos após reboot (rode tty-supervisor)"; fi
    systemctl --user restart "$UNIT_USER" >/dev/null 2>&1 || true
  fi
  # reinicia o supervisor para ele (e o agente) lerem a configuração nova; as sessões tmux continuam vivas
  if [ -f "$STATE/supervisor.pid" ]; then
    kill "$(cat "$STATE/supervisor.pid")" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8; do kill -0 "$(cat "$STATE/supervisor.pid" 2>/dev/null || echo 0)" 2>/dev/null || break; sleep 0.5; done
  fi
  setsid nohup "$BIN/tty-supervisor" >/dev/null 2>&1 < /dev/null &
  log "serviços ativos (projeto $PROJ, vigia, agente) sob o tty-supervisor"
fi

# ---------- 5. registro no painel ----------
if [ -n "$PAINEL" ]; then
  corpo=$(jq -n --arg n "$NOME" --arg i "$IP" --argjson p "$PORTA" --arg u "$USUARIO" '{nome:$n,ip:$i,porta:$p,usuario:$u}')
  curl -fsS -m 10 "${AUTH[@]}" -H 'Content-Type: application/json' -d "$corpo" "$PAINEL/api/registrar" >/dev/null || die "não consegui registrar no painel $PAINEL"
  log "registrado no painel"
else
  log "sem --painel: máquina não registrada"
fi

# ---------- 6. self-test ----------
if [ "$CRIAR_PROJ" = 1 ]; then
  ok=0
  for _ in $(seq 1 20); do
    if curl -fsS -m 2 -o /dev/null "http://$IP:$PORTA/"; then ok=1; break; fi
    sleep 1
  done
  if [ "$MODO" = root ]; then DICA="journalctl -u tty-orquestrador-projeto-$PROJ"; else DICA="tail $STATE/projeto:$PROJ.log $STATE/supervisor.log"; fi
  [ "$ok" = 1 ] || die "self-test: a TTY web não respondeu em http://$IP:$PORTA/ (veja: $DICA)"
  log "self-test: TTY web responde em http://$IP:$PORTA/"
fi
if [ -n "$PAINEL" ]; then
  if ! curl -fsS -m 5 "${AUTH[@]}" "$PAINEL/api/maquinas" | jq -e --arg n "$NOME" 'has($n)' >/dev/null; then
    if [ -z "$TOKEN" ]; then
      log "aviso: sem --token não dá para conferir o painel (humanos exigem senha; reinstale com o --token do convite)"
    else
      die "self-test: o painel não lista $NOME"
    fi
  else
    log "self-test: painel lista $NOME"
  fi
  agente_ok=0
  for _ in $(seq 1 25); do
    [ -z "$TOKEN" ] && break
    if [ "$CRIAR_PROJ" = 1 ]; then
      curl -fsS -m 3 "${AUTH[@]}" "$PAINEL/api/estado" | jq -e --arg n "$NOME" --arg p "$PROJ" '.maquinas[] | select(.nome==$n and .online) | .projetos[] | select(.nome==$p and .estado!="offline")' >/dev/null && { agente_ok=1; break; }
    else  # sem projeto padrão: basta o agente desta instalação estar online na máquina
      curl -fsS -m 3 "${AUTH[@]}" "$PAINEL/api/estado" | jq -e --arg n "$NOME" --arg a "${TTY_AGENTE_ID:-$([ "$MODO" = root ] && echo root || echo "$USUARIO")}" '.maquinas[] | select(.nome==$n and .online) | .agentes[] | select(.id==$a and .online)' >/dev/null && { agente_ok=1; break; }
    fi
    sleep 1
  done
  if [ "$MODO" = root ]; then DICA="journalctl -u $UNIT_AGENTE"; else DICA="tail $STATE/agente.log"; fi
  if [ -z "$TOKEN" ]; then
    log "aviso: sem --token pulei a espera do agente online (confira na Tessera)"
  else
    [ "$agente_ok" = 1 ] || die "self-test: o agente não chegou ao painel (veja: $DICA)"
    log "self-test: agente envia heartbeat (online na máquina $NOME)"
  fi
  TTY_CONF=$CONF "$BIN/tty-notify" online "instância $NOME online" && log "self-test: aviso 'online' enviado ao painel"
fi
if [ "$CRIAR_PROJ" = 1 ]; then log "pronto. Abra: http://$IP:$PORTA/"; else log "pronto. Abra o painel: ${PAINEL:-(sem --painel)}"; fi
