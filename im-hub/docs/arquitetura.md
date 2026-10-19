# Arquitetura — Tessera

## Stack

Python (só biblioteca padrão no painel), tmux, ttyd, Tailscale, xterm.js, Telegram Bot API, systemd, Bash.

## Componentes

```text
painel/          servidor + interface web (ThreadingHTTPServer, sem dependências)
agente/          agente por máquina (métricas e tarefas)
projetos/        tty-projeto, tty-sessao, tty-ttyd, tty-supervisor
bootstrap/       instalação de máquina nova via convite
notificador/     tty-notify, shell-hook, vigia
facilitadores/   tty-add / tty-push (sincroniza configs de ferramentas)
testes/          integração; ./test.sh roda tudo
```

## Decisões

- **Só biblioteca padrão no painel**: zero dependências para o servidor ser trivialmente instalável e auditável.
- **tmux como fonte da verdade**: a sessão sobrevive ao navegador; o ttyd só espelha. Fechar tab com processo rodando pede confirmação.
- **Lista fechada de tarefas** (`projeto.add`, `terminal.novo`, `ferramenta.add`...): o agente nunca executa comando arbitrário do painel.
- **Autenticação em qualquer acesso humano**: localhost isento (dev); tailnet e IP público exigem senha (Basic) com sessão em cookie; agentes usam Bearer. A TTY web não tem senha própria: só escuta em `tailscale0` com `-O`, e o navegador passa pelo relé `/tty/<maquina>/<projeto>` autenticado do painel.
- **Segredos fora do código**: destino de deploy e chaves vivem em arquivos `.env` não versionados.
