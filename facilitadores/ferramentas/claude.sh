BIN=claude
DESC="CLI da Anthropic e a configuração dela (~/.claude)."
# sem credenciais, histórico nem projetos: o login é feito na própria TTY
ITENS=(.claude/settings.json .claude/CLAUDE.md .claude/skills .claude/commands .claude/agents .claude/plugins/known_marketplaces.json)
# plugins: só a lista (marketplaces + enabledPlugins do settings.json); o código deles é baixado por `claude plugin` em cada máquina
instalar() { curl -fsSL https://claude.ai/install.sh | bash; }
plugins() {
  local f="$HOME/.claude/settings.json" m src p n=0 falhas=0
  command -v jq >/dev/null 2>&1 && [ -s "$f" ] || return 0
  # marketplaces: os do settings.json (extraKnownMarketplaces) e os do known_marketplaces.json trazido do outro host
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    claude plugin marketplace add "$src" </dev/null >/dev/null 2>&1 || true
  done < <({ jq -r '(.extraKnownMarketplaces // {}) | to_entries[] | .value.source | (.repo // .url // .path // empty)' "$f" 2>/dev/null
             jq -r 'to_entries[] | .value.source | (.repo // .url // .path // empty)' "$HOME/.claude/plugins/known_marketplaces.json" 2>/dev/null; } | sort -u)
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if claude plugin install "$p" </dev/null >/dev/null 2>&1; then n=$((n+1)); else falhas=$((falhas+1)); echo "• plugin $p: FALHOU"; fi
  done < <(jq -r '(.enabledPlugins // {}) | to_entries[] | select(.value == true) | .key' "$f" 2>/dev/null)
  echo "• plugins do Claude Code: $n instalados${falhas:+, $falhas falharam}"
}
pos() {
  local f="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
  [ -s "$f" ] || echo '{}' > "$f"
  # hooks nativos: Notification (pediu aprovação/input) e Stop (terminou); idempotente
  jq '
    def sem: map(select(([.hooks[]?.command] | any(startswith("tty-notify"))) | not));
    def com($c): [{"hooks":[{"type":"command","command":$c}]}];
    .hooks.Notification = ((.hooks.Notification // []) | sem) + com("tty-notify --claude aprovacao")
    | .hooks.Stop = ((.hooks.Stop // []) | sem) + com("tty-notify --claude terminou")
  ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  echo "• hooks de notificação do Claude Code ligados"
}
