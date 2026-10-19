---
name: tessera
description: Use quando estiver rodando num terminal da Tessera (variável TMUX com sessão "projeto/terminal", ou comandos tty-* no PATH) para avisar o usuário no celular, mandar arquivos, criar/listar projetos e orquestrar o trabalho entre máquinas. Dispare ao terminar tarefas longas, ao precisar de aprovação/decisão, ao falhar, ou quando o usuário pedir para "me avisa", "manda o arquivo", "cria um projeto" ou "configura essa ferramenta nas outras máquinas".
---

# Tessera — ferramentas de orquestração

O usuário trabalha por terminais web (tmux + ttyd) em várias máquinas e **não está olhando a tela**.
Os comandos `tty-*` são o seu canal com ele. Verifique com `command -v tty-notify` antes de usar;
se não existir, ignore esta skill em silêncio.

## Quando avisar (`tty-notify`) — faça sem ser pedido

```bash
tty-notify terminou "o que foi feito, em uma frase"     # fim de tarefa longa ou etapa grande
tty-notify aprovacao "o que preciso que ele decida"     # bloqueado esperando decisão/permissão
tty-notify pergunta "a pergunta objetiva"               # dúvida que só ele resolve
tty-notify erro "o que falhou e onde"                   # falha que você não consegue contornar
tty-notify --arquivo CAMINHO "legenda"                  # entrega um artefato (relatório, log, PDF, imagem; até 50 MB)
```

Regras:
- Avise ao **terminar** o que o usuário pediu e sempre que **parar esperando ele**. Uma mensagem curta,
  com o resultado ou a decisão pedida (o aviso já leva máquina, projeto e terminal).
- Não faça spam: nada de aviso por passo intermediário, nem repetido para o mesmo fato.
- Nunca inclua segredos (tokens, senhas, chaves) no texto nem em arquivos enviados.
- O comando nunca falha de propósito; não trate saída como erro da tarefa.
- Claude Code e OpenCode já disparam `terminou`/`aprovacao` sozinhos via hook; use o comando manualmente
  para texto útil, `pergunta`, `erro` e arquivos.

## Projetos e terminais (`tty-projeto`)

```bash
tty-projeto ls                                   # projetos desta máquina
tty-projeto add [nome] [--dir D] [--usuario U]   # sem argumentos: vira projeto a pasta atual
tty-projeto rm <nome>                            # desliga o ttyd; pasta e tmux ficam
```
Cada projeto é uma pasta com um ttyd próprio; terminais são sessões tmux `projeto/terminal`.
Para trabalho paralelo na mesma pasta, abra outro terminal pela Tessera (`+ Novo TTY`) em vez de criar projeto.
Não rode `tty-projeto rm` nem `bootstrap.sh` sem o usuário pedir.

## Configuração entre máquinas (`tty-push` / `tty-add` / `tty-atualizar`)

```bash
tty-push claude|opencode      # guarda a config desta máquina no painel (opencode leva o jsonc inteiro: cheque chaves!)
tty-add claude|opencode       # instala a ferramenta e aplica a config guardada (--sem-config, --so item1,item2)
tty-atualizar                 # atualiza os scripts tty-* desta máquina; reinicia o agente se ele mudou
```
`tty-push`/`tty-add` só quando o usuário pedir. `tty-atualizar` e `deploy.sh` reiniciam serviços:
**só com pedido explícito** (pode haver terminais trabalhando).

## Convenções

- Terminal atual: `tmux display-message -p '#S'` → `projeto/terminal`.
- Link para o usuário: `#/maquina/projeto/terminal` na Tessera.
- Testes do próprio projeto Tessera: `./test.sh`; deploy: `./deploy.sh` (só sob pedido).
- Mais detalhes: `docs/USO.md` (humano) e `docs/IA.md` (técnico) no repositório da Tessera.
