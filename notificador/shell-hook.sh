# Avisa quando um comando longo termina na shell (bash ou zsh). Use: . shell-hook.sh
# Só age dentro do tmux. Ajuste com TTY_MIN_SEGUNDOS (padrão 15).
[ -n "${TMUX:-}" ] || return 0 2>/dev/null || exit 0
[ -n "${__TTY_HOOK:-}" ] && return 0 2>/dev/null
__TTY_HOOK=1

: "${TTY_MIN_SEGUNDOS:=15}"
# ferramentas interativas: terminam quando você sai delas, avisar seria ruído
: "${TTY_SHELL_IGNORA:= claude opencode vim nvim nano less man ssh top htop tmux fzf watch tail }"

__tty_fim_comando() {
  local rc=$1 cmd=$2 dur=$3 prim=${2%% *}
  [ -n "$cmd" ] || return 0
  [ "$dur" -ge "$TTY_MIN_SEGUNDOS" ] || return 0
  case "$TTY_SHELL_IGNORA" in *" $prim "*) return 0 ;; esac
  if [ "$rc" -eq 0 ]; then
    tty-notify terminou "$cmd (${dur}s)" &
  else
    tty-notify erro "$cmd saiu com $rc (${dur}s)" &
  fi
  disown 2>/dev/null
  return 0
}

if [ -n "${ZSH_VERSION:-}" ]; then
  __tty_pre() { __tty_cmd=$1; __tty_t0=$SECONDS; }
  __tty_pos() { local rc=$?; __tty_fim_comando "$rc" "${__tty_cmd:-}" "$((SECONDS - ${__tty_t0:-$SECONDS}))"; __tty_cmd=; }
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec __tty_pre
  add-zsh-hook precmd __tty_pos
elif [ -n "${BASH_VERSION:-}" ]; then
  # Robusto contra rc files que mexem em PROMPT_COMMAND/PS0 (o Arch define os dele):
  #  - o PS0 só expande quando o usuário executa algo: cria um marcador = "comando do usuário vai começar"
  #  - o trap DEBUG só registra início quando o marcador existe (ignora comandos do prompt)
  #  - __tty_pos (fim + código de saída) é recolocado em primeiro lugar no PROMPT_COMMAND a cada comando
  __tty_marca="${TMPDIR:-/tmp}/.tty-hook-$$"
  __tty_cmd=; __tty_t0=0; __tty_ativo=
  __tty_pre() {
    if [ -e "$__tty_marca" ]; then
      rm -f "$__tty_marca"
      if [ "$BASH_COMMAND" != "__tty_pos" ]; then
        __tty_cmd=$BASH_COMMAND; __tty_t0=$SECONDS; __tty_ativo=1
      fi
    fi
    if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
      if [ "${PROMPT_COMMAND[0]:-}" != "__tty_pos" ]; then
        local novo=(__tty_pos) x
        for x in "${PROMPT_COMMAND[@]}"; do [ "$x" = "__tty_pos" ] || novo+=("$x"); done
        PROMPT_COMMAND=("${novo[@]}")
      fi
    else
      case "$PROMPT_COMMAND" in
        __tty_pos|__tty_pos\;*) ;;
        *) local resto=${PROMPT_COMMAND//__tty_pos;/}; resto=${resto//__tty_pos/}
           PROMPT_COMMAND="__tty_pos${resto:+;$resto}" ;;
      esac
    fi
    return 0
  }
  __tty_pos() {
    local rc=$? linha cmd
    if [ -n "$__tty_ativo" ]; then
      __tty_ativo=
      linha=$(HISTTIMEFORMAT= history 1 2>/dev/null)
      read -r _ cmd <<< "$linha"
      __tty_fim_comando "$rc" "${cmd:-$__tty_cmd}" "$((SECONDS - __tty_t0))"
    fi
    return $rc
  }
  PS0="${PS0-}"'$(: >"'"$__tty_marca"'")'
  trap '__tty_pre' DEBUG
  trap 'rm -f "$__tty_marca"' EXIT
  if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
    PROMPT_COMMAND=(__tty_pos "${PROMPT_COMMAND[@]}")
  else
    PROMPT_COMMAND="__tty_pos${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
  fi
fi
