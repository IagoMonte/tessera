#!/usr/bin/env bash
# Testes dos scripts de shell contra um painel real (local, sem Telegram) e um tmux isolado.
set -uo pipefail
RAIZ=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); PORTA=$((20000 + RANDOM % 10000))
falhas=0
ok()   { printf '  ok   %s\n' "$1"; }
erro() { printf '  FALHA %s\n' "$1"; falhas=$((falhas + 1)); }
checa() { if eval "$2"; then ok "$1"; else erro "$1"; fi; }

cleanup() { [ -f "$T/uhome/.local/state/tty-orquestrador/supervisor.pid" ] && kill "$(cat "$T/uhome/.local/state/tty-orquestrador/supervisor.pid")" 2>/dev/null; kill "$PAINEL_PID" "${AGENTE_PID:-}" 2>/dev/null; tmux -L tty-teste kill-server 2>/dev/null; TMUX_TMPDIR=$T/tmux tmux kill-server 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

PAINEL_BIND=127.0.0.1 PAINEL_PORTA=$PORTA PAINEL_DADOS=$T/dados python3 "$RAIZ/painel/painel.py" >/dev/null 2>&1 &
PAINEL_PID=$!
for _ in $(seq 20); do curl -fs "http://127.0.0.1:$PORTA/api/maquinas" >/dev/null && break; sleep 0.2; done
printf 'PAINEL_URL=http://127.0.0.1:%s\nMAQUINA=teste\n' "$PORTA" > "$T/conf"
export TTY_CONF=$T/conf PATH="$RAIZ/notificador:$RAIZ/facilitadores:$RAIZ/projetos:$PATH"
eventos() { curl -fs "http://127.0.0.1:$PORTA/api/eventos?n=50"; }

echo "tty-notify"
tty-notify aprovacao "Allow Bash?"
checa "evento chega ao painel" 'eventos | jq -e "any(.[]; .motivo==\"aprovacao\" and .texto==\"Allow Bash?\" and .maquina==\"teste\")" >/dev/null'
echo '{"message":"Claude precisa da sua permissão"}' | tty-notify --claude aprovacao
checa "--claude lê a mensagem do hook" 'eventos | jq -e "any(.[]; .texto==\"Claude precisa da sua permissão\")" >/dev/null'
echo '{}' | tty-notify --claude terminou
checa "--claude sem mensagem tem texto padrão" 'eventos | jq -e "any(.[]; .texto==\"Claude Code terminou\")" >/dev/null'
TTY_SESSAO=api/t2 tty-notify aprovacao "no projeto"
checa "sessão projeto/terminal vira campos do evento" 'eventos | jq -e "any(.[]; .projeto==\"api\" and .terminal==\"t2\" and .texto==\"no projeto\")" >/dev/null'
checa "painel fora do ar não quebra o chamador" 'PAINEL_URL=http://127.0.0.1:1 tty-notify terminou x; [ $? -eq 0 ]'
checa "sem PAINEL_URL não quebra" 'TTY_CONF=/nao/existe tty-notify terminou x; [ $? -eq 0 ]'

echo "shell-hook (bash)"
TMUX=1 TTY_MIN_SEGUNDOS=2 TTY_SESSAO=hook bash --norc -i >/dev/null 2>&1 <<EOS
. $RAIZ/notificador/shell-hook.sh
sleep 2
bash -c "sleep 2; exit 3"
sleep 0.1
exit
EOS
TMUX=1 TTY_MIN_SEGUNDOS=1 TTY_SESSAO=hookarray bash --norc -i >/dev/null 2>&1 <<EOS
precmd_outro() { :; }
PROMPT_COMMAND=(precmd_outro "true")
. $RAIZ/notificador/shell-hook.sh
sleep 1.2
exit
EOS
TMUX=1 TTY_MIN_SEGUNDOS=1 TTY_SESSAO=hooksobrescrito bash --norc -i >/dev/null 2>&1 <<EOS
. $RAIZ/notificador/shell-hook.sh
PROMPT_COMMAND='printf "\\033]0;titulo\\007"'
true
sleep 1.2

sleep 1.2; echo fim
exit
EOS
sleep 1
checa "PROMPT_COMMAND sobrescrito depois: ainda avisa, com a linha completa" 'eventos | jq -e "any(.[]; .sessao==\"hooksobrescrito\" and .texto==\"sleep 1.2; echo fim (2s)\" or (.sessao==\"hooksobrescrito\" and (.texto|startswith(\"sleep 1.2; echo fim\"))))" >/dev/null'
checa "PROMPT_COMMAND sobrescrito: nada vindo do prompt" 'eventos | jq -e "all(.[] | select(.sessao==\"hooksobrescrito\"); (.texto|test(\"printf|__tty\"))|not)" >/dev/null'
checa "linha vazia não gera aviso" 'eventos | jq -e "[.[]|select(.sessao==\"hooksobrescrito\")]|length<=2" >/dev/null'
sleep 1
checa "PROMPT_COMMAND array: texto é o comando do usuário, não o do prompt" 'eventos | jq -e "any(.[]; .sessao==\"hookarray\" and (.texto|startswith(\"sleep 1.2\"))) and (all(.[]; (.texto|startswith(\"precmd\"))|not))" >/dev/null'
checa "comando longo ok -> terminou" 'eventos | jq -e "any(.[]; .motivo==\"terminou\" and (.texto|startswith(\"sleep 2\")))" >/dev/null'
checa "comando longo com falha -> erro com código" 'eventos | jq -e "any(.[]; .motivo==\"erro\" and (.texto|contains(\"saiu com 3\")))" >/dev/null'
checa "comando curto não avisa" '! eventos | jq -e "any(.[]; .texto|startswith(\"sleep 0.1\"))" >/dev/null'

echo "vigia (tmux isolado)"
export TTY_TMUX="-L tty-teste" TTY_ESTADO=$T/estado TTY_ESTAVEL=1 TTY_OCIOSO=2
tmux -L tty-teste new-session -d -s pergunta "printf 'Remover tudo? Continue? [y/N] '; sleep 60"
tmux -L tty-teste new-session -d -s quieto "echo trabalhando; sleep 60"
vigia.sh --uma-vez; sleep 2.5; vigia.sh --uma-vez; sleep 0.5
checa "pergunta y/N -> aprovacao com a sessão" 'eventos | jq -e "any(.[]; .motivo==\"aprovacao\" and .sessao==\"pergunta\")" >/dev/null'
checa "programa parado sem pergunta -> ocioso" 'eventos | jq -e "any(.[]; .motivo==\"ocioso\" and .sessao==\"quieto\")" >/dev/null'
n1=$(eventos | jq length); vigia.sh --uma-vez; sleep 0.5; n2=$(eventos | jq length)
checa "não repete o mesmo aviso" '[ "$n1" = "$n2" ]'
checa "shell ocioso não gera aviso" '! eventos | jq -e "any(.[]; .motivo==\"ocioso\" and (.texto|test(\"^(bash|zsh) \")))" >/dev/null'
unset TTY_TMUX

echo "projetos (tty-projeto, tty-sessao)"
export TTY_ETC=$T/etc TTY_SYSTEMD_DIR=$T/sd TTY_SEM_SYSTEMD=1 TMUX_TMPDIR=$T/tmux
mkdir -p "$T/p1" "$T/p2" "$T/tmux"; EU=$(id -un)
tty-projeto add api --dir "$T/p1" --usuario "$EU" --porta 7711 >/dev/null
checa "add grava conf com dir, usuário e porta" '( . "$T/etc/projetos/api.conf" && [ "$DIR" = "$T/p1" ] && [ "$USUARIO" = "$EU" ] && [ "$PORTA" = 7711 ] )'
checa "add gera a unit do projeto com User e tty-ttyd" 'grep -q "^User=$EU" "$T/sd/tty-orquestrador-projeto-api.service" && grep -q "ExecStart=/usr/local/bin/tty-ttyd api" "$T/sd/tty-orquestrador-projeto-api.service"'
checa "add garante um vigia por usuário" '[ -f "$T/sd/tty-orquestrador-vigia-$EU.service" ]'
tty-projeto add api --dir "$T/p1" --usuario "$EU" >/dev/null
checa "add repetido mantém a porta (idempotente)" '( . "$T/etc/projetos/api.conf" && [ "$PORTA" = 7711 ] )'
tty-projeto add web --dir "$T/p2" --usuario "$EU" >/dev/null
checa "porta automática >= 7700 e diferente das usadas" '( . "$T/etc/projetos/web.conf" && [ "$PORTA" -ge 7700 ] && [ "$PORTA" != 7711 ] )'
checa "porta duplicada é recusada" '! tty-projeto add dup --dir "$T/p2" --usuario "$EU" --porta 7711 >/dev/null 2>&1'
checa "nome inválido é recusado" '! tty-projeto add "a b" --dir "$T/p1" >/dev/null 2>&1 && ! tty-projeto add ../x --dir "$T/p1" >/dev/null 2>&1'
checa "diretório inexistente é recusado" '! tty-projeto add z --dir "$T/nao-existe" >/dev/null 2>&1'
checa "usuário inexistente é recusado" '! tty-projeto add z --dir "$T/p1" --usuario usuario_que_nao_existe >/dev/null 2>&1'
LS=$(tty-projeto ls)
checa "ls lista os projetos" 'grep -q "^api .*7711" <<<"$LS" && grep -q "^web " <<<"$LS"'
checa "tty-projeto funciona sem HOME (o agente roda como root no systemd, sem HOME)" 'out=$(env -u HOME tty-projeto ls 2>&1) && grep -q "^api " <<<"$out"'
checa "tty-notify e tty-ttyd não quebram sem HOME" 'env -u HOME tty-notify terminou x; [ $? -eq 0 ] && out=$(env -u HOME TTY_IFACE=lo bash -c "tty-ttyd ../x" 2>&1); grep -q "inválido" <<<"$out"'
checa "rm remove conf e unit" 'tty-projeto rm web >/dev/null && [ ! -e "$T/etc/projetos/web.conf" ] && [ ! -e "$T/sd/tty-orquestrador-projeto-web.service" ]'

checa "tty-sessao recusa terminal inválido" 'tty-sessao api "$T/p1" "a;b" 2>/dev/null; [ $? -eq 2 ]'
checa "tty-sessao recusa projeto inválido" 'tty-sessao "../x" "$T/p1" main 2>/dev/null; [ $? -eq 2 ]'
checa "tty-sessao recusa diretório inexistente" 'tty-sessao api "$T/nao-existe" main 2>/dev/null; [ $? -eq 2 ]'
timeout 3 script -qec "tty-sessao api $T/p1 logs" /dev/null >/dev/null 2>&1
checa "tty-sessao cria a sessão projeto/terminal no diretório do projeto" 'tmux list-sessions -F "#{session_name}" | grep -qx "api/logs" && [ "$(tmux display-message -p -t api/logs "#{pane_current_path}")" = "$T/p1" ]'
checa "tty-sessao esconde a status line do tmux" '[ "$(tmux show-options -t api/logs -v status)" = off ]'
checa "tty-sessao segue o último cliente (window-size latest)" '[ "$(tmux show-options -t api/logs -v window-size)" = latest ]'

echo "agente (ponta a ponta com painel real e tmux isolado)"
curl -fs -X POST -d '{"nome":"teste","ip":"127.0.0.1","porta":7711}' "http://127.0.0.1:$PORTA/api/registrar" >/dev/null
tmux new-session -d -s api/main -c "$T/p1" "sleep 120"
TTY_INTERVALO=1 python3 "$RAIZ/agente/agente.py" >"$T/agente.log" 2>&1 &
AGENTE_PID=$!
estado() { curl -fs "http://127.0.0.1:$PORTA/api/estado"; }
esperar() { for _ in $(seq 1 ${2:-15}); do eval "$1" && return 0; sleep 0.5; done; return 1; }
checa "máquina aparece online com núcleos e RAM" 'esperar "estado | jq -e \".maquinas[0] | select(.online and .cores>0 and .ram_mb>0)\" >/dev/null"'
checa "projeto e terminal chegam com comando e porta" 'estado | jq -e ".maquinas[0].projetos[] | select(.nome==\"api\" and .porta==7711 and (.terminais|any(.nome==\"main\" and .cmd==\"sleep\" and .estado==\"rodando\")))" >/dev/null'
tid=$(curl -fs -X POST -d '{"maquina":"teste","tipo":"terminal.novo","args":{"projeto":"api"}}' "http://127.0.0.1:$PORTA/api/tarefas" | jq -r .id)
checa "tarefa terminal.novo cria a sessão api/t2 e termina ok" 'esperar "curl -fs http://127.0.0.1:$PORTA/api/tarefas/$tid | jq -e \".estado==\\\"ok\\\"\" >/dev/null" 20 && tmux list-sessions -F "#{session_name}" | grep -qx "api/t2"'
tid=$(curl -fs -X POST -d '{"maquina":"teste","tipo":"ferramenta.add","args":{"usuario":"root","ferramenta":"claude"}}' "http://127.0.0.1:$PORTA/api/tarefas" | jq -r .id)
checa "tarefa fora da lista segura é recusada pelo agente" 'esperar "curl -fs http://127.0.0.1:$PORTA/api/tarefas/$tid | jq -e \".estado==\\\"erro\\\" and (.saida|startswith(\\\"recusada\\\"))\" >/dev/null" 20'
tid=$(curl -fs -X POST -d '{"maquina":"teste","tipo":"terminal.encerrar","args":{"projeto":"api","terminal":"t2"}}' "http://127.0.0.1:$PORTA/api/tarefas" | jq -r .id)
checa "tarefa terminal.encerrar remove a sessão" 'esperar "curl -fs http://127.0.0.1:$PORTA/api/tarefas/$tid | jq -e \".estado==\\\"ok\\\"\" >/dev/null" 20 && ! tmux list-sessions -F "#{session_name}" | grep -qx "api/t2"'
mkdir -p "$T/p1/alfa" "$T/p1/beta" "$T/p1/.oculta"; : > "$T/p1/arquivo.txt"
lista_pastas() {  # lista_pastas <dir> [ocultas] -> imprime o JSON da tarefa concluída
  local id; id=$(curl -fs -X POST -d "{\"maquina\":\"teste\",\"tipo\":\"pasta.listar\",\"args\":{\"usuario\":\"$EU\",\"dir\":\"$1\",\"ocultas\":${2:-false}}}" "http://127.0.0.1:$PORTA/api/tarefas" | jq -r .id)
  for _ in $(seq 1 40); do
    local t; t=$(curl -fs "http://127.0.0.1:$PORTA/api/tarefas/$id")
    case $(jq -r .estado <<<"$t") in ok|erro) echo "$t"; return 0 ;; esac
    sleep 0.1
  done; return 1
}
T0=$(date +%s.%N); R1=$(lista_pastas "$T/p1/"); T1=$(date +%s.%N)
checa "pasta.listar devolve só as subpastas (sem arquivos nem ocultas), pelo agente real" 'jq -e ".estado==\"ok\" and .dados.pastas==[\"alfa\",\"beta\"] and .dados.dir==\"$T/p1\"" <<<"$R1" >/dev/null'
checa "autocompletar responde em menos de 3 s (consulta leve de 1 s)" 'awk -v a="$T0" -v b="$T1" "BEGIN{exit !((b-a) < 3.0)}"'
checa "pasta.listar com ocultas=true inclui .oculta" 'jq -e ".dados.pastas | index(\".oculta\") != null" <<<"$(lista_pastas "$T/p1/" true)" >/dev/null'
checa "pasta inexistente volta com erro claro" 'jq -e ".estado==\"erro\" and .saida==\"a pasta não existe\"" <<<"$(lista_pastas "$T/p1/nao-existe/")" >/dev/null'
checa "caminho relativo é recusado" 'jq -e ".estado==\"erro\" and (.saida|test(\"absoluto\"))" <<<"$(lista_pastas "relativo/")" >/dev/null'
checa "~ expande para o HOME do usuário e devolve o home" 'jq -e ".estado==\"ok\" and .dados.home==\"$HOME\" and .dados.dir==\"$HOME\"" <<<"$(lista_pastas "~")" >/dev/null'
kill "$AGENTE_PID" 2>/dev/null
[ "$falhas" -gt 0 ] && [ -s "$T/agente.log" ] && sed 's/^/    [agente] /' "$T/agente.log" | head -20
unset TTY_ETC TTY_SYSTEMD_DIR TTY_SEM_SYSTEMD TMUX_TMPDIR

