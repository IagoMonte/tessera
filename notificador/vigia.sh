#!/usr/bin/env bash
# Vigia heurístico das sessões tmux: rede de segurança para ferramentas sem evento nativo.
# Avisa quando um painel
#   - parou mostrando uma pergunta (y/n, Approve, Continue?, senha...)  -> aprovacao
#   - está há TTY_OCIOSO segundos sem saída com um programa rodando       -> ocioso
# Variáveis: TTY_INTERVALO(2) TTY_ESTAVEL(3) TTY_OCIOSO(600) TTY_VIGIA_IGNORA(" claude opencode ")
#            TTY_ESTADO TTY_TMUX (args extras do tmux, ex.: "-L teste")
# Uso: vigia.sh [--uma-vez]
ESTADO=${TTY_ESTADO:-/tmp/tty-vigia-$(id -u)}
ESTAVEL=${TTY_ESTAVEL:-3}
OCIOSO=${TTY_OCIOSO:-600}
IGNORA=${TTY_VIGIA_IGNORA:- claude opencode }
SHELLS=" bash zsh fish sh dash "
PADRAO='(\[[yY]/[nN]\]|\[[yY]es/[nN]o\]|\([yY]/[nN]\)|\(yes/no|Do you want|Approve|Allow (this|once|always)|Continue\?|Proceed\?|Press (enter|any key)|[Pp]assword( for [^:]*)?:|passphrase)'
mkdir -p "$ESTADO"

tm() { tmux ${TTY_TMUX:-} "$@"; }

passo() {
  local agora sess pane cmd txt h f ph pt pn po d
  agora=$(date +%s)
  while IFS='|' read -r sess pane cmd; do
    case "$IGNORA" in *" $cmd "*) continue ;; esac
    txt=$(tm capture-pane -p -t "$pane" 2>/dev/null | sed '/^[[:space:]]*$/d' | tail -n 15)
    h=$(printf %s "$txt" | md5sum | cut -c1-32)
    f="$ESTADO/${sess//[^A-Za-z0-9]/_}_${pane//[^A-Za-z0-9]/_}"
    if [ -r "$f" ]; then read -r ph pt pn po < "$f"; else ph=$h; pt=$agora; pn=-; po=0; fi
    if [ "$h" != "$ph" ]; then ph=$h; pt=$agora; pn=-; po=0; fi
    d=$((agora - pt))
    if printf %s "$txt" | tail -n 3 | grep -Eq "$PADRAO"; then
      # pergunta na tela: avisa uma vez por tela estável; enquanto ela ficar lá, não vira "ocioso"
      if [ "$d" -ge "$ESTAVEL" ] && [ "$pn" != "$h" ]; then
        TTY_SESSAO=$sess tty-notify aprovacao "$(printf %s "$txt" | tail -n 1 | cut -c1-200)" 2>/dev/null
        pn=$h
      fi
    elif [ "$d" -ge "$OCIOSO" ] && [ "$po" = 0 ]; then
      case "$SHELLS" in *" $cmd "*) ;; *) TTY_SESSAO=$sess tty-notify ocioso "$cmd sem saída há ${d}s"; po=1 ;; esac
    fi
    printf '%s %s %s %s\n' "$ph" "$pt" "$pn" "$po" > "$f"
  done < <(tm list-panes -a -F '#{session_name}|#{pane_id}|#{pane_current_command}' 2>/dev/null)
}

if [ "${1:-}" = "--uma-vez" ]; then passo; exit 0; fi
while :; do passo; sleep "${TTY_INTERVALO:-2}"; done
