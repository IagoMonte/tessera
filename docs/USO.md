# Tessera — guia de uso

O Tessera é um painel web que dá **terminais persistentes em todas as suas máquinas**, acessados pela
rede Tailscale, e **avisa no Telegram** quando algo precisa de você (comando terminou, pedido de
aprovação, erro).

## Conceitos

| termo | o que é |
|---|---|
| **Máquina** | computador/servidor na sua tailnet que roda o *agente*. Aparece na barra lateral com CPU/MEM. |
| **Projeto** | uma pasta (e, opcionalmente, um usuário) numa máquina. Cada projeto tem seu próprio ttyd e porta. |
| **Terminal** | uma sessão tmux dentro do projeto. Continua rodando mesmo se você fechar o navegador. |
| **Painel** | o servidor central (`https://<seu domínio>`) que serve a interface e coordena tudo. |
| **Agente** | programa em cada máquina: envia métricas e executa tarefas pedidas pelo painel (lista fechada). |

## Usando a interface

- **Abrir**: acesse `https://<seu domínio>`, entre com a senha e o código do app autenticador e
  escolha um projeto na barra lateral.
- **Novo terminal**: botão `+ Novo TTY` (ou `+` nas tabs). As tabs acima da grade são as sessões
  (estilo VS Code): clique foca, `×` **mata a sessão tmux**. A grade mostra até 4; no celular, um por vez (chips).
- **`×` no terminal/tab mata a sessão de verdade**. Se estiver rodando algo (ex.: `node`, `claude`),
  pede confirmação antes — igual ao VS Code. O `main` nunca some: encerrado, ele reaparece como
  "Reabrir main" (vazio, tabs, paleta) e recria limpo ao abrir.
- **🗑 Limpar**: mata todos os TTYs do projeto e volta pro `main` limpo (header, chips no celular, paleta).
- **⟳** recarrega a conexão do terminal sem tocar na sessão.
- **Paleta de comandos**: `Alt+K` (Mac: `⌘K`). Busca projetos e ações (novo projeto, tema, recarregar…).
- **Cores de estado**: verde = rodando · cinza = ocioso/offline · **âmbar pulsante = aguardando você**.
- **Celular**: navegação inferior (Projetos, Terminais, Facilitadores, Config), campo de comando e
  barra de teclas (esc, tab, ctrl-c, setas, clear).

## Projetos

- **Criar**: `+` ao lado da máquina (nome, pasta com autocomplete — Tab aceita — e usuário), ou, dentro
  da pasta, no terminal da máquina: `tty-projeto add` (também `tty-projeto ls` e `tty-projeto rm <nome>`).
- **Remover**: `×` no projeto desliga o ttyd; pasta e sessões tmux ficam intactas.
  Com a máquina offline vira "Esquecer" (só tira da tela).

## Adicionar uma máquina

1. Tessera → ⚙ Configurações → Máquinas → dê um nome → **Gerar convite**.
2. Cole o comando gerado na máquina nova. O `bootstrap.sh` instala tmux, jq, ttyd e Tailscale, cria o
   projeto padrão e liga o agente. A máquina aparece sozinha.
3. **Sem sudo** (ex.: usuário de site em hospedagem): marque "Sem sudo" no convite; tudo fica no HOME.
4. Desfazer: rode o mesmo comando com `--remover`.

> Não re-rode o convite para "adicionar pasta" — use o `+` da máquina. O bootstrap recusa trocar o nome
> de uma máquina já instalada (a menos que use `--renomear`).

## Avisos no Telegram

Cada aviso traz um link que abre o Tessera direto no terminal certo. Motivos (liga/desliga em
Configurações › Notificações): **terminou**, **aprovação**, **erro**, **ocioso** (10 min, desligado por
padrão), **online** (desligado por padrão).

Fontes: Claude Code e OpenCode (via `tty-add`), qualquer shell no tmux (comando longo ou com erro) e um
"vigia" que detecta perguntas paradas na tela (`[y/N]`, `Password:`…).
Veja também o **tty-notify** acima, para avisar e enviar arquivos manualmente.

## tty-notify — mande avisos e arquivos para o seu celular

A ferramenta mais útil do conjunto: qualquer coisa que rode num terminal do Tessera pode te chamar no
Telegram. Instalada em toda máquina pelo bootstrap; nunca falha (avisar não quebra o script que chama).
O aviso já sai com `máquina · projeto/terminal` e um **link que abre o Tessera no terminal certo**.

```bash
tty-notify <motivo> [texto...]          # motivos: terminou aprovacao pergunta erro ocioso online
tty-notify terminou "build do site pronto"
tty-notify erro "deploy falhou"
tty-notify --arquivo CAMINHO [legenda]  # manda o arquivo ao Telegram (até 50 MB)
tty-notify --claude <motivo>            # lê o JSON do hook do Claude Code no stdin
```

Receitas:
- **Avisar quando algo longo acabar**: `make build; tty-notify terminou "build: $?"` ou
  `./script.sh && tty-notify terminou ok || tty-notify erro "falhou"`.
- **Receber um arquivo** (log, PDF, dump, print): `tty-notify --arquivo relatorio.pdf "relatório de hoje"`.
  Diferente dos avisos, sai com erro se o arquivo não existir ou o envio falhar.
- **Marcar um terminal como "aguardando você"**: use o motivo `aprovacao` ou `pergunta`; o ponto do
  terminal fica âmbar pulsando até haver atividade nele.