echo "bootstrap sem root (HOME falso, painel real, TTY em lo; nada de sudo/cron/systemd da máquina)"
UH=$T/uhome; mkdir -p "$UH" "$T/tmux"; PU=$((12000 + RANDOM % 7000))
como_u() { env -u TTY_ETC -u TTY_CONF -u TTY_SEM_SYSTEMD -u TTY_SYSTEMD_DIR HOME="$UH" PATH="$UH/.local/bin:$PATH" \
  TTY_ROOT_CONF=/nao/existe TTY_IP=127.0.0.1 TTY_IFACE=lo TTY_SEM_PERSISTENCIA=1 TTY_SUP_INTERVALO=1 TMUX_TMPDIR="$T/tmux" "$@"; }
printf 'umask 007' > "$UH/.bashrc"   # .bashrc SEM quebra de linha final: o marcador não pode colar nele
SAIDA=$(como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta "$PU" --projeto uma 2>&1); RC=$?
checa "bootstrap como usuário comum termina ok e anuncia o modo" '[ "$RC" = 0 ] && grep -q "modo sem root" <<<"$SAIDA" && grep -q "pronto." <<<"$SAIDA"'
[ "$RC" != 0 ] && sed 's/^/    [bootstrap] /' <<<"$SAIDA" | tail -15
checa "scripts e config ficam no HOME do usuário" '[ -x "$UH/.local/bin/tty-projeto" ] && [ -x "$UH/.local/bin/tty-supervisor" ] && grep -q "^MAQUINA=umaq" "$UH/.config/tty-orquestrador/conf" && grep -q "^PORTA=$PU" "$UH/.config/tty-orquestrador/projetos/uma.conf"'
checa ".bashrc sem quebra de linha final continua válido e a linha do usuário intacta" 'bash -n "$UH/.bashrc" && grep -qx "umask 007" "$UH/.bashrc"'
checa "bloco do ~/.bashrc existe uma vez só" '[ "$(grep -c "^# >>> tty-orquestrador >>>" "$UH/.bashrc")" = 1 ]'
python3 - "$UH/.bashrc" <<'EOF'
import sys
p = sys.argv[1]; t = open(p).read()
open(p, "w").write(t.replace("\n# >>> tty-orquestrador >>>", "# >>> tty-orquestrador >>>", 1))   # como as versões antigas deixavam
EOF
checa "(pré-condição) o arquivo ficou quebrado como nas versões antigas" '! bash -n "$UH/.bashrc" 2>/dev/null && grep -q "umask 007# >>> tty-orquestrador" "$UH/.bashrc"'
como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta "$PU" --projeto uma >/dev/null 2>&1
checa "rodar o bootstrap de novo REPARA o .bashrc quebrado" 'bash -n "$UH/.bashrc" && grep -qx "umask 007" "$UH/.bashrc" && [ "$(grep -c "^# >>> tty-orquestrador >>>" "$UH/.bashrc")" = 1 ]'
checa "rodar de novo é idempotente (bashrc e supervisor)" '[ "$(grep -c "^# >>> tty-orquestrador >>>" "$UH/.bashrc")" = 1 ] && [ "$(ps -eo args | grep -c "$UH/.local/bin/[t]ty-supervisor")" = 1 ]'
checa "painel enxerga a máquina online com o projeto na porta" 'estado | jq -e ".maquinas[] | select(.nome==\"umaq\" and .online) | .projetos[] | select(.nome==\"uma\" and .porta==$PU)" >/dev/null'
checa "a TTY web do projeto responde" 'curl -fs -o /dev/null "http://127.0.0.1:$PU/"'
porta_pid() { ss -ltnp 2>/dev/null | grep ":$1 " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2; }
OLD=$(porta_pid "$PU"); kill "$OLD" 2>/dev/null
checa "supervisor religa o ttyd que caiu" 'esperar "NEW=\$(porta_pid $PU); [ -n \"\$NEW\" ] && [ \"\$NEW\" != \"$OLD\" ]" 20'
como_u tty-projeto add extra --dir "$T/p1" --porta $((PU + 1)) >/dev/null 2>&1
checa "tty-projeto add sem root sobe o ttyd do projeto novo" 'esperar "curl -fs -o /dev/null http://127.0.0.1:$((PU + 1))/" 20'
como_u tty-projeto rm extra >/dev/null 2>&1
checa "tty-projeto rm sem root derruba o ttyd" 'esperar "! curl -fs -o /dev/null -m 1 http://127.0.0.1:$((PU + 1))/" 20'
checa "tty-projeto sem root não pede root nem systemd" '! grep -q "rode como root" <<<"$(como_u tty-projeto ls 2>&1)"'

