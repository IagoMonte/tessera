#!/usr/bin/env python3
"""tty-agente: roda em cada máquina (como root, serviço systemd).

A cada ciclo manda ao painel um heartbeat com as métricas da máquina e de cada projeto/terminal
(CPU, memória, branch, comando atual) e recebe de volta tarefas. A máquina PUXA as tarefas:
nada entra nela. As tarefas são uma lista fechada, validadas aqui (nunca via shell, sem comando
arbitrário) e executadas como o usuário do projeto.

Só biblioteca padrão. Testes: test_agente.py.
Variáveis: TTY_CONF (padrão /etc/tty-orquestrador.conf, ou ~/.config/tty-orquestrador/conf no modo sem root), TTY_ETC, TTY_PROC, TTY_INTERVALO (3 s).
"""
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# modo sem root: a configuração fica no HOME do usuário (instalada pelo bootstrap sem sudo)
UETC = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tty-orquestrador"
if os.environ.get("TTY_ETC"):
    ETC = Path(os.environ["TTY_ETC"])
elif (UETC / "conf").is_file():
    ETC = UETC
else:
    ETC = Path("/etc/tty-orquestrador")
PROC = Path(os.environ.get("TTY_PROC", "/proc"))
CONF = os.environ.get("TTY_CONF") or (str(UETC / "conf") if ETC == UETC else "/etc/tty-orquestrador.conf")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGINA_KB = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4

RE_NOME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
RE_MAQUINA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RE_TERMINAL = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RE_FERRAMENTA = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RE_USUARIO = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SHELLS = {"bash", "zsh", "fish", "sh", "dash"}
_FERR_REL = "tty-orquestrador/facilitadores/ferramentas"
FERRAMENTAS_DIR = Path(os.environ.get("TTY_FERRAMENTAS_DIR") or (
    Path.home() / ".local/lib" / _FERR_REL if (Path.home() / ".local/lib" / _FERR_REL).is_dir() else Path("/usr/local/lib") / _FERR_REL))


class TarefaInvalida(ValueError):
    pass


# ---------- leitura de configuração ----------

def ler_chave_valor(caminho):
    dados = {}
    try:
        linhas = Path(caminho).read_text().splitlines()
    except OSError:
        return dados
    for linha in linhas:
        if "=" in linha and not linha.lstrip().startswith("#"):
            k, v = linha.split("=", 1)
            try:
                partes = shlex.split(v)
            except ValueError:
                partes = [v]
            dados[k.strip()] = partes[0] if partes else ""
    return dados


def projetos_locais(etc=None):
    """{nome: {dir, usuario, porta}} lido de /etc/tty-orquestrador/projetos/*.conf."""
    etc = Path(etc or ETC)
    saida = {}
    for conf in sorted((etc / "projetos").glob("*.conf")):
        d = ler_chave_valor(conf)
        if RE_NOME.match(conf.stem) and d.get("DIR") and d.get("PORTA", "").isdigit():
            saida[conf.stem] = {"dir": d["DIR"], "usuario": d.get("USUARIO", ""), "porta": int(d["PORTA"])}
    return saida


# ---------- métricas (/proc) ----------

def snapshot_proc(proc=None):
    """{pid: (ppid, ticks_cpu, rss_kb)} de todos os processos."""
    proc = Path(proc or PROC)
    snap = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            txt = (d / "stat").read_text()
        except OSError:
            continue
        # o campo comm pode ter espaços e parênteses: tudo depois do último ")" é confiável
        resto = txt.rpartition(")")[2].split()
        try:
            ppid, utime, stime, rss = int(resto[1]), int(resto[11]), int(resto[12]), int(resto[21])
        except (IndexError, ValueError):
            continue
        snap[int(d.name)] = (ppid, utime + stime, rss * PAGINA_KB)
    return snap


def arvore(snap, raiz):
    filhos = {}
    for pid, (ppid, _, _) in snap.items():
        filhos.setdefault(ppid, []).append(pid)
    achados, pilha = set(), [raiz]
    while pilha:
        p = pilha.pop()
        if p in achados or p not in snap:
            continue
        achados.add(p)
        pilha.extend(filhos.get(p, []))
    return achados


