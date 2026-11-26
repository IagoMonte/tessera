# Tessera (tty-orquestrador) — documentação para IA

> Referência técnica para agentes de IA que vão mexer neste repositório. Para uso humano veja [USO.md](USO.md).
> Regras: nada de segredos no git (`painel/.env`, `painel/dados/` são ignorados); scripts rodados pelo agente root no systemd não têm `HOME` (use `${HOME:-}`); não re-rodar bootstrap em máquinas reais sem necessidade; rode `./test.sh` antes de atualizar o painel.


Terminais web persistentes (tmux + ttyd) em todas as máquinas, pela Tailscale, organizados **por projeto**
(diretório + usuário opcional, quantos terminais quiser), com avisos no Telegram para tudo que exige você.
A interface (Tessera) fica em `painel/static/`
(vanilla JS + xterm.js vendorizado, sem build).

**Modelo de acesso:** cada ttyd sobe com `-W -O -a -c tessera:<CRED>`. O `CRED` (32 hex, gerado pelo `tty-projeto add`, no `.conf` do projeto, modo 640) é exigido no handshake (`Authorization: Basic`) e no primeiro frame (`AuthToken`); sem ele o `tty-ttyd` nem sobe. O `-O` sozinho não protege (cliente não-navegador forja `Origin`). O agente manda o `CRED` ao painel no heartbeat (só em memória); o relé injeta o Basic e o front manda o `AuthToken` a partir de `/api/estado` (que exige a senha do painel). O `-i` já falhou em ttyd 1.7.4 e o `tty-ttyd` aborta se o socket sair em curinga — mantenha também firewall/NSG fechando a porta na interface pública. Projeto antigo sem `CRED`: rode `tty-projeto add <nome>` (mantém dir/porta). Em produção o
painel só atende `https://<PAINEL_DOMINIO>` e descarta conexões vindas da tailnet; em dev escuta em
`127.0.0.1`. Agentes/máquinas usam um token POR MÁQUINA (`<nome>.<HMAC(PAINEL_TOKEN, nome)>`, Bearer, entregue no convite; o
`PAINEL_TOKEN` mestre não sai do painel). Esse token só vale no plano-agente (`/api/agente/*`, registrar, evento, arquivo,
pacote) e `GET /api/config/*`, e só em nome da própria máquina: tarefas, convite, `PUT/POST` de config e `DELETE` são só de humano
(403 com token de máquina; `tty-push` exige `TTY_SENHA`). Sem `PAINEL_TOKEN`, o plano-agente é 401. Loopback NÃO é mais dono:
só com `PAINEL_LOCALHOST_LIVRE=1` (dev/testes), e request com `X-Forwarded-For` nunca conta como loopback. POST/PUT/DELETE e o
WebSocket do relé com `Origin` de outro host levam 403 (CSRF/CSWSH); `PAINEL_DOMINIO` é o ÚNICO Host aceito (421 para IP, localhost e qualquer outro nome, inclusive agentes e bootstrap; em produção o painel não sobe sem ele e sem TLS, e só atende HTTPS; cookie `__Host-Tessera`; convite e agente só usam `https://<domínio>`; o relé só alcança IP da tailnet). `PAINEL_SENHA`
com menos de 20 caracteres impede o start. Login humano = senha + código TOTP do app autenticador (`POST /api/login`; segredo em `PAINEL_TOTP_SECRET`, gere com `python3 painel.py --totp-novo`;
obrigatório em produção; cada código vale uma vez; sem 2FA configurado o Basic só existe em dev). Sessão: cookie `__Host-Tessera` (SameSite=Strict,
atrelado ao User-Agent) que vence 30 min após o login/última renovação; só `POST /api/sessao/renovar` (o front chama ao detectar atividade) estende,
o polling não; teto de 8 h; `POST /api/logout`. Expirada: F5, ação nova, terminal novo e reconexão pedem login, mas o terminal já aberto (WebSocket)
segue. Ações sensíveis vão para `dados/auditoria.log`. O relé só encaminha o WebSocket (`/ws`); `/token`, `/` e qualquer outro caminho do ttyd são 404 (o `/token` devolveria o CRED). O primeiro frame
(`AuthToken`) é reescrito pelo painel: o CRED nunca chega ao navegador. 5 códigos 2FA errados com a senha CERTA em 5 min pausam o 2FA e avisam no
Telegram; falhas por IPv6 contam por /64. **Limite conhecido:** o CRED aparece no argv do ttyd (`/proc/<pid>/cmdline`) para outros usuários da
MESMA máquina; o firewall (modo root) já bloqueia o loopback, mas em servidor com usuários não confiáveis monte `/proc` com `hidepid=2`.
Tetos de conexão: 256 simultâneas no total e 64 por IPv4 ou /64 IPv6; a requisição tem prazo TOTAL (10 s para linha+headers, 120 s + Content-Length/100 KB/s para o corpo; o relé de terminal não tem prazo).
Risco residual aceito: DoS volumétrico distribuído (muitos IPs) e o limite de 64 conexões por IP atrás de NAT compartilhado. O terminal passa SEMPRE pelo relé
(CSP `connect-src 'self'`). `tty-add` só extrai os `ITENS` da ferramenta (recusa links, `..` e arquivos fora deles). Proteção do login: 401 + atraso anti-brute-force,
`>5 falhas/60s → 429 + Retry-After`, com bloqueio que dobra na reincidência (60 s até 1 h). Exceções sem
sessão: `GET /bootstrap.sh` (instalador sem segredos, p/ a máquina nova pré-tailnet) e a tela de login. O relé `/tty/` exige
sessão autenticada, valida `?arg=` (`400` em `a/b`), não repassa `Authorization`/`Cookie`/`Origin`/
`Referer` e forja `Origin` igual ao host. Quem alcança o painel autenticado pode
pedir tarefas (lista fechada) aos agentes. Não afrouxe a lista de tarefas, não grave segredos
no repositório e trate qualquer mudança que amplie o acesso como mudança de segurança.