echo "  adicionar PASTAS (projetos) sem confundir com instalar/renomear a máquina"
mkdir -p "$T/pokerrange teste" "$T/dir-extra"
( cd "$T/pokerrange teste" && como_u tty-projeto add --porta $((PU + 3)) >/dev/null 2>&1 )
checa "tty-projeto add dentro da pasta: nome = nome da pasta ajustado, dir = pasta" '( . "$UH/.config/tty-orquestrador/projetos/pokerrange-teste.conf" && [ "$DIR" = "$T/pokerrange teste" ] && [ "$PORTA" = $((PU + 3)) ] )'
como_u tty-projeto rm pokerrange-teste >/dev/null 2>&1
SAIDAR=$(como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome outronome --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" 2>&1); RCR=$?
checa "bootstrap com outro --nome numa máquina já instalada recusa e explica como adicionar pasta" '[ "$RCR" != 0 ] && grep -q "já está instalada como .umaq." <<<"$SAIDAR" && grep -q "tty-projeto add" <<<"$SAIDAR" && grep -q -- "--renomear" <<<"$SAIDAR"'
checa "a recusa não mexeu na configuração nem no painel" 'grep -q "^MAQUINA=umaq" "$UH/.config/tty-orquestrador/conf" && ! estado | jq -e ".maquinas[] | select(.nome==\"outronome\")" >/dev/null'
printf 'MAQUINA=raiz\nPAINEL_URL=x\n' > "$T/rootconf"; mkdir -p "$T/uhome3"
SAIDAR=$(env HOME="$T/uhome3" PATH="$PATH" TTY_ROOT_CONF="$T/rootconf" bash "$RAIZ/bootstrap/bootstrap.sh" --nome outro --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" 2>&1); RCR=$?
checa "sem root num servidor que já é outra máquina (instalação com root): recusa e sugere --nome <a máquina>" '[ "$RCR" != 0 ] && grep -q "já é a máquina .raiz." <<<"$SAIDAR" && grep -q -- "--nome raiz" <<<"$SAIDAR"'
como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta "$PU" --projeto uma --dir "$T/dir-extra" >/dev/null 2>&1
checa "--dir sem --projeto nomeia o projeto pela pasta, com o diretório dela" '( . "$UH/.config/tty-orquestrador/projetos/uma.conf" ) && [ -f "$UH/.config/tty-orquestrador/projetos/uma.conf" ] && ( . "$UH/.config/tty-orquestrador/projetos/uma.conf" && [ "$DIR" = "$T/dir-extra" ] )'
como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta "$PU" --projeto uma --dir "$UH" >/dev/null 2>&1
checa "voltar o --dir ao HOME restaura o projeto padrão" '( . "$UH/.config/tty-orquestrador/projetos/uma.conf" && [ "$DIR" = "$UH" ] )'
SAIDAD=$(como_u bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --dir "$T/nao-existe" 2>&1); RCD=$?
checa "--dir inexistente é recusado" '[ "$RCD" != 0 ] && grep -q "a pasta não existe" <<<"$SAIDAD"'

echo "  mudar o diretório de um projeto existente reinicia o ttyd dele"
abre_sessao() {  # abre ws://127.0.0.1:$1/ws?arg=$2 (o ttyd cria a sessão tmux) e a mantém aberta por 3 s
  python3 - "$1" "$2" <<'EOF'
import base64, json, os, socket, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.sendall((f"GET /ws?arg={sys.argv[2]} HTTP/1.1\r\nHost: 127.0.0.1:{sys.argv[1]}\r\nOrigin: http://127.0.0.1:{sys.argv[1]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: tty\r\n\r\n").encode())
r = b""
while b"\r\n\r\n" not in r: r += s.recv(1)
d = json.dumps({"AuthToken": "", "columns": 80, "rows": 24}).encode(); m = os.urandom(4)
s.sendall(bytes([0x81, 0x80 | len(d)]) + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))
time.sleep(3)
EOF
}
PM=$((PU + 2))
como_u tty-projeto add mud --dir "$T/p1" --porta "$PM" >/dev/null 2>&1
checa "projeto mud sobe com o diretório p1" 'esperar "curl -fs -o /dev/null http://127.0.0.1:$PM/" 20'
OLDM=$(porta_pid "$PM")
como_u tty-projeto add mud --dir "$T/p1" --porta "$PM" >/dev/null 2>&1; sleep 8
checa "add idêntico (mesmo diretório) não reinicia o ttyd" '[ "$(porta_pid "$PM")" = "$OLDM" ]'
OUTM=$(como_u tty-projeto add mud --dir "$T/p2" --porta "$PM" 2>&1)
checa "mudar o diretório avisa o que acontece com as sessões abertas" 'grep -q "diretório alterado" <<<"$OUTM" && grep -q "já abertos ficam" <<<"$OUTM"'
checa "o supervisor reinicia o ttyd (PID novo, porta de volta)" 'esperar "N=\$(porta_pid $PM); [ -n \"\$N\" ] && [ \"\$N\" != \"$OLDM\" ]" 25'
abre_sessao "$PM" novo
checa "terminal novo abre no diretório novo (p2)" 'esperar "TMUX_TMPDIR=$T/tmux tmux display-message -p -t mud/novo \"#{pane_current_path}\" 2>/dev/null | grep -qx \"$T/p2\"" 20'
como_u tty-projeto rm mud >/dev/null 2>&1
checa "rm do projeto derruba o ttyd" 'esperar "! curl -fs -o /dev/null -m 1 http://127.0.0.1:$PM/" 20'

echo "  dois usuários no mesmo servidor = UMA máquina com dois projetos"
UH2=$T/uhome2; mkdir -p "$UH2"; EU=$(id -un)
como_u2() { env -u TTY_ETC -u TTY_CONF -u TTY_SEM_SYSTEMD -u TTY_SYSTEMD_DIR HOME="$UH2" PATH="$UH2/.local/bin:$PATH" TTY_AGENTE_ID=u2 \
  TTY_ROOT_CONF=/nao/existe TTY_IP=127.0.0.1 TTY_IFACE=lo TTY_SEM_PERSISTENCIA=1 TTY_SUP_INTERVALO=1 TMUX_TMPDIR="$T/tmux" "$@"; }
SAIDA2=$(como_u2 bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta $((PU + 5)) 2>&1); RC2=$?
[ "$RC2" != 0 ] && sed 's/^/    [bootstrap2] /' <<<"$SAIDA2" | tail -10
checa "segundo usuário instala sem erro e o projeto padrão é shell-<usuário>" '[ "$RC2" = 0 ] && [ -f "$UH2/.config/tty-orquestrador/projetos/shell-$EU.conf" ]'
checa "continua UMA máquina, agora com dois projetos" 'esperar "estado | jq -e \"[.maquinas[] | select(.nome==\\\"umaq\\\")] | length==1 and (.[0].projetos | map(.nome) | sort == [\\\"shell-$EU\\\",\\\"uma\\\"])\" >/dev/null" 20'
checa "dois agentes online na mesma máquina" 'esperar "estado | jq -e \"[.maquinas[] | select(.nome==\\\"umaq\\\")][0].agentes | (map(.id) | sort) == [\\\"$EU\\\", \\\"u2\\\"] and all(.online)\" >/dev/null" 20'
checa "terminal do projeto do segundo usuário abre" 'curl -fs -o /dev/null "http://127.0.0.1:$((PU + 5))/"'
tid=$(curl -fs -X POST -d "{\"maquina\":\"umaq\",\"tipo\":\"terminal.novo\",\"args\":{\"projeto\":\"shell-$EU\"}}" "http://127.0.0.1:$PORTA/api/tarefas" | jq -r .id)
checa "tarefa do projeto do 2º usuário é executada pelo agente dele" 'esperar "curl -fs http://127.0.0.1:$PORTA/api/tarefas/$tid | jq -e \".estado==\\\"ok\\\"\" >/dev/null" 20 && TMUX_TMPDIR="$T/tmux" tmux list-sessions -F "#{session_name}" | grep -q "^shell-$EU/t2$"'
como_u2 tty-projeto add outro2 --dir "$T/p2" --porta $((PU + 7)) >/dev/null 2>&1   # u2 passa a ter OUTRO projeto além do padrão
como_u2 tty-projeto rm "shell-$EU" >/dev/null 2>&1
SAIDA3=$(como_u2 bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta $((PU + 5)) 2>&1); RC3=$?
[ "$RC3" != 0 ] && sed 's/^/    [bootstrap3] /' <<<"$SAIDA3" | tail -8
checa "reinstalar/atualizar NÃO ressuscita o projeto padrão que o usuário removeu (ele tem outros projetos)" '[ "$RC3" = 0 ] && [ ! -f "$UH2/.config/tty-orquestrador/projetos/shell-$EU.conf" ] && grep -q "NÃO recriado" <<<"$SAIDA3"'
checa "...e o agente reinstalado continua online na máquina" 'esperar "estado | jq -e \".maquinas[] | select(.nome==\\\"umaq\\\") | .agentes[] | select(.id==\\\"u2\\\" and .online)\" >/dev/null" 20'
checa "...e o projeto removido não volta ao painel" 'esperar "! estado | jq -e \".maquinas[] | select(.nome==\\\"umaq\\\") | .projetos[] | select(.nome==\\\"shell-$EU\\\")\" >/dev/null" 20'
como_u2 tty-projeto rm outro2 >/dev/null 2>&1   # agora a instalação do u2 está VAZIA
SAIDA4=$(como_u2 bash "$RAIZ/bootstrap/bootstrap.sh" --nome umaq --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" --porta $((PU + 5)) 2>&1); RC4=$?
[ "$RC4" != 0 ] && sed 's/^/    [bootstrap4] /' <<<"$SAIDA4" | tail -8
checa "instalação SEM nenhum projeto: o bootstrap recria o padrão (senão nada aparece no painel)" '[ "$RC4" = 0 ] && [ -f "$UH2/.config/tty-orquestrador/projetos/shell-$EU.conf" ] && ! grep -q "NÃO recriado" <<<"$SAIDA4"'
checa "...e ele aparece no painel" 'esperar "estado | jq -e \".maquinas[] | select(.nome==\\\"umaq\\\") | .projetos[] | select(.nome==\\\"shell-$EU\\\")\" >/dev/null" 20'
como_u2 bash "$RAIZ/bootstrap/bootstrap.sh" --remover --nome umaq --painel "http://127.0.0.1:$PORTA" >/dev/null 2>&1
checa "remover o 2º usuário não apaga a máquina nem o projeto do 1º" 'esperar "estado | jq -e \"[.maquinas[] | select(.nome==\\\"umaq\\\")] | length==1 and (.[0].projetos | map(.nome) == [\\\"uma\\\"])\" >/dev/null" 10 && curl -fs -o /dev/null "http://127.0.0.1:$PU/"'
como_u bash "$RAIZ/bootstrap/bootstrap.sh" --remover --nome umaq --painel "http://127.0.0.1:$PORTA" >/dev/null 2>&1
checa "--remover desfaz: supervisor, ttyd, arquivos e bloco do bashrc" 'esperar "! kill -0 \$(cat $UH/.local/state/tty-orquestrador/supervisor.pid 2>/dev/null) 2>/dev/null && ! curl -fs -o /dev/null -m 1 http://127.0.0.1:$PU/" 20 && [ ! -e "$UH/.config/tty-orquestrador" ] && [ ! -e "$UH/.local/lib/tty-orquestrador" ] && ! grep -q "tty-orquestrador" "$UH/.bashrc" && grep -qx "umask 007" "$UH/.bashrc" && bash -n "$UH/.bashrc"'
checa "--remover tira a máquina do painel" '! estado | jq -e ".maquinas[] | select(.nome==\"umaq\")" >/dev/null'
checa "sem root e sem tailscale: erro claro" 'out=$(como_u env -u TTY_IP TTY_TS_IFACE=naoexiste0 TTY_TS_CLI=/nao/existe bash "$RAIZ/bootstrap/bootstrap.sh" --nome semts --painel "http://127.0.0.1:$PORTA" --pacote "$RAIZ" 2>&1); [ $? -ne 0 ] && grep -qi "tailnet" <<<"$out"; r=$?; como_u bash "$RAIZ/bootstrap/bootstrap.sh" --remover --nome semts >/dev/null 2>&1; [ $r -eq 0 ]'

echo "facilitadores"
export HOME=$T/home1; mkdir -p "$HOME/.claude/skills/x" "$HOME/.config/opencode/skills" "$T/bin"
echo '{"model":"opus","hooks":{"Stop":[{"hooks":[{"type":"command","command":"outro"}]}]}}' > "$HOME/.claude/settings.json"
echo "# regras" > "$HOME/.claude/CLAUDE.md"; echo s > "$HOME/.claude/skills/x/SKILL.md"
echo SEGREDO > "$HOME/.claude/.credentials.json"
echo '{}' > "$HOME/.config/opencode/opencode.jsonc"
printf '#!/bin/sh\n' > "$T/bin/claude"; cp "$T/bin/claude" "$T/bin/opencode"; chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH"
tty-add claude --sem-config >/dev/null; tty-add claude --sem-config >/dev/null
checa "hooks do claude: um Notification e um Stop de tty-notify (idempotente)" \
  'jq -e "([.hooks.Notification[]|select(.hooks[0].command|startswith(\"tty-notify\"))]|length)==1 and ([.hooks.Stop[]|select(.hooks[0].command|startswith(\"tty-notify\"))]|length)==1" "$HOME/.claude/settings.json" >/dev/null'
checa "hook preexistente preservado" 'jq -e "any(.hooks.Stop[]; .hooks[0].command==\"outro\")" "$HOME/.claude/settings.json" >/dev/null'
tty-push claude >/dev/null && tty-push opencode >/dev/null
checa "push não leva credenciais" '! tar -tzf "$T/dados/configs/claude.tar.gz" | grep -q credentials'
tty-add opencode --sem-config >/dev/null
checa "plugin do opencode criado" '[ -s "$HOME/.config/opencode/plugins/tty-notify.js" ]'
checa "opencode sem versão legível recebe o plugin v1" 'grep -q "export const TtyNotify" "$HOME/.config/opencode/plugins/tty-notify.js"'
printf '#!/bin/sh\necho "v2.0.22"\n' > "$T/bin/opencode"
tty-add opencode --sem-config >/dev/null
checa "opencode 2.x recebe o plugin v2" 'grep -q "session.execution.succeeded" "$HOME/.config/opencode/plugins/tty-notify.js" && ! grep -q "export const TtyNotify" "$HOME/.config/opencode/plugins/tty-notify.js"'
printf '#!/bin/sh\necho "1.4.3"\n' > "$T/bin/opencode"
tty-add opencode --sem-config >/dev/null
checa "opencode 1.x volta ao plugin v1" 'grep -q "export const TtyNotify" "$HOME/.config/opencode/plugins/tty-notify.js"'
export HOME=$T/home2; mkdir -p "$HOME"
tty-add claude >/dev/null
checa "tty-add traz config de outro host" '[ "$(cat "$HOME/.claude/CLAUDE.md")" = "# regras" ] && [ -f "$HOME/.claude/skills/x/SKILL.md" ]'
checa "tty-add não traz credenciais" '[ ! -e "$HOME/.claude/.credentials.json" ]'
export HOME=$T/home3; mkdir -p "$HOME"
tty-add claude --so CLAUDE.md >/dev/null
checa "--so traz só o item pedido" '[ -f "$HOME/.claude/CLAUDE.md" ] && [ ! -e "$HOME/.claude/skills" ]'
checa "ferramenta desconhecida falha com código 2" 'tty-add naoexiste >/dev/null 2>&1; [ $? -eq 2 ]'
export HOME=$T/home4; mkdir -p "$HOME"
checa "tty-add sem config no painel avisa SEM CONFIG (não finge que aplicou) e não falha" \
  'printf "BIN=sh\nITENS=(.x)\ninstalar(){ :; }\npos(){ :; }\n" > "$RAIZ/facilitadores/ferramentas/zz.sh"; out=$(tty-add zz 2>&1); r=$?; rm -f "$RAIZ/facilitadores/ferramentas/zz.sh"; [ $r -eq 0 ] && grep -q "SEM CONFIG" <<<"$out" && grep -q "tty-push zz" <<<"$out" && ! grep -q "aplicada em" <<<"$out"'
checa "ferramenta nova só precisa de um arquivo" \
  'printf "BIN=sh\nITENS=(.x)\ninstalar(){ :; }\npos(){ echo POS; }\n" > "$RAIZ/facilitadores/ferramentas/zz.sh"; tty-add zz --sem-config | grep -q POS; r=$?; rm -f "$RAIZ/facilitadores/ferramentas/zz.sh"; [ $r -eq 0 ]'

echo "bootstrap"
checa "recusa rodar sem root" 'out=$(bash "$RAIZ/bootstrap/bootstrap.sh" --nome x 2>&1); [ $? -ne 0 ] && echo "$out" | grep -q root'
checa "--help funciona" 'bash "$RAIZ/bootstrap/bootstrap.sh" --help | grep -q -- --nome'

echo "tty-notify por pasta (sem tmux)"
mkdir -p "$T/etcp/projetos" "$T/pj/api/sub" "$T/pj/api2"
printf 'DIR=%s\nUSUARIO=x\nPORTA=1\n' "$T/pj/api" > "$T/etcp/projetos/api.conf"
printf 'DIR=%q\nUSUARIO=x\nPORTA=2\n' "$T/pj/api2" > "$T/etcp/projetos/api2.conf"
printf 'PAINEL_URL=http://127.0.0.1:%s\nMAQUINA=teste\n' "$PORTA" > "$T/etcp/conf"
(cd "$T/pj/api/sub" && env -u TMUX -u TMUX_PANE -u TTY_SESSAO TTY_ETC=$T/etcp TTY_CONF=$T/etcp/conf tty-notify terminou "pelo cwd")
checa "pasta dentro do projeto vira o projeto (sem terminal)" 'eventos | jq -e "any(.[]; .texto==\"pelo cwd\" and .projeto==\"api\" and .terminal==\"\")" >/dev/null'
(cd /tmp && env -u TMUX -u TMUX_PANE -u TTY_SESSAO TTY_DIR=$T/pj/api2 TTY_ETC=$T/etcp TTY_CONF=$T/etcp/conf tty-notify terminou "por TTY_DIR")
checa "TTY_DIR tem prioridade sobre a pasta atual (e não confunde api com api2)" 'eventos | jq -e "any(.[]; .texto==\"por TTY_DIR\" and .projeto==\"api2\")" >/dev/null'
(cd /tmp && env -u TMUX -u TMUX_PANE -u TTY_SESSAO TTY_ETC=$T/etcp TTY_CONF=$T/etcp/conf tty-notify terminou "fora de projeto")
checa "pasta fora de qualquer projeto fica sem projeto" 'eventos | jq -e "any(.[]; (.texto|startswith(\"fora de projeto [sem projeto: pasta /tmp\")) and .projeto==\"\")" >/dev/null'

echo "tty-atualizar"
INST=$T/inst; mkdir -p "$INST/lib/tty-orquestrador" "$INST/bin" "$INST/cfg/tty-orquestrador"
curl -fs "http://127.0.0.1:$PORTA/pacote.tar.gz" | tar -xz -C "$INST/lib/tty-orquestrador"
printf '\n# versao antiga\n' >> "$INST/lib/tty-orquestrador/notificador/tty-notify"
cp "$RAIZ/projetos/tty-atualizar" "$INST/lib/tty-orquestrador/projetos/tty-atualizar"; chmod +x "$INST/lib/tty-orquestrador/projetos/tty-atualizar"
ln -sf "$INST/lib/tty-orquestrador/projetos/tty-atualizar" "$INST/bin/tty-atualizar"
printf 'PAINEL_URL=http://127.0.0.1:%s\nMAQUINA=teste\n' "$PORTA" > "$INST/cfg/tty-orquestrador/conf"
out=$(XDG_CONFIG_HOME=$INST/cfg env -u TTY_CONF "$INST/bin/tty-atualizar" 2>&1); r=$?
checa "tty-atualizar termina bem e diz o que mudou" '[ $r -eq 0 ] && grep -q "atualizado: .*tty-notify" <<<"$out"'
checa "o script foi trocado pelo do painel" 'cmp -s "$INST/lib/tty-orquestrador/notificador/tty-notify" "$RAIZ/notificador/tty-notify"'
checa "cria o link do tty-notify em bin" '[ "$(readlink "$INST/bin/tty-notify")" = "$INST/lib/tty-orquestrador/notificador/tty-notify" ]'
checa "sem sobras (.novo/.velho)" '[ ! -e "$INST/lib/tty-orquestrador.novo" ] && [ ! -e "$INST/lib/tty-orquestrador.velho" ]'
out=$(XDG_CONFIG_HOME=$INST/cfg env -u TTY_CONF "$INST/bin/tty-atualizar" 2>&1)
checa "rodar de novo não muda nada" 'grep -q "já estava" <<<"$out"'
checa "painel fora do ar não destrói a instalação" 'printf "PAINEL_URL=http://127.0.0.1:1\n" > "$INST/cfg/tty-orquestrador/conf"; XDG_CONFIG_HOME=$INST/cfg env -u TTY_CONF "$INST/bin/tty-atualizar" >/dev/null 2>&1; [ $? -ne 0 ] && [ -x "$INST/lib/tty-orquestrador/notificador/tty-notify" ]'

echo
if [ "$falhas" -eq 0 ]; then echo "tudo ok"; else echo "$falhas falha(s)"; exit 1; fi
