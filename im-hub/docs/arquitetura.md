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
- **Entrada única para humanos**: `https://<domínio>` com senha e TOTP; sessão de 30 min renovada pelo uso, com teto de 8 h. O painel descarta conexões vindas da tailnet.
- **Terminal só pelo relé**: a TTY de cada projeto escuta na Tailscale, exige uma credencial própria e o navegador passa pelo relé `/tty/<maquina>/<projeto>` do painel, sem nunca receber essa credencial.
- **Um token por máquina**: derivado de um segredo mestre, restrito às rotas de agente e à leitura das configurações compartilhadas de ferramentas.
- **Segredos fora do código**: chaves e configuração do painel vivem em um arquivo `.env` não versionado.