## 1. Modelo

- **Máquina**: entra na tailnet e roda o **agente** (`tty-agente`). Aparece como cartão na sidebar com CPU/MEM.
  Uma máquina pode ter **vários agentes** (um por usuário, ex. hospedagem multi-usuário): cada usuário sem root roda o
  próprio agente com o **mesmo `--nome`** de máquina.
- **Projeto**: `nome` + `diretório` + `usuário`, numa máquina. Um serviço systemd (root) ou uma entrada do
  supervisor (sem root) e uma porta por projeto. Porta padrão do primeiro: `7681`; os seguintes: livres
  desde `7700`. Toda máquina tem o projeto `shell` (diretório = HOME, porta 7681); sem root o padrão é
  `shell-<usuário>`.
- **Terminal**: sessão tmux `projeto/terminal`, criada ao abrir `ws://IP:PORTA/ws?arg=<terminal>`.
  O painel mostra até 4 na grade (desktop); no celular, um por vez com abas. `×` fecha só a vista
  (a sessão continua); "Encerrar" (paleta) mata a sessão via `tmux kill-session`.

## 2. Peças

| pasta/arquivo | o que é | detalhes |
|---|---|---|
| `painel/painel.py` | painel (porta `8787` por padrão): serve o Tessera, `/api/estado`, fila de tarefas, eventos → Telegram, convites, bootstrap/pacote/configs | só stdlib (`http.server`); `painel/.env` modo 600; dados em `painel/dados/` |
| `painel/static/` | a interface: `index.html`, `tessera.css`, `tessera.js` (+ `vendor/` xterm.js) | sem build; tema escuro/claro em `localStorage` |
| `painel/instalar-painel.sh` | instala/atualiza o painel em `/opt/tty-orquestrador` como systemd | preserva `.env`+`dados`; `--remover` só tira a unit |
| `agente/agente.py` | por máquina: heartbeat de CPU/MEM/branch/terminais/ferramentas; puxa e executa tarefas **de lista fechada** | heartbeat 3 s, polling de tarefas 1 s; roda como root (systemd) ou como o usuário (supervisor) |
| `projetos/` | `tty-projeto add/rm/ls`, `tty-sessao` (abre `projeto/terminal`), `tty-ttyd` (ttyd de um projeto), `tty-supervisor` (modo sem root) | um ttyd por projeto; `tty-sessao` usa `tmux new -A -s projeto/terminal` |
| `bootstrap/bootstrap.sh` | máquina nova: pacotes + tailnet + scripts + projeto padrão + vigia + agente + registro + self-test. Idempotente; `--remover` desfaz | com root (systemd) ou sem root (tudo no HOME + supervisor via systemd `--user`/cron) |
| `notificador/` | `tty-notify`, `shell-hook.sh` (comando longo terminou), `vigia.sh` (heurística no tmux, um por usuário) | fontes dos avisos; ver §7 |
| `facilitadores/` | `tty-add <ferramenta>` / `tty-push <ferramenta>`; uma ferramenta = um arquivo em `ferramentas/` (`claude.sh`, `opencode.sh`) | dois passos: `tty-push` guarda a config no painel, `tty-add` instala/aplica |
| `testes/shell.sh` + `test.sh` | integração dos scripts com painel real e tmux isolado; `painel/test_painel.py`, `agente/test_agente.py` | `./test.sh` roda tudo |