- **Automático, sem digitar nada**: o `shell-hook` avisa sozinho quando um comando leva ≥ 15 s ou
  termina com erro (ajuste com `TTY_MIN_SEGUNDOS`; ignora `vim`, `ssh`, `claude`… via `TTY_SHELL_IGNORA`).

O motivo precisa estar ligado em Configurações › Notificações (exceto `--arquivo`, que sempre envia).
Precisa do Telegram configurado no painel (`TG_TOKEN`/`TG_CHAT`).

## Facilitadores (`tty-add` / `tty-push`)

Copiam sua configuração de ferramentas entre máquinas:

1. Na máquina que já tem tudo configurado: `tty-push claude` (ou `opencode`) — guarda a config no painel.
2. Nas outras: `tty-add claude` — instala a ferramenta (se faltar) e aplica a config.

No Tessera (botão **Facilitadores**), cada ferramenta tem **Atualizar todos**: roda `tty-add` em todas as máquinas online e aplica a config guardada no painel (não reinicia nada). O botão **Atualizar** do cabeçalho é outra coisa: só atualiza os scripts `tty-*`.

O `tty-push` pede a senha do painel em `TTY_SENHA` e, em produção, o código atual do autenticador
em `TTY_TOTP`.

O `claude` envia settings, CLAUDE.md, skills, commands e agents (sem credenciais). O `opencode` envia o
`opencode.jsonc` **inteiro — confira chaves de API antes**. Para outra ferramenta, copie
`facilitadores/ferramentas/_modelo.sh`.

## Referência de comandos (CLI)

Todos ficam no PATH das máquinas instaladas (`/usr/local/bin` com root, `~/.local/bin` sem root).

| comando | para que serve | uso |
|---|---|---|
| `tty-notify` | manda aviso ou arquivo ao Telegram | `tty-notify <motivo> [texto]` · `--arquivo CAMINHO [legenda]` · `--claude <motivo>` |
| `tty-projeto` | cria, lista e remove projetos da máquina | `tty-projeto add [nome] [--dir D] [--usuario U] [--porta N]` (sem argumentos usa a pasta atual) · `tty-projeto ls` · `tty-projeto rm <nome>` |
| `tty-add` | instala uma ferramenta e aplica a config guardada no painel | `tty-add <ferramenta> [--sem-config] [--so item1,item2]` |
| `tty-push` | guarda no painel a config local de uma ferramenta | `tty-push <ferramenta>` |
| `tty-atualizar` | atualiza os scripts desta máquina com o pacote atual do painel (não mexe em projetos, serviços nem tailnet) | `tty-atualizar` |
| `tty-sessao` | abre/reanexa a sessão tmux `projeto/terminal` (chamado pelo ttyd; útil para anexar à mão) | `tty-sessao <projeto> <diretório> [terminal]` |
| `tty-ttyd` | sobe o ttyd de um projeto a partir da config dele (chamado pelo systemd/supervisor) | `tty-ttyd <projeto>` |
| `tty-supervisor` | modo sem root: mantém ttyd, vigia e agente vivos, relendo projetos a cada 5 s | `tty-supervisor` (uma instância por usuário; cron/systemd --user chamam) |
| `vigia.sh` | vigia as sessões tmux e avisa de perguntas paradas e ociosidade | `vigia.sh [--uma-vez]` (variáveis `TTY_INTERVALO`, `TTY_ESTAVEL`, `TTY_OCIOSO`, `TTY_VIGIA_IGNORA`) |
| `shell-hook.sh` | aviso automático de comando longo/com erro | `. shell-hook.sh` no `.bashrc`/`.zshrc` (`TTY_MIN_SEGUNDOS`, `TTY_SHELL_IGNORA`) |
| `bootstrap.sh` | instala (ou com `--remover` desfaz) o Tessera numa máquina/usuário | `bash -s -- --nome NOME --painel URL [--usuario U] [--porta N] [--projeto NOME] [--dir D] [--renomear] [--remover]` — normalmente via convite do Tessera |
| `instalar-painel.sh` | instala/atualiza o painel como serviço systemd | `sudo painel/instalar-painel.sh [--usuario U]` · `--remover` |
| `test.sh` | roda todos os testes | `./test.sh` |

Dica: `tty-projeto add` dentro de uma pasta é o jeito mais rápido de virar projeto; `tty-notify` no fim de
qualquer comando longo te avisa no celular.

## Instalar/atualizar o painel (admin)

- Primeira instalação: siga o [quickstart do README](../README.md#quickstart).
- Atualizar: na cópia atualizada do repositório, rode `./test.sh` e depois
  `sudo painel/instalar-painel.sh` no servidor (preserva `.env` e `dados/`). Em seguida clique em
  **Atualizar** no Tessera para propagar os scripts às máquinas.

## Segurança

- Entrada única: `https://<PAINEL_DOMINIO>`. Acesso por IP, `localhost` ou outro nome é recusado, e a
  tailnet nunca acessa o painel.
- Login com senha (20+ caracteres) e código TOTP. A sessão vale 30 min, renovada pelo uso, com teto de
  8 h. Falhas repetidas bloqueiam o IP.
- A TTY de cada projeto só é alcançada pelo relé do painel e exige uma credencial própria.
- Cada máquina usa um token só dela, que age em nome dela nas rotas de agente e também lê as
  configurações de ferramentas guardadas no painel (compartilhadas entre as máquinas).
- `PAINEL_LOCALHOST_LIVRE=1` (loopback sem senha) é só para desenvolvimento.

Modelo completo e riscos aceitos: [SEGURANCA.md](SEGURANCA.md).

Para detalhes técnicos (API, tarefas, arquivos), veja [IA.md](IA.md).