def ler_proc_stat(proc=None):
    """(ocupado, total) em ticks, da primeira linha de /proc/stat."""
    linha = (Path(proc or PROC) / "stat").read_text().splitlines()[0].split()[1:]
    v = [int(x) for x in linha]
    ocioso = v[3] + (v[4] if len(v) > 4 else 0)
    total = sum(v[:8])
    return total - ocioso, total


def ler_meminfo(proc=None):
    """(total_mb, usado_mb)."""
    info = {}
    for linha in (Path(proc or PROC) / "meminfo").read_text().splitlines():
        k, _, v = linha.partition(":")
        info[k] = int(v.split()[0])
    total = info["MemTotal"]
    return total // 1024, (total - info.get("MemAvailable", info.get("MemFree", 0))) // 1024


class Medidor:
    """Guarda a leitura anterior para calcular CPU% por diferença."""

    def __init__(self, proc=None):
        self.proc = proc
        self.snap = {}
        self.t = None
        self.maq = None

    def amostrar(self, agora=None):
        agora = time.monotonic() if agora is None else agora
        novo = snapshot_proc(self.proc)
        maq = ler_proc_stat(self.proc)
        anterior, t0, maq0 = self.snap, self.t, self.maq
        self.snap, self.t, self.maq = novo, agora, maq
        self._dt = (agora - t0) if t0 is not None else None
        self._anterior = anterior
        cpu_maq = 0.0
        if maq0 and maq[1] > maq0[1]:
            cpu_maq = 100.0 * (maq[0] - maq0[0]) / (maq[1] - maq0[1])
        return round(max(0.0, min(100.0, cpu_maq)), 1)

    def uso(self, raiz):
        """(cpu%, mem_mb) da árvore de processos de `raiz` (100% = um núcleo inteiro)."""
        pids = arvore(self.snap, raiz)
        mem = sum(self.snap[p][2] for p in pids) / 1024
        cpu = 0.0
        if self._dt and self._dt > 0:
            ticks = sum(max(0, self.snap[p][1] - self._anterior[p][1]) for p in pids if p in self._anterior)
            cpu = 100.0 * ticks / CLK_TCK / self._dt
        return round(cpu, 1), round(mem)


# ---------- execução como o usuário do projeto ----------

def usuario_atual():
    return pwd.getpwuid(os.geteuid()).pw_name


def como(usuario, argv):
    """argv para rodar `argv` como `usuario` (runuser se somos root)."""
    if usuario == usuario_atual():
        return list(argv)
    if os.geteuid() != 0:
        raise TarefaInvalida(f"sem permissão para agir como {usuario}")
    home = pwd.getpwnam(usuario).pw_dir
    return ["runuser", "-u", usuario, "--", "env", f"HOME={home}", f"USER={usuario}", f"LOGNAME={usuario}"] + list(argv)