## 3. Interface Tessera — desktop

- **Sidebar (248 px)**: logo `>_ Tessera`, lista de máquinas (dot online, nome, IP no hover, `N vCPU · M GB`,
  barras CPU/MEM, botão `+` novo projeto) e, por máquina, os projetos (dot de estado, nome, `branch · N tty`,
  cpu/mem à direita, `×` no hover para remover/esquecer). Rodapé: `⚙ Configurações`.
- **Cabeçalho**: título do projeto (`nome` + `máquina · dir · cpu/mem`), busca `Buscar ou executar… (⌘K/Alt K)`,
  `Facilitadores`, `+ Novo TTY`.
- **Grade**: 1 coluna com 1 terminal, 2 colunas com 2+; com 3 terminais o primeiro ocupa 2 linhas.
  Sem terminal: tela vazia com CTA. Cada painel tem barra (dot, título, comando atual, `cpu% · mem`,
  `⟳` recarregar, `×` fechar vista) e o xterm abaixo; overlay "conectando…/reconectando…/máquina offline".
- **Rodapé**: `● N ativos`, `● N aguardando você` (âmbar, só se > 0), `N sessões · M máquinas`,
  `painel inacessível` se o `/api/estado` falhar.
- **Estados**: verde = rodando, cinza = ocioso/offline, **âmbar pulsante = aguardando você**
  (projeto, terminal, aba e título da página `(N) Tessera`).

## 4. Interface — celular (< 860 px)

Layout do design Mobile: cabeçalho (título, subtítulo, busca `⌕`, `+` novo terminal), páginas
**Projetos · Terminais · Facilitadores · Config** na navegação inferior, chips de terminais (36 px),
o terminal real, campo de comando `❯` (Enter envia `+\r`) e **barra de teclas**
(`esc`, `tab`, `ctrl-c`, `↑`, `↓`, `clear`) para o toque. O teclado redimensiona a página.
Projetos viram linhas grandes com `×` de 36×44 px; Facilitadores/Config reutilizam o mesmo conteúdo
do desktop dentro da página.

## 5. Paleta, atalhos e links

- **Paleta**: `⌘K` (Mac) / `Alt+K` (resto; `Ctrl+K` fora do terminal) ou botão de busca. Filtra por
  substring: projetos (`nome · máquina` se > 1 máquina) + ações (Novo TTY, Novo projeto…, Facilitadores,
  Configurações, alternar tema, Recarregar em foco/todos, Remover projeto, Reabrir main, 🗑 Limpar e voltar
  ao main, Encerrar este/outros/terminal).
  `↑↓` + Enter; `Esc` fecha.
- **Recarregar**: `⟳` por terminal ou paleta (foco/todos): fecha e reabre o websocket, `term.reset()`,
  o tmux redesenha. A sessão no servidor **não é tocada**.
- **Links do Telegram**: `#/maquina/projeto/terminal` — abrem o Tessera já no terminal certo;
  recarregar a página ou voltar no hash restaura seleção e foco.
- **Tabs = sessões (modelo VS Code)**: a tab É a sessão tmux. Clicar foca (e reabre o `main`
  fechado); o `×` da tab/pane mata (`terminal.encerrar`), com confirmação se há processo rodando.
  `🗑 Limpar` mata tudo e volta ao `main` limpo (`terminal.limpar`). Estado local (`fechados/extras`)
  é só otimismo + GC pelo heartbeat: nome morto não reaparece, e `+ Novo TTY` nunca nasce invisível.

