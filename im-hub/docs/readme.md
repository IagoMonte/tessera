# Tessera

Orquestrador de terminais focado em CLI: terminais persistentes em todas as suas máquinas, organizados por projeto, com avisos no celular quando algo precisa de você.

## Problema

Trabalhar com agentes de IA e builds longos em várias máquinas obrigava a manter SSHs abertos e checar abas sem parar. Uma pergunta parada (`[y/N]`, `Password:`) ou um comando terminado passavam despercebidos.

## O que foi construído

- **Painel web**: grade de terminais por projeto (até 4 lado a lado, um por vez no celular), tabs estilo VS Code, paleta de comandos (`Alt+K`), cores de estado (verde rodando, âmbar pulsante aguardando você).
- **Terminais persistentes**: cada projeto tem seu ttyd e sessões tmux, que continuam rodando com o navegador fechado.
- **Agente por máquina**: envia CPU/MEM e executa uma lista fechada de tarefas pedidas pelo painel.
- **tty-notify**: qualquer comando avisa no Telegram com link que abre o painel direto no terminal certo; `--arquivo` envia arquivos de até 50 MB.
- **Vigia e shell-hook**: detectam perguntas paradas, comandos longos (15 s ou mais) e erros automaticamente.
- **Facilitadores**: `tty-push` guarda a config de ferramentas (Claude Code, OpenCode) no painel; `tty-add` aplica nas outras máquinas.
- **Bootstrap**: o convite gerado no painel instala tmux, jq, ttyd e Tailscale na máquina nova.

## Segurança

- Entrada única por `https://<domínio>`, com senha e segundo fator (TOTP).
- O terminal só passa pelo relé autenticado do painel; a rede Tailscale é o canal interno até as máquinas.
- Um token por máquina, restrito às rotas de agente.

## Como rodar

```bash
# experimentar o painel localmente (modo de desenvolvimento)
PAINEL_LOCALHOST_LIVRE=1 python3 painel/painel.py

# instalar em um servidor (requer o painel/.env preenchido)
sudo painel/instalar-painel.sh

# testes
./test.sh
```

O passo a passo de produção está no README do repositório.
