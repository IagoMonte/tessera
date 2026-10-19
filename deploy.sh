#!/usr/bin/env bash
# Envia o código deste projeto ao Oracle (/opt/tty-orquestrador) e reinicia o painel.
# Depois, o botão "Atualizar" da Tessera leva os scripts do painel a todas as máquinas.
#   ./deploy.sh            roda os testes antes de enviar
#   ./deploy.sh --sem-testes
# Mantém o .env e a pasta dados/ do painel; o reinício do painel dura ~2 s.
set -euo pipefail
cd "$(dirname "$0")"
# Destino e chave vêm do ambiente ou do deploy.env (não versionado; modelo em deploy.env.example).
[ -f deploy.env ] && . ./deploy.env
ORACLE=${TTY_ORACLE:?defina TTY_ORACLE em deploy.env (veja deploy.env.example)}
CHAVE=${TTY_CHAVE:?defina TTY_CHAVE em deploy.env (veja deploy.env.example)}
[ "${1:-}" = --sem-testes ] || ./test.sh >/dev/null || { echo "testes falharam: nada foi enviado (rode ./test.sh)"; exit 1; }
tar --exclude=dados --exclude=.env --exclude=__pycache__ --exclude='*.log' -czf - painel bootstrap facilitadores notificador projetos agente |
  ssh -i "$CHAVE" -o BatchMode=yes "$ORACLE" 'tar -xzf - -C /opt/tty-orquestrador && sudo systemctl restart tty-orquestrador-painel && sleep 2 && systemctl is-active tty-orquestrador-painel'
echo "• painel atualizado em $ORACLE. Agora clique em Atualizar na Tessera."