## 6. Projetos — criar, listar, remover

- **Nova pasta numa máquina que já existe** (convite/bootstrap é só para máquina/usuário novo):
  - Tessera: `+` ao lado da máquina (ou paleta → "Novo projeto…"): nome, pasta e usuário.
    A pasta **autocompleta**: o agente lista subpastas (como o usuário, só pastas, sem ocultas até
    digitar `.`); `↑↓` navegam, **Tab** aceita (e sugere o Nome), Enter aceita se navegou,
    Esc fecha só a lista; `~/` vira caminho absoluto. Uma listagem por pasta-pai, cache 30 s.
  - No terminal da máquina, **dentro da pasta**: `tty-projeto add` (nome = nome da pasta;
    `sudo` se a instalação for de root). Formas: `tty-projeto add <nome> --dir D [--usuario U] [--porta N]`,
    `tty-projeto rm <nome>`, `tty-projeto ls`. Mudar o dir (`add <mesmo-nome> --dir NOVO`) reinicia
    o ttyd em até 5 s (sem root): novos terminais abrem no novo, os abertos ficam no antigo.
  - `bootstrap.sh --dir D [--projeto NOME]` só muda a pasta do projeto padrão da instalação.
- **O bootstrap não ressuscita projetos**: o projeto padrão (`shell`, `shell-<usuário>` ou o de
  `--dir`) só nasce na **primeira instalação**, quando pedido (`--projeto`/`--dir`), quando a
  instalação está **vazia**, ou como reparo. Reinstalar não recria o que você removeu —
  o log diz `NÃO recriado … use + no Tessera ou tty-projeto add`. O bootstrap **recusa**
  `--nome` diferente do já instalado (sugere adicionar pasta ou `--renomear`).
- **Remover vs esquecer**: com agente online o `×` pede confirmação e manda `projeto.rm`
  (desliga o ttyd; **pasta e tmux ficam**). Com agente offline o `×` vira **"Esquecer"**
  (`DELETE /api/maquinas/<m>/projetos/<p>`): só tira da tela; se o agente voltar e a pasta ainda
  estiver configurada, o projeto **reaparece**. Sem root, remover só vale para o próprio usuário.
- **Limitação conhecida (multi-usuário)**: numa máquina com vários agentes (ex. hospedagem multi-usuário),
  criar via Tessera **exige o usuário** (`400 informe o usuário` se omitido) e a sugestão de
  usuário vem dos projetos restantes — se você removeu o último projeto de um usuário, digite
  o usuário à mão (ex. Nome `shell-alice`, Pasta `/home/alice`, Usuário `alice`).

## 7. Avisos (terminou, aprovação, pergunta, erro, ocioso, online)

Nativo primeiro, heurística como rede de segurança; ligar/desligar cada motivo em
Configurações › Notificações (padrão: **ocioso e online desligados**; ocioso = 10 min).
O link do Telegram abre o Tessera no terminal certo. Terminal que pediu você fica com
**ponto âmbar pulsando** (painel, projeto e rodapé) até haver atividade nele.

| fonte | o que gera |
|---|---|
| Claude Code (`tty-add claude`: hooks `Notification` → `aprovacao`, `Stop` → `terminou`) | aprovação, terminou |
| OpenCode (`tty-add opencode`: plugin `tty-notify.js`, escolhido pela versão do OpenCode: v1 `session.idle`, v2 `session.execution.succeeded` → `terminou`; `permission.*` → `aprovacao`; `session.error`/`execution.failed` → `erro`) | terminou, aprovação, erro |
| Qualquer shell no tmux (`shell-hook.sh`: comando ≥ 15 s ou com erro; ignora `claude/opencode/vim/…`) | terminou, erro |
| `vigia.sh` (um por usuário; pergunta parada na tela, ex. `[y/N]/Approve/Password:` estável 3 s; ociosidade 10 min fora de shells) | aprovação, ocioso |
| `bootstrap` (self-test) | online (`instância NOME online`) |

