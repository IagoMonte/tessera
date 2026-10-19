# Tessera (tty-orquestrador)

Terminais web persistentes (tmux + ttyd) em todas as suas máquinas via Tailscale, organizados por
projeto, com avisos no Telegram.

- 📘 [Guia de uso](docs/USO.md) — o que cada coisa faz e como usar (inclui o `tty-notify`)
- 🤖 [Documentação para IA](docs/IA.md) — referência técnica completa

```
painel/          servidor + interface web (Tessera)
agente/          agente por máquina (métricas e tarefas)
projetos/        tty-projeto, tty-sessao, tty-ttyd, tty-supervisor
bootstrap/       instalação de máquina nova
notificador/     avisos (tty-notify, shell-hook, vigia)
facilitadores/   tty-add / tty-push
testes/          integração; ./test.sh roda tudo
deploy.sh        envia o painel ao Oracle
```
