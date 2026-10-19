# Tessera

Orquestrador de terminais focado em CLI: terminais persistentes em todas as suas máquinas, organizados por projeto, com avisos no celular quando algo precisa de você.

## Problema

Trabalhar com agentes de IA e builds longos em várias máquinas obrigava a manter SSHs abertos e checar abas sem parar. Uma pergunta parada (`[y/N]`, `Password:`) ou um comando terminado passavam despercebidos.

## O que foi construído

- **Painel web**: grade de terminais por projeto (até 4 lado a lado, um por vez no celular), tabs estilo VS Code, paleta de comandos (`Alt+K`), cores de estado (verde rodando, âmbar pulsante aguardando você).
- **Terminais persistentes**: cada projeto tem seu ttyd e sessões tmux — continua rodando com o navegador fechado.
- **Agente por máquina**: envia CPU/MEM e executa uma lista fechada de tarefas pedidas pelo painel.
- **tty-notify**: qualquer comando avisa no Telegram com link que abre o painel direto no terminal certo; `--arquivo` envia arquivos de até 50 MB.
- **Vigia e shell-hook**: detectam perguntas paradas, comandos longos (>= 15 s) e erros automaticamente.
- **Facilitadores**: `tty-push` guarda a config de ferramentas (Claude, OpenCode) no painel; `tty-add` aplica em todas as máquinas.
- **Bootstrap**: convite gerado no painel instala tmux, jq, ttyd e Tailscale na máquina nova.

## Como rodar

```bash
# painel (servidor central)
# configure painel/.env com modo 600 (chaves de API e Telegram)
painel/instalar-painel.sh   # primeira instalação como serviço
./deploy.sh                 # testa, envia ao servidor e reinicia
./test.sh                   # roda todos os testes
```

Na tailnet e no IP público o painel pede a senha do `.env` (com sessão em cookie); em localhost entra sem senha. Agentes usam token (Bearer).