Dedup de 5 s por (máquina, projeto, terminal, motivo, texto); texto cortado em 500 chars;
`PUT /api/config` alterna motivos; `GET /api/eventos?n=` lê o `eventos.log`.

## 8. Facilitadores (tty-add / tty-push)

Por usuário: cada agente age pelo **próprio usuário**, mesmo sem projetos (agente sem root sem
projetos continua servindo Facilitadores e autocomplete). Fluxo em dois passos:
**1)** numa máquina que já tem a ferramenta configurada, `tty-push <ferramenta>` (botão
"tty-push · enviar config", que envia a config da máquina do projeto aberto) guarda a config
no painel (`POST /api/config/<nome>`, tar.gz até 20 MB); **2)** nas outras,
`tty-add <ferramenta>` instala (se faltar o binário) e aplica essa config (baixa de
`GET /api/config/<nome>`). Sem config no painel, `tty-add` diz `SEM CONFIG` (instala/atualiza
só o plugin de avisos). `--sem-config` pula a config; `--so a,b` filtra itens.
O que o `tty-push` leva é o `ITENS` de `facilitadores/ferramentas/<f>.sh`:
`claude` = settings/CLAUDE.md/skills/commands/agents (**sem credenciais**);
`opencode` = `opencode.jsonc` **inteiro — confira chaves de API antes de enviar**.
Outra ferramenta: copie `ferramentas/_modelo.sh` (`BIN`, `DESC`, `ITENS`, `instalar`, `pos`).

## 9. Máquina nova (com sudo) ou usuário comum (sem sudo)

1. Tessera → ⚙ Configurações → Máquinas → nome → **Gerar convite** (exige login no painel;
   auth key Tailscale de uso único, 1 h; exige `TS_API_KEY` no `.env`) e cole o comando na máquina. Marcando
   **"Sem sudo"**, o comando não leva auth key nem `sudo`: serve para um usuário comum de uma
   máquina que **já está na tailnet** (ex. usuário de site em hospedagem multi-usuário).
2. O `bootstrap.sh` detecta sozinho se há root:
   - **com root**: instala pacotes (tmux, jq, ttyd, Tailscale), entra na tailnet (`--hostname=NOME`),
     scripts em `/usr/local`, conf em `/etc/tty-orquestrador`, serviços systemd
     (`tty-orquestrador-projeto-<p>`, `tty-orquestrador-vigia-<u>`, `tty-orquestrador-agente`).
     **Todos os usuários do mesmo servidor usam o MESMO `--nome`**.
   - **sem root**: tudo no HOME (`~/.local`, `~/.config/tty-orquestrador`); baixa `ttyd`/`jq`/`tmux`
     se faltarem; o `tty-supervisor` (flock, loop 5 s, log com teto 5 MB) mantém ttyd/vigia/agente
     vivos (systemd `--user` com linger, senão cron `@reboot` + a cada 5 min) e relê os projetos
     a cada 5 s (reinicia ttyd se o `.conf` mudar; derruba se removido).
3. Ela aparece sozinha (registro `POST /api/registrar` + self-test: TTY responde, painel lista,
   agente com heartbeat, aviso `online`). Novos projetos: paleta → "Novo projeto…" (ou
   `sudo tty-projeto add <nome> --dir D [--usuario U]`).
4. Desfazer: o mesmo comando com `--remover` (sem root, só tira aquele usuário — a máquina some
   do painel quando não sobra projeto; com root, tira a máquina inteira). Máquina offline pode
   ser tirada da tela em Configurações › Máquinas › remover. Pacotes, tailnet, pastas e tmux ficam.

## 10. Tarefas do agente (lista fechada)

O painel nunca executa shell arbitrário: o Tessera cria tarefas (`POST /api/tarefas`), o agente
da máquina puxa (polling 1 s) e executa com argv fixo (timeout 15 min). Estados:
`pendente → enviada → ok/erro`, ou `expirada` (10 min sem entrega); `GET /api/tarefas/<id>`
mostra `saida` (últimos 500 chars) e `dados` (só `pasta.listar`, até 30 KB).