def rodar(argv, timeout=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"{type(e).__name__}: {e}"


def sessoes_tmux(usuario):
    """[(sessão, pane_pid, comando, atividade_epoch)] dos tmux do usuário."""
    try:
        argv = como(usuario, ["tmux", "list-panes", "-a", "-F",
                              "#{session_name}|#{pane_pid}|#{pane_current_command}|#{session_activity}"])
    except TarefaInvalida:
        return []
    rc, saida = rodar(argv, 5)
    if rc != 0:
        return []
    achados = []
    for linha in saida.splitlines():
        partes = linha.split("|")
        if len(partes) == 4 and partes[1].isdigit() and partes[3].isdigit():
            achados.append((partes[0], int(partes[1]), partes[2], int(partes[3])))
    return achados


def ferramentas_disponiveis(diretorio=None):
    """{nome: binário} lido (sem executar) de facilitadores/ferramentas/<nome>.sh (linha BIN=...)."""
    achadas = {}
    for arq in sorted(Path(diretorio or FERRAMENTAS_DIR).glob("*.sh")):
        if arq.stem.startswith("_") or not RE_FERRAMENTA.match(arq.stem):
            continue
        m = re.search(r"^BIN=(\S+)", arq.read_text(), re.M)
        if m and re.match(r"^[A-Za-z0-9._-]+$", m.group(1)):
            achadas[arq.stem] = m.group(1)
    return achadas


def ferramentas_instaladas(usuario, disponiveis):
    """{nome: bool}: o binário existe no PATH do usuário (inclui ~/.local/bin e ~/.opencode/bin)?"""
    if not disponiveis:
        return {}
    script = ('export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"; '
              'for b in "$@"; do command -v "$b" >/dev/null 2>&1 && echo "$b"; done; exit 0')
    try:
        rc, saida = rodar(como(usuario, ["bash", "-c", script, "x", *disponiveis.values()]), 5)
    except TarefaInvalida:
        return {}
    presentes = set(saida.split()) if rc == 0 else set()
    return {nome: b in presentes for nome, b in disponiveis.items()}


def branch_git(diretorio, usuario):
    try:
        rc, saida = rodar(como(usuario, ["git", "-C", diretorio, "rev-parse", "--abbrev-ref", "HEAD"]), 3)
    except TarefaInvalida:
        return ""
    return saida if rc == 0 and saida and "\n" not in saida else ""


def coletar(med, projetos=None, sessoes_fn=sessoes_tmux, branch_fn=branch_git, agora=None):
    """Monta o payload do heartbeat. `med.amostrar()` deve ter sido chamado antes."""
    projetos = projetos_locais() if projetos is None else projetos
    agora = int(time.time()) if agora is None else agora
    ram_mb, mem_mb = ler_meminfo(med.proc)
    saida = {}
    por_usuario = {u: sessoes_fn(u) for u in {p["usuario"] for p in projetos.values()}}
    for nome, p in projetos.items():
        terminais = {}
        total_cpu = total_mem = 0.0
        for sess, pid, cmd, ativ in por_usuario.get(p["usuario"], []):
            proj, _, term = sess.partition("/")
            if proj != nome or not RE_TERMINAL.match(term):
                continue
            cpu, mem = med.uso(pid)
            t = terminais.setdefault(term, {"cmd": cmd, "cpu": 0.0, "mem_mb": 0, "atividade_s": max(0, agora - ativ)})
            t["cpu"] = round(t["cpu"] + cpu, 1)
            t["mem_mb"] += mem
            if cmd not in SHELLS or t["cmd"] in SHELLS:
                t["cmd"] = cmd
            total_cpu += cpu
            total_mem += mem
        saida[nome] = {"dir": p["dir"], "usuario": p["usuario"], "porta": p["porta"],
                       "branch": branch_fn(p["dir"], p["usuario"]),
                       "cpu": round(total_cpu, 1), "mem_mb": round(total_mem), "terminais": terminais}
    return {"cores": os.cpu_count() or 1, "ram_mb": ram_mb, "mem_mb": mem_mb,
            "shell": os.path.basename(os.environ.get("SHELL", "bash")), "projetos": saida}


# ---------- listagem de pastas (autocompletar do "Novo projeto") ----------

MAX_PASTAS = 300
MAX_DADOS_BYTES = 24000


def listar_subpastas(caminho, ocultas=False, maximo=MAX_PASTAS):
    """Subpastas de `caminho` (absoluto ou ~/...), como o usuário que roda isto. Só pastas; sem as ocultas por padrão.
    Devolve {"ok": True, "dir", "home", "pastas", "total", "truncado"} ou {"ok": False, "erro"}."""
    home = str(Path.home())
    if not isinstance(caminho, str) or not caminho or "\0" in caminho or len(caminho) > 4096:
        return {"ok": False, "erro": "caminho inválido"}
    exp = home + caminho[1:] if caminho == "~" or caminho.startswith("~/") else caminho
    if not exp.startswith("/"):
        return {"ok": False, "erro": "use um caminho absoluto (ou ~/...)"}
    p = os.path.normpath(exp)
    try:
        with os.scandir(p) as it:
            nomes = sorted(e.name for e in it if (ocultas or not e.name.startswith(".")) and _eh_pasta(e))
    except FileNotFoundError:
        return {"ok": False, "erro": "a pasta não existe", "dir": p, "home": home}
    except NotADirectoryError:
        return {"ok": False, "erro": "isso não é uma pasta", "dir": p, "home": home}
    except PermissionError:
        return {"ok": False, "erro": "sem permissão para ler essa pasta", "dir": p, "home": home}
    except OSError as e:
        return {"ok": False, "erro": f"não consegui ler: {e.strerror or type(e).__name__}", "dir": p, "home": home}
    total = len(nomes)
    saida = {"ok": True, "dir": p, "home": home, "pastas": nomes[:maximo], "total": total, "truncado": total > maximo}
    while len(json.dumps(saida, ensure_ascii=False)) > MAX_DADOS_BYTES and saida["pastas"]:  # nomes enormes: corta o fim
        saida["pastas"] = saida["pastas"][: len(saida["pastas"]) // 2]
        saida["truncado"] = True
    return saida


def _eh_pasta(entrada):
    try:
        return entrada.is_dir()  # segue links simbólicos para pastas
    except OSError:
        return False


# ---------- tarefas: lista fechada ----------

def _campo(args, nome, regex):
    v = args.get(nome)
    if not isinstance(v, str) or not regex.match(v):
        raise TarefaInvalida(f"campo '{nome}' inválido")
    return v


def usuarios_permitidos(projetos):
    """Quem este agente pode representar: donos de projetos e, num agente SEM root, o próprio usuário dele
    (um agente sem projetos — porque o último foi removido — continua servindo Facilitadores e autocompletar)."""
    permitidos = {p["usuario"] for p in projetos.values()}
    if os.geteuid() != 0:
        permitidos.add(usuario_atual())
    return permitidos


def _usuario_do_projeto(args, projetos):
    u = _campo(args, "usuario", RE_USUARIO)
    if u not in usuarios_permitidos(projetos):
        raise TarefaInvalida("usuário não é dono de nenhum projeto desta máquina (nem é o usuário deste agente)")
    return u


def montar_comando(tarefa, projetos=None, sessoes=()):
    """Valida a tarefa e devolve a lista argv (nunca uma string de shell). Levanta TarefaInvalida."""
    projetos = projetos_locais() if projetos is None else projetos
    tipo = tarefa.get("tipo")
    args = tarefa.get("args") or {}
    if not isinstance(args, dict):
        raise TarefaInvalida("args inválidos")
    if tipo == "projeto.add":
        nome = _campo(args, "nome", RE_NOME)
        diretorio = args.get("dir")
        if not isinstance(diretorio, str) or not diretorio.startswith("/") or "\n" in diretorio or "\0" in diretorio:
            raise TarefaInvalida("dir deve ser um caminho absoluto")
        argv = ["tty-projeto", "add", nome, "--dir", diretorio]
        if args.get("usuario"):
            argv += ["--usuario", _usuario_do_projeto(args, projetos)]
        if args.get("porta") is not None:
            porta = args["porta"]
            if not isinstance(porta, int) or isinstance(porta, bool) or not 1024 <= porta <= 65535:
                raise TarefaInvalida("porta inválida")
            argv += ["--porta", str(porta)]
        return argv, None
    if tipo == "projeto.rm":
        return ["tty-projeto", "rm", _campo(args, "nome", RE_NOME)], None
    if tipo in ("terminal.novo", "terminal.encerrar", "terminal.limpar"):
        nome = _campo(args, "projeto", RE_NOME)
        if nome not in projetos:
            raise TarefaInvalida("projeto desconhecido nesta máquina")
        p = projetos[nome]
        if tipo == "terminal.encerrar":
            term = _campo(args, "terminal", RE_TERMINAL)
            return ["tmux", "kill-session", "-t", f"={nome}/{term}"], p["usuario"]
        if tipo == "terminal.limpar":
            # marcador interno: executar() mata uma a uma (tmux não tem curinga), main inclusive;
            # o tty-sessao (tmux new-session -A) recria o main limpo no próximo attach
            return ["tmux", "limpar", nome], p["usuario"]
        if args.get("terminal"):
            term = _campo(args, "terminal", RE_TERMINAL)
        else:  # nome automático: t2, t3, ...
            usados = {s.partition("/")[2] for s in sessoes}
            n = 2
            while f"t{n}" in usados:
                n += 1
            term = f"t{n}"
        return ["tmux", "new-session", "-d", "-s", f"{nome}/{term}", "-c", p["dir"]], p["usuario"]
    if tipo in ("ferramenta.add", "ferramenta.push"):
        usuario = _usuario_do_projeto(args, projetos)
        ferr = _campo(args, "ferramenta", RE_FERRAMENTA)
        if tipo == "ferramenta.push":
            return ["tty-push", ferr], usuario
        argv = ["tty-add", ferr]
        if args.get("sem_config") is True:
            argv.append("--sem-config")
        return argv, usuario
    if tipo == "sistema.atualizar":
        return ["tty-atualizar"], None
    if tipo == "pasta.listar":
        usuario = _usuario_do_projeto(args, projetos)
        caminho = args.get("dir")
        if not isinstance(caminho, str) or not caminho or len(caminho) > 4096 or "\0" in caminho:
            raise TarefaInvalida("campo 'dir' inválido")
        argv = [sys.executable, str(Path(__file__).resolve()), "--listar", caminho]
        if args.get("ocultas") is True:
            argv.append("--ocultas")
        return argv, usuario
    raise TarefaInvalida(f"tipo de tarefa desconhecido: {tipo}")


def executar(tarefa, projetos=None, sessoes=()):
    """Roda a tarefa. Devolve {"id", "ok", "saida"}; nunca levanta."""
    try:
        argv, usuario = montar_comando(tarefa, projetos, sessoes)
        if usuario:
            argv = como(usuario, argv)
        if tarefa.get("tipo") == "pasta.listar":
            return _executar_listagem(tarefa.get("id"), argv)
        if tarefa.get("tipo") == "terminal.limpar":
            return _executar_limpar(tarefa.get("id"), tarefa.get("args") or {}, projetos, sessoes, usuario)
        rc, saida = rodar(argv, 900)
        return {"id": tarefa.get("id"), "ok": rc == 0, "saida": saida[-500:]}
    except TarefaInvalida as e:
        return {"id": tarefa.get("id"), "ok": False, "saida": f"recusada: {e}"}


def _executar_limpar(tid, args, projetos, sessoes, usuario):
    """Mata todas as sessões tmux de um projeto (main inclusive; ele recria no próximo attach)."""
    nome = args.get("projeto", "")
    alvos = sorted({s for s in (sessoes or []) if s == nome or s.startswith(nome + "/")})
    if not alvos:
        return {"id": tid, "ok": True, "saida": "nada a limpar"}
    falhas = []
    for sess in alvos:
        rc, saida = rodar(como(usuario, ["tmux", "kill-session", "-t", f"={sess}"]), 30)
        if rc != 0 and "can't find session" not in saida and "no server running" not in saida:
            falhas.append(f"{sess}: {saida[:120]}")
    if falhas:
        return {"id": tid, "ok": False, "saida": "; ".join(falhas)[:500]}
    n = len(alvos)
    return {"id": tid, "ok": True, "saida": f"{n} terminal{'ais' if n != 1 else ''} encerrado{'' if n == 1 else 's'}; main recria ao reabrir"}


def _executar_listagem(tid, argv):
    """pasta.listar devolve JSON em `dados` (o texto curto `saida` fica só para o erro)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        dados = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"id": tid, "ok": False, "saida": f"não consegui listar: {type(e).__name__}"}
    return {"id": tid, "ok": bool(dados.get("ok")), "saida": str(dados.get("erro", ""))[:500], "dados": dados}


# ---------- laço principal ----------

def token_painel():
    """Token do agente (Bearer). Env TTY_TOKEN ou PAINEL_TOKEN= no CONF. Vazio = sem auth (localhost)."""
    env = (os.environ.get("TTY_TOKEN") or "").strip()
    if env:
        return env
    try:
        return str(ler_chave_valor(CONF).get("PAINEL_TOKEN", "")).strip()
    except OSError:
        return ""


class Agente:
    def __init__(self, painel, maquina, intervalo=3.0):
        self.painel, self.maquina, self.intervalo = painel.rstrip("/"), maquina, intervalo
        self.med = Medidor()
        self.resultados = []
        self.lock = threading.Lock()
        self.erro_avisado = False
        self.cache_ferr = (0.0, {})
        self.id = os.environ.get("TTY_AGENTE_ID") or usuario_atual()  # vários usuários podem somar numa máquina
        self.intervalo_tarefas = float(os.environ.get("TTY_INTERVALO_TAREFAS", 1.0))

    def ferramentas(self):
        """Cache de 60 s: checar binários a cada heartbeat seria caro."""
        agora = time.monotonic()
        if agora - self.cache_ferr[0] > 60:
            usuario = next(iter(sorted(usuarios_permitidos(projetos_locais()))), None)  # sem projetos: o próprio usuário (sem root)
            self.cache_ferr = (agora, ferramentas_instaladas(usuario, ferramentas_disponiveis()) if usuario else {})
        return self.cache_ferr[1]

    def _tarefa_em_thread(self, tarefa):
        projetos = projetos_locais()
        sessoes = [s for u in {p["usuario"] for p in projetos.values()} for (s, *_r) in sessoes_tmux(u)]
        res = executar(tarefa, projetos, sessoes)
        with self.lock:
            self.resultados.append(res)

    def _enviar(self, caminho, corpo, resultados):
        """POST ao painel; em falha devolve os resultados à fila (nada se perde). Devolve a resposta ou None."""
        cab = {"Content-Type": "application/json"}
        tok = token_painel()
        if tok:
            cab["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(f"{self.painel}{caminho}", data=json.dumps(corpo).encode(),
                                     headers=cab, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read() or b"{}")
            self.erro_avisado = False
            return resp
        except (OSError, ValueError) as e:
            with self.lock:
                self.resultados = resultados + self.resultados
            if not self.erro_avisado:
                print(f"painel inacessível: {type(e).__name__}", file=sys.stderr, flush=True)
                self.erro_avisado = True
            return None

    def _despachar(self, resp):
        for tarefa in (resp or {}).get("tarefas", []):
            threading.Thread(target=self._tarefa_em_thread, args=(tarefa,), daemon=True).start()

    def ciclo(self):
        cpu = self.med.amostrar()
        payload = coletar(self.med)
        payload["cpu"] = cpu
        payload["agente"] = self.id
        payload["ferramentas"] = self.ferramentas()
        with self.lock:
            payload["resultados"], self.resultados = self.resultados, []
        self._despachar(self._enviar(f"/api/agente/{self.maquina}", payload, payload["resultados"]))

    def consultar_tarefas(self):
        """Consulta leve (1 s): só entrega resultados e recebe tarefas; o heartbeat pesado segue a cada 3 s.
        É o que deixa ações interativas (autocompletar pastas) com latência de ~0,5 s."""
        with self.lock:
            resultados, self.resultados = self.resultados, []
        self._despachar(self._enviar(f"/api/agente/{self.maquina}/tarefas", {"agente": self.id, "resultados": resultados}, resultados))

    def _laco_tarefas(self):
        while True:
            try:
                self.consultar_tarefas()
            except Exception as e:
                print(f"consulta de tarefas falhou: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(self.intervalo_tarefas)

    def rodar(self):
        threading.Thread(target=self._laco_tarefas, daemon=True).start()
        while True:
            t0 = time.monotonic()
            try:
                self.ciclo()
            except Exception as e:  # o agente não pode morrer por um ciclo ruim
                print(f"ciclo falhou: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0.2, self.intervalo - (time.monotonic() - t0)))


def main():
    if sys.argv[1:2] == ["--listar"]:  # modo auxiliar: roda como o usuário alvo e imprime JSON (ver pasta.listar)
        caminho = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(listar_subpastas(caminho, ocultas="--ocultas" in sys.argv[3:]), ensure_ascii=False))
        return
    conf = ler_chave_valor(CONF)
    painel, maquina = conf.get("PAINEL_URL", ""), conf.get("MAQUINA", "")
    if not painel or not RE_MAQUINA.match(maquina or ""):
        sys.exit(f"{CONF} precisa de PAINEL_URL e MAQUINA")
    print(f"agente de {maquina} -> {painel}", flush=True)
    Agente(painel, maquina, float(os.environ.get("TTY_INTERVALO", 3))).rodar()


if __name__ == "__main__":
    main()