| tipo | args | executa como |
|---|---|---|
| `projeto.add` | `nome`, `dir` (absoluto), `usuario?`, `porta?` | root (sem root: próprio usuário) → `tty-projeto add` |
| `projeto.rm` | `nome` | idem → `tty-projeto rm` |
| `terminal.novo` | `projeto`, `terminal?` (auto `t2…`) | dono do projeto → `tmux new-session -d -s proj/term -c dir` |
| `terminal.encerrar` | `projeto`, `terminal` | dono → `tmux kill-session -t =proj/term` |
| `terminal.limpar` | `projeto` | dono → `tmux kill-session` em cada `proj/*` (main inclusive; recria no próximo attach via `tty-sessao -A`) |
| `ferramenta.add` / `ferramenta.push` | `usuario` (dono), `ferramenta` | usuário → `tty-add` / `tty-push` |
| `pasta.listar` | `usuario`, `dir`, `ocultas?` | usuário → `agente.py --listar` (só pastas, máx. 300, `~` expande) |

Roteamento multi-agente: pelo dono do projeto, senão pelo usuário; sem alvo e > 1 agente,
`projeto.add`/`ferramenta.*` exigem `usuario` (400). Usuário válido = dono de projeto ou
(sem root) o próprio usuário do agente.

### Mandar arquivos

`tty-notify --arquivo CAMINHO [legenda...]` envia o arquivo ao Telegram como documento
(legenda: `📎 máquina · projeto/terminal` + o texto). O cliente faz `POST /api/arquivo` no painel
(corpo = bytes do arquivo; query `maquina`, `projeto`, `terminal`, `nome`, `legenda`) e o painel
repassa via `sendDocument`. Limite 50 MB (teto do Telegram; acima disso 413). Diferente dos avisos,
sai com erro se o arquivo não existir ou o envio falhar, e ignora o liga/desliga de motivos.
**Precisa do painel atualizado** (`painel/instalar-painel.sh` no servidor); painel antigo responde 404.

## 11. Configuração do painel (`painel/.env`, modo 600)

`TS_API_KEY` (+`TS_TAG` opcional) para convites com sudo, `TG_TOKEN` e `TG_CHAT` para o Telegram.
No servidor do painel: `/opt/tty-orquestrador/painel/.env`; depois de editar:
`sudo systemctl restart tty-orquestrador-painel`. Atualizar o painel:
`painel/instalar-painel.sh` rodado na cópia (preserva `.env` e `dados/`).
Produção (sem elas o painel não sobe): `PAINEL_DOMINIO`, `PAINEL_SENHA` (≥ 20), `PAINEL_TOTP_SECRET`,
`PAINEL_TLS_CERT`/`PAINEL_TLS_KEY`. `PAINEL_TOKEN` (≥ 32) é o mestre dos tokens de agente.
Bind/porta/dados: `PAINEL_BIND` (padrão: `0.0.0.0` em produção, `127.0.0.1` em dev), `PAINEL_PORTA` (8787),
`PAINEL_ATRAS_PROXY=1` (só atende o proxy local; exige bind em loopback),
`PAINEL_LOCALHOST_LIVRE=1` (só dev/testes: loopback sem senha),
`PAINEL_PUBLICO` (IP/host público p/ o download do instalador no convite),
`PAINEL_DADOS` (`maquinas.json`, `eventos.log`, `config.json`, `configs/*.tar.gz`).

## 12. Exemplo de inventário (fictício — não usar nomes/IPs reais aqui)

- `srv-painel` — painel (`:8787`) + projeto `shell` (`:7681`, usuário `dev`)
- `srv-dev` (PC) — projetos `site` (`:7700`) e `api` (`:7701`), todos usuário `dev`
  (ver `/etc/tty-orquestrador/projetos/*.conf`)
- `srv-hospedagem` — multi-usuário: agentes `alice` e `bob` online **sem projetos**
  (recriar via §6)

## 13. Testes

`./test.sh`: unitários do painel (`painel/test_painel.py`) + do agente (`agente/test_agente.py`) +
integração (`testes/shell.sh`: sobe painel real local + tmux isolado; cobre notify, shell-hook bash,
vigia, projetos, agente ponta-a-ponta, bootstrap sem-root com 2 usuários, facilitadores;
`TTY_SEM_PERSISTENCIA=1`, `TTY_IP`, `TTY_ROOT_CONF` e `TTY_AGENTE_ID` só para testes).
