#!/usr/bin/env python3
"""Painel único do tty-orquestrador.

Interface Tessera (static/), estado das máquinas/projetos/terminais (heartbeat dos agentes),
fila de tarefas para os agentes, eventos (com envio ao Telegram), convites (auth key da Tailscale)
e distribuição do bootstrap, do pacote de scripts e das configurações das ferramentas.

Só biblioteca padrão. Roda no PC durante o desenvolvimento e depois no Oracle.

Configuração (variáveis de ambiente ou arquivo .env ao lado deste script):
  PAINEL_BIND    endereço de escuta (padrão: 0.0.0.0 — Tailscale + IP público)
  PAINEL_PORTA   porta (padrão 8787)
  PAINEL_SENHA   senha de humanos (navegador). Localhost entra sem senha; tailnet e internet exigem.
  PAINEL_TOKEN   token dos agentes/máquinas (Bearer). Sem ele, registro/heartbeat na tailnet é 401.
  PAINEL_TLS_CERT/PAINEL_TLS_KEY  ativa HTTPS (HSTS + cookie Secure)
  PAINEL_PUBLICO IP ou host público usado no download do instalador do convite (opcional; padrão: a página atual)
  PAINEL_DADOS   pasta de dados: maquinas.json, eventos.log, configs/ (padrão: dados/ ao lado)
  TS_API_KEY     credencial da API da Tailscale (gera as auth keys dos convites)
  TS_TAG         tag opcional para as máquinas convidadas (ex.: tag:tty)
  TG_TOKEN       token do bot do Telegram
  TG_CHAT        chat id que recebe os avisos
"""
import base64
import hmac
import io
import ipaddress
import json
import os
import re
import select
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
LOCK = threading.Lock()
NOME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROJ_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TERM_OK = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AGENTE_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
TIPOS_TAREFA = {"projeto.add", "projeto.rm", "terminal.novo", "terminal.encerrar", "terminal.limpar", "ferramenta.add", "ferramenta.push", "pasta.listar", "sistema.atualizar"}
MAX_DADOS_TAREFA = 30000   # bytes de JSON aceitos no campo "dados" do resultado de uma tarefa
NOTIF_PADRAO = {"terminou": True, "aprovacao": True, "pergunta": True, "erro": True, "ocioso": False, "online": False}
ATENCAO = {"aprovacao", "pergunta", "ocioso"}  # motivos que deixam o terminal "aguardando você"
ONLINE_S = 10          # sem heartbeat há mais que isso = máquina offline
AGENTE_VALE_S = 600    # agente sem heartbeat há mais que isso sai da memória (a estrutura continua registrada)
RODANDO_S = 10         # saída nos últimos N segundos = terminal rodando
TAREFA_VALE_S = 600    # tarefa não entregue após isso expira
SHELLS = {"bash", "zsh", "fish", "sh", "dash"}
ESTATICO = AQUI / "static"
TIPOS_MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
              ".woff2": "font/woff2", ".json": "application/json", ".map": "application/json"}
MOTIVOS = {
    "terminou": "✅",
    "aprovacao": "✋",
    "pergunta": "❓",
    "erro": "❌",
    "ocioso": "⏸",
    "online": "🟢",
}
LIMITE_CONFIG = 20 * 1024 * 1024
JANELA_DEDUP = 5
USUARIO_OK = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
FERRAMENTA_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Freio de brute-force na senha pública (só conta falha de auth, nunca o uso legítimo):
# após LIMITE_FALHAS 401 em JANELA_FALHAS_S, o IP leva 429 com Retry-After por BLOQUEIO_S.
LIMITE_FALHAS = 10
JANELA_FALHAS_S = 60
BLOQUEIO_S = 60
# CSP que permite exatamente o que a Tessera usa (sem mudar a UI):
# - connect ws:/wss: (terminal direto na tailnet), fontes do Google, hash do <script> de tema.
# - style 'unsafe-inline': o xterm.js (renderizador DOM) injeta um <style> com a paleta;
#   sem isso todo terminal vira preto-e-branco. Scripts seguem sem 'unsafe-inline'.
CSP_TEMA_HASH = "sha256-0mivrbvGuI+6CBwI4dtn8IsEKJ7B6nwKDAp01TbkzJk="
CSP_PADRAO = (f"default-src 'self'; script-src 'self' '{CSP_TEMA_HASH}'; "
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
              "img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'")
LIMITE_EVENTOS_MIN = 30  # por máquina
LIMITE_ARQUIVOS_MIN = 6  # por máquina
SESSAO_VALE_S = 12 * 3600
_FALHAS = {}  # {ip: [epoch das falhas]}


def _falhas_recentes(ip, agora):
    with LOCK:
        ts = [t for t in _FALHAS.get(ip, []) if agora - t < JANELA_FALHAS_S]
        _FALHAS[ip] = ts
        return ts


def _bloqueado(ip, agora=None):
    agora = time.time() if agora is None else agora
    return len(_falhas_recentes(ip, agora)) > LIMITE_FALHAS


def _registrar_falha(ip, agora=None):
    agora = time.time() if agora is None else agora
    with LOCK:
        ts = [t for t in _FALHAS.get(ip, []) if agora - t < JANELA_FALHAS_S]
        ts.append(agora)
        _FALHAS[ip] = ts
        return len(ts) > LIMITE_FALHAS


def _limpar_falhas(ip):
    with LOCK:
        _FALHAS.pop(ip, None)


def _is_localhost(ip):
    """Só loopback entra sem senha (dev/testes). Tailnet e internet exigem credencial."""
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_loopback
    except ValueError:
        return False


def _bearer(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return (handler.headers.get("X-Painel-Token") or "").strip()


def _sessao_ok(cfg, handler):
    cookie = handler.headers.get("Cookie", "")
    for parte in cookie.split(";"):
        k, _, v = parte.strip().partition("=")
        if k.strip() == "Tessera" and v.strip():
            with LOCK:
                exp = cfg.sessoes.get(v.strip())
                if exp and exp > time.time():
                    return True
                cfg.sessoes.pop(v.strip(), None)
    return False


def _nova_sessao(cfg):
    tok = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
    with LOCK:
        cfg.sessoes[tok] = time.time() + SESSAO_VALE_S
        if len(cfg.sessoes) > 5000:  # teto: expira as mais velhas
            for k in sorted(cfg.sessoes, key=cfg.sessoes.get)[:1000]:
                del cfg.sessoes[k]
    return tok


def _quota_ok(cfg, tabela, chave, limite):
    agora = time.time()
    with LOCK:
        ts = [x for x in tabela.get(chave, []) if agora - x < 60]
        if len(ts) >= limite:
            tabela[chave] = ts
            return False
        tabela[chave] = ts + [agora]
        return True


def _args_tarefa_validos(tipo, args):
    """Validação estrita no painel (o agente revalida). Legítimo passa, malformado leva 400."""
    if not isinstance(args, dict):
        return False
    if tipo in ("terminal.novo", "terminal.encerrar", "terminal.limpar"):
        if not isinstance(args.get("projeto"), str) or not PROJ_OK.match(args["projeto"]):
            return False
        if tipo == "terminal.encerrar":
            if not isinstance(args.get("terminal"), str) or not TERM_OK.match(args["terminal"]):
                return False
        if "terminal" in args and args.get("terminal") is not None:
            if not isinstance(args["terminal"], str) or not TERM_OK.match(args["terminal"]):
                return False
        return True
    if tipo == "projeto.add":
        if not isinstance(args.get("nome"), str) or not NOME_OK.match(args["nome"]):
            return False
        d = args.get("dir")
        if not isinstance(d, str) or not d.startswith("/") or "\n" in d or "\0" in d:
            return False
        if args.get("usuario") is not None and (not isinstance(args["usuario"], str) or not USUARIO_OK.match(args["usuario"])):
            return False
        if args.get("porta") is not None:
            po = args["porta"]
            if not isinstance(po, int) or isinstance(po, bool) or not 1024 <= po <= 65535:
                return False
        return True
    if tipo == "projeto.rm":
        return isinstance(args.get("nome"), str) and bool(NOME_OK.match(args["nome"]))
    if tipo in ("ferramenta.add", "ferramenta.push"):
        return (isinstance(args.get("usuario"), str) and bool(USUARIO_OK.match(args["usuario"]))
                and isinstance(args.get("ferramenta"), str) and bool(FERRAMENTA_OK.match(args["ferramenta"])))
    if tipo == "pasta.listar":
        return (isinstance(args.get("usuario"), str) and bool(USUARIO_OK.match(args["usuario"]))
                and isinstance(args.get("dir"), str) and 1 <= len(args["dir"]) <= 4096 and "\0" not in args["dir"])
    if tipo == "sistema.atualizar":
        return True
    return False


PLANO_AGENTE_PREFIXOS = ("/api/agente/",)
PLANO_AGENTE_EXATOS = {"/api/registrar", "/api/evento", "/api/arquivo", "/pacote.tar.gz"}


def _plano_agente(caminho):
    return caminho in PLANO_AGENTE_EXATOS or caminho.startswith(PLANO_AGENTE_PREFIXOS)


def carregar_env(caminho):
    """Lê KEY=VALOR de um arquivo .env, sem sobrescrever o ambiente."""
    try:
        linhas = Path(caminho).read_text().splitlines()
    except FileNotFoundError:
        return
    for linha in linhas:
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip("'\""))


class Registro:
    """Registro de máquinas persistido em JSON."""

    def __init__(self, caminho):
        self.caminho = Path(caminho)

    def _ler(self):
        try:
            return json.loads(self.caminho.read_text())
        except FileNotFoundError:
            return {}

    def _gravar(self, dados):
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, indent=2))
        tmp.replace(self.caminho)

    def listar(self):
        with LOCK:
            return self._ler()

    def registrar(self, nome, ip, porta, usuario=""):
        with LOCK:
            dados = self._ler()
            dados[nome] = {
                "ip": ip,
                "porta": int(porta),
                "usuario": usuario,
                "visto_em": int(time.time()),
            }
            self._gravar(dados)
            return dados[nome]

    def guardar_projetos(self, nome, projetos, cores, ram_mb):
        """Guarda só a estrutura (dir/usuário/porta/agente) para mostrar a máquina mesmo offline."""
        with LOCK:
            dados = self._ler()
            m = dados.get(nome)
            if m is None:
                return False
            estrutura = {k: {"dir": v["dir"], "usuario": v["usuario"], "porta": v["porta"], "agente": v.get("agente", "")}
                         for k, v in projetos.items()}
            if m.get("projetos") != estrutura or m.get("cores") != cores or m.get("ram_mb") != ram_mb:
                m.update(projetos=estrutura, cores=cores, ram_mb=ram_mb)
                self._gravar(dados)
            return True

    def tirar_projeto(self, nome, projeto):
        """Esquece UM projeto da estrutura guardada (a máquina continua). True se existia."""
        with LOCK:
            dados = self._ler()
            m = dados.get(nome)
            if m is None or projeto not in (m.get("projetos") or {}):
                return False
            del m["projetos"][projeto]
            self._gravar(dados)
            return True

    def tirar_agente(self, nome, agente):
        """Remove os projetos de um agente; apaga a máquina se não sobrar nenhum. Devolve quantos projetos restam (ou None)."""
        with LOCK:
            dados = self._ler()
            m = dados.get(nome)
            if m is None:
                return None
            restantes = {k: v for k, v in (m.get("projetos") or {}).items() if v.get("agente", "") != agente}
            if restantes:
                m["projetos"] = restantes
            else:
                del dados[nome]
            self._gravar(dados)
            return len(restantes)

    def remover(self, nome):
        with LOCK:
            dados = self._ler()
            existia = dados.pop(nome, None) is not None
            if existia:
                self._gravar(dados)
            return existia


class Config:
    """Configuração e estado volátil do painel, injetável nos testes."""

    def __init__(self, dados, **extra):
        self.dados = Path(dados)
        self.senha = os.environ.get("PAINEL_SENHA", "")
        self.publico = os.environ.get("PAINEL_PUBLICO", "")  # IP/host público p/ o download do convite
        self.ts_api_key = os.environ.get("TS_API_KEY", "")
        self.ts_api_base = "https://api.tailscale.com"
        self.ts_tag = os.environ.get("TS_TAG", "")
        self.tg_token = os.environ.get("TG_TOKEN", "")
        self.tg_chat = os.environ.get("TG_CHAT", "")
        self.tg_api = "https://api.telegram.org"
        self.raiz = RAIZ
        self.sincrono = False  # testes: envia o Telegram na própria requisição
        self.token = os.environ.get("PAINEL_TOKEN", "")
        self.tls = False
        self.sessoes = {}        # {sessao: expira_epoch}
        self.ev_quota = {}       # {maquina: [epoch]}
        self.arq_quota = {}      # {maquina: [epoch]}
        self.recentes = {}     # deduplicação de eventos
        self.vivo = {}         # {maquina: {"em": epoch, "payload": {...}}} (heartbeat dos agentes)
        self.tarefas = {}      # {id: {...}}
        self.atencao = {}      # {(maquina, projeto, terminal): epoch do último evento que pede você}
        for k, v in extra.items():
            setattr(self, k, v)

    @property
    def eventos_log(self):
        return self.dados / "eventos.log"

    @property
    def configs(self):
        return self.dados / "configs"

    @property
    def arquivo_config(self):
        return self.dados / "config.json"

    def notificacoes(self):
        try:
            salvo = json.loads(self.arquivo_config.read_text()).get("notificacoes", {})
        except (OSError, ValueError):
            salvo = {}
        return {k: bool(salvo.get(k, v)) for k, v in NOTIF_PADRAO.items()}

    def salvar_notificacoes(self, novos):
        atual = self.notificacoes()
        atual.update({k: bool(v) for k, v in novos.items() if k in NOTIF_PADRAO})
        self.dados.mkdir(parents=True, exist_ok=True)
        tmp = self.arquivo_config.with_suffix(".tmp")
        tmp.write_text(json.dumps({"notificacoes": atual}, indent=2))
        tmp.replace(self.arquivo_config)
        return atual


def _requisicao_json(url, corpo=None, metodo=None, cabecalhos=None, timeout=10):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo, headers=cabecalhos or {})
    if dados is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        texto = r.read().decode()
        return json.loads(texto) if texto else {}


def _auth_tailscale(cfg):
    token = base64.b64encode(f"{cfg.ts_api_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def criar_authkey(cfg, descricao, validade=3600):
    """Cria uma auth key de uso único, pré-autorizada, que expira em `validade` segundos."""
    if not cfg.ts_api_key:
        raise RuntimeError("TS_API_KEY não configurada")
    criar = {"reusable": False, "ephemeral": False, "preauthorized": True}
    if cfg.ts_tag:
        criar["tags"] = [cfg.ts_tag]
    corpo = {
        "capabilities": {"devices": {"create": criar}},
        "expirySeconds": validade,
        "description": f"tty-orquestrador {descricao}"[:50],
    }
    resp = _requisicao_json(
        f"{cfg.ts_api_base}/api/v2/tailnet/-/keys", corpo, "POST", _auth_tailscale(cfg)
    )
    return resp["key"], resp["id"]


def revogar_authkey(cfg, key_id):
    _requisicao_json(
        f"{cfg.ts_api_base}/api/v2/tailnet/-/keys/{key_id}", None, "DELETE", _auth_tailscale(cfg)
    )


def enviar_telegram(cfg, texto):
    if not (cfg.tg_token and cfg.tg_chat):
        return False
    try:
        _requisicao_json(
            f"{cfg.tg_api}/bot{cfg.tg_token}/sendMessage",
            {"chat_id": cfg.tg_chat, "text": texto, "disable_web_page_preview": True},
            "POST",
        )
        return True
    except (OSError, ValueError) as e:
        print(f"telegram falhou: {type(e).__name__}", file=sys.stderr, flush=True)
        return False


def enviar_telegram_arquivo(cfg, nome, dados, legenda):
    """sendDocument (multipart escrito à mão: só biblioteca padrão)."""
    if not (cfg.tg_token and cfg.tg_chat):
        return False
    lim = uuid.uuid4().hex
    nome_seguro = re.sub(r'[^\w.\- ]', "_", nome)[:100] or "arquivo"
    def campo(k, v):
        return f'--{lim}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    corpo = (campo("chat_id", cfg.tg_chat) + campo("caption", legenda[:1000])
             + f'--{lim}\r\nContent-Disposition: form-data; name="document"; filename="{nome_seguro}"\r\n'
               f'Content-Type: application/octet-stream\r\n\r\n'.encode()
             + dados + f"\r\n--{lim}--\r\n".encode())
    req = urllib.request.Request(f"{cfg.tg_api}/bot{cfg.tg_token}/sendDocument", data=corpo, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={lim}"})
    try:
        with urllib.request.urlopen(req, timeout=120):
            return True
    except (OSError, ValueError) as e:
        print(f"telegram (arquivo) falhou: {type(e).__name__}", file=sys.stderr, flush=True)
        return False


# ---------- eventos ----------

def tratar_evento(cfg, registro, ev, host=""):
    """Valida, deduplica, registra e (se o motivo estiver ligado) envia ao Telegram. Devolve (codigo, resposta)."""
    maquina = str(ev.get("maquina", "")).strip()
    motivo = str(ev.get("motivo", "")).strip()
    projeto = str(ev.get("projeto", "")).strip()
    terminal = str(ev.get("terminal", "")).strip()
    sessao = str(ev.get("sessao", "")).strip()
    texto = str(ev.get("texto", "")).replace("\x00", "").replace("\r", "").strip()[:500]
    if not NOME_OK.match(maquina) or motivo not in MOTIVOS:
        return 400, {"erro": f"maquina inválida ou motivo fora de {sorted(MOTIVOS)}"}
    if (projeto and not PROJ_OK.match(projeto)) or (terminal and not TERM_OK.match(terminal)):
        return 400, {"erro": "projeto ou terminal inválido"}
    if not _quota_ok(cfg, cfg.ev_quota, maquina or "?", LIMITE_EVENTOS_MIN):
        return 429, {"erro": "muitos eventos, tente mais tarde"}
    if not projeto and "/" in sessao:  # clientes antigos mandam só a sessão "projeto/terminal"
        p, _, t = sessao.partition("/")
        if PROJ_OK.match(p) and TERM_OK.match(t):
            projeto, terminal = p, t
    chave = (maquina, projeto, terminal, motivo, texto)
    agora = time.time()
    with LOCK:
        cfg.recentes = {k: t for k, t in cfg.recentes.items() if agora - t < JANELA_DEDUP}
        if chave in cfg.recentes:
            return 200, {"duplicado": True}
        cfg.recentes[chave] = agora
        if motivo in ATENCAO and projeto and terminal:
            cfg.atencao[(maquina, projeto, terminal)] = agora
    registro_ev = {"em": int(agora), "maquina": maquina, "sessao": sessao, "projeto": projeto, "terminal": terminal,
                   "motivo": motivo, "texto": texto}
    cfg.dados.mkdir(parents=True, exist_ok=True)
    with LOCK, cfg.eventos_log.open("a") as f:
        f.write(json.dumps(registro_ev, ensure_ascii=False) + "\n")
    if not cfg.notificacoes().get(motivo, True):
        return 200, {"telegram": False, "silenciado": True}
    local = maquina + (f" · {projeto}/{terminal}" if projeto and terminal else (f" · {projeto}" if projeto else (f" · {sessao}" if sessao else "")))
    msg = f"{MOTIVOS[motivo]} {local}\n{motivo}" + (f": {texto}" if texto else "")
    if host:
        msg += f"\nhttp://{host}/#/{maquina}" + (f"/{projeto}/{terminal}" if projeto and terminal else "")
    if cfg.sincrono:
        enviado = enviar_telegram(cfg, msg)
    else:
        threading.Thread(target=enviar_telegram, args=(cfg, msg), daemon=True).start()
        enviado = bool(cfg.tg_token and cfg.tg_chat)
    return 200, {"telegram": enviado}


def ultimos_eventos(cfg, n=20):
    try:
        linhas = cfg.eventos_log.read_text().splitlines()[-n:]
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in linhas]


# ---------- agentes: heartbeat, tarefas, estado ----------

def _uniao_projetos(agentes, agora):
    """Projetos de todos os agentes de uma máquina. Em caso de nome repetido ganha o agente mais fresco
    (um agente parado não pode esconder o projeto de quem está respondendo)."""
    uniao = {}
    for ag in sorted(agentes, key=lambda a: (agora - agentes[a]["em"] > ONLINE_S, a)):
        for nome, p in agentes[ag]["payload"]["projetos"].items():
            uniao.setdefault(nome, {**p, "agente": ag})
    return uniao


def receber_heartbeat(cfg, registro, maquina, payload, agora=None):
    """Guarda o heartbeat de UM agente da máquina, aplica resultados de tarefas e devolve as tarefas dele.
    Uma máquina pode ter vários agentes (um por usuário, no modo sem root): os projetos se somam."""
    agora = time.time() if agora is None else agora
    if maquina not in registro.listar():
        return None
    agente = str(payload.get("agente") or "").strip()
    if agente and not AGENTE_OK.match(agente):
        agente = ""
    projetos = payload.get("projetos") if isinstance(payload.get("projetos"), dict) else {}
    limpos = {}
    for nome, p in projetos.items():
        if not PROJ_OK.match(str(nome)) or not isinstance(p, dict):
            continue
        terminais = {t: v for t, v in (p.get("terminais") or {}).items() if TERM_OK.match(str(t)) and isinstance(v, dict)}
        limpos[nome] = {
            "dir": str(p.get("dir", "")), "usuario": str(p.get("usuario", "")), "porta": int(p.get("porta", 0)),
            "branch": str(p.get("branch", ""))[:80], "cpu": float(p.get("cpu", 0)), "mem_mb": int(p.get("mem_mb", 0)),
            "terminais": terminais,
        }
    vivo = {"cores": int(payload.get("cores", 0)), "ram_mb": int(payload.get("ram_mb", 0)),
            "mem_mb": int(payload.get("mem_mb", 0)), "cpu": float(payload.get("cpu", 0)),
            "shell": str(payload.get("shell", ""))[:20], "projetos": limpos,
            "ferramentas": {str(k): bool(v) for k, v in (payload.get("ferramentas") or {}).items() if FERRAMENTA_OK.match(str(k))}}
    with LOCK:
        agentes = cfg.vivo.setdefault(maquina, {})
        agentes[agente] = {"em": agora, "payload": vivo}
        for ag in [a for a in agentes if agora - agentes[a]["em"] > AGENTE_VALE_S]:  # agente sumido há muito tempo
            del agentes[ag]
        pendentes = _resultados_e_pendentes(cfg, maquina, agente, payload.get("resultados"), agora)
        uniao = _uniao_projetos(agentes, agora)
    registro.guardar_projetos(maquina, uniao, vivo["cores"], vivo["ram_mb"])
    return {"tarefas": pendentes}


def _resultados_e_pendentes(cfg, maquina, agente, resultados, agora):
    """(com o LOCK já tomado) aplica os resultados que o agente trouxe e entrega as tarefas pendentes dele."""
    meu = lambda t: t["maquina"] == maquina and t.get("agente") in (None, agente)  # noqa: E731
    for r in resultados or []:
        t = cfg.tarefas.get(r.get("id")) if isinstance(r, dict) else None
        if t and meu(t):
            t["estado"] = "ok" if r.get("ok") else "erro"
            t["saida"] = str(r.get("saida", ""))[-500:]
            dados = r.get("dados")
            if dados is not None and len(json.dumps(dados, ensure_ascii=False)) <= MAX_DADOS_TAREFA:
                t["dados"] = dados
    pendentes = []
    for t in cfg.tarefas.values():
        if not meu(t) or t["estado"] != "pendente":
            continue
        if agora - t["criada_em"] > TAREFA_VALE_S:
            t["estado"] = "expirada"
            continue
        t["estado"] = "enviada"
        pendentes.append({"id": t["id"], "tipo": t["tipo"], "args": t["args"]})
    for k in [k for k, t in cfg.tarefas.items() if agora - t["criada_em"] > 3600]:
        del cfg.tarefas[k]
    return pendentes


def consultar_tarefas(cfg, registro, maquina, corpo, agora=None):
    """Consulta leve (agente, a cada 1 s): entrega resultados e devolve tarefas, sem mexer em métricas.
    Só vale para agentes que já mandaram heartbeat (um desconhecido não pega tarefas)."""
    agora = time.time() if agora is None else agora
    if maquina not in registro.listar():
        return None
    agente = str(corpo.get("agente") or "").strip()
    if agente and not AGENTE_OK.match(agente):
        agente = ""
    with LOCK:
        if agente not in cfg.vivo.get(maquina, {}):
            return {"tarefas": []}
        return {"tarefas": _resultados_e_pendentes(cfg, maquina, agente, corpo.get("resultados"), agora)}


def _agente_alvo(agentes, tipo, args):
    """Qual agente da máquina deve executar a tarefa: o dono do projeto ou do usuário citado."""
    nome = args.get("projeto") or (args.get("nome") if tipo == "projeto.rm" else None)
    if nome:
        for ag, v in agentes.items():
            if nome in v["payload"]["projetos"]:
                return ag
    usuario = args.get("usuario")
    if usuario:
        if usuario in agentes:
            return usuario
        for ag, v in agentes.items():
            if any(p["usuario"] == usuario for p in v["payload"]["projetos"].values()):
                return ag
    return None


def criar_tarefa(cfg, registro, corpo, agora=None):
    maquina = str(corpo.get("maquina", ""))
    tipo = corpo.get("tipo")
    args = corpo.get("args") or {}
    if maquina not in registro.listar():
        return 404, {"erro": "máquina desconhecida"}
    if tipo not in TIPOS_TAREFA or not isinstance(args, dict):
        return 400, {"erro": f"tipo deve ser um de {sorted(TIPOS_TAREFA)}"}
    if not _args_tarefa_validos(tipo, args):
        return 400, {"erro": "args inválidos para este tipo"}
    agentes = cfg.vivo.get(maquina, {})
    alvo = corpo.get("agente")
    if alvo is not None:
        if alvo not in agentes:
            return 400, {"erro": "agente desconhecido nesta máquina"}
    else:
        alvo = _agente_alvo(agentes, tipo, args)
        if alvo is None and len(agentes) > 1 and tipo in ("projeto.add", "ferramenta.add", "ferramenta.push"):
            return 400, {"erro": "esta máquina tem vários agentes (usuários): informe o usuário"}
    tid = uuid.uuid4().hex
    with LOCK:
        cfg.tarefas[tid] = {"id": tid, "maquina": maquina, "agente": alvo, "tipo": tipo, "args": args, "estado": "pendente",
                            "saida": "", "criada_em": time.time() if agora is None else agora}
    return 200, {"id": tid}


def esquecer_projeto(cfg, registro, maquina, projeto, agora=None):
    """Tira do painel um projeto cujo agente está OFFLINE (fantasma). Com o agente online use projeto.rm (desliga o ttyd)."""
    agora = time.time() if agora is None else agora
    if maquina not in registro.listar():
        return 404, {"erro": "máquina desconhecida"}
    with LOCK:
        agentes = cfg.vivo.get(maquina, {})
        achou = False
        for ag, v in agentes.items():
            if projeto in v["payload"]["projetos"]:
                achou = True
                if agora - v["em"] <= ONLINE_S:
                    return 409, {"erro": "o agente deste projeto está online: use Remover (ele desliga o terminal web)"}
        for v in agentes.values():
            v["payload"]["projetos"].pop(projeto, None)
    achou = registro.tirar_projeto(maquina, projeto) or achou
    return (200, {"esquecido": projeto}) if achou else (404, {"erro": "projeto desconhecido"})


def remover_agente(cfg, registro, maquina, agente):
    """Tira UM agente (usuário) da máquina; a máquina só some quando não sobra nenhum projeto."""
    with LOCK:
        agentes = cfg.vivo.get(maquina, {})
        agentes.pop(agente, None)
        if not agentes:
            cfg.vivo.pop(maquina, None)
    restam = registro.tirar_agente(maquina, agente)
    return restam is not None, restam or 0


def _estado_terminal(t, maquina, projeto, nome, online, em, cfg):
    atividade_em = em - int(t.get("atividade_s", 0))
    ts = cfg.atencao.get((maquina, projeto, nome))
    if not online:
        estado = "offline"
    elif ts is not None and atividade_em <= ts + 2:
        estado = "aguardando"
    elif t.get("cmd") not in SHELLS or int(t.get("atividade_s", 999)) < RODANDO_S:
        estado = "rodando"
    else:
        estado = "ocioso"
    return {"nome": nome, "cmd": t.get("cmd", ""), "cpu": t.get("cpu", 0), "mem_mb": t.get("mem_mb", 0), "estado": estado}


def montar_estado(cfg, registro, agora=None):
    agora = time.time() if agora is None else agora
    maquinas = []
    for nome, m in sorted(registro.listar().items()):
        agentes = cfg.vivo.get(nome, {})
        on = lambda ag: agora - agentes[ag]["em"] <= ONLINE_S  # noqa: E731
        frescos = [ag for ag in agentes if on(ag)]
        online = bool(frescos)
        ref = max(frescos or list(agentes), key=lambda ag: agentes[ag]["em"], default=None)  # métricas: a leitura mais nova
        vivo = agentes[ref]["payload"] if ref is not None else {}
        projs = _uniao_projetos(agentes, agora) if agentes else {
            k: {**p, "branch": "", "cpu": 0, "mem_mb": 0, "terminais": {}} for k, p in (m.get("projetos") or {}).items()}
        lista = []
        for pn, p in sorted(projs.items()):
            dono = p.get("agente", "")
            p_online = bool(dono in agentes and on(dono))
            terms = [_estado_terminal(t, nome, pn, tn, p_online, agentes[dono]["em"] if dono in agentes else agora, cfg)
                     for tn, t in sorted(p["terminais"].items())]
            estados = {t["estado"] for t in terms}
            est = ("offline" if not p_online else "aguardando" if "aguardando" in estados
                   else "rodando" if "rodando" in estados else "ocioso")
            lista.append({"nome": pn, "dir": p["dir"], "usuario": p["usuario"], "porta": p["porta"], "agente": dono,
                          "branch": p.get("branch", ""), "cpu": p.get("cpu", 0), "mem_mb": p.get("mem_mb", 0),
                          "estado": est, "terminais": terms})
        ferr = {}
        for ag in agentes:
            for k, v in agentes[ag]["payload"].get("ferramentas", {}).items():
                ferr[k] = ferr.get(k, False) or v
        visto = int(max((agentes[ag]["em"] for ag in agentes), default=m.get("visto_em", 0)))
        maquinas.append({"nome": nome, "ip": m["ip"], "online": online, "visto_em": visto,
                         "cores": vivo.get("cores", m.get("cores", 0)), "ram_mb": vivo.get("ram_mb", m.get("ram_mb", 0)),
                         "cpu": vivo.get("cpu", 0), "mem_mb": vivo.get("mem_mb", 0), "shell": vivo.get("shell", ""),
                         "ferramentas": ferr,
                         "agentes": [{"id": ag, "online": on(ag), "ferramentas": agentes[ag]["payload"].get("ferramentas", {})} for ag in sorted(agentes)],
                         "projetos": lista})
    return {"agora": int(agora), "maquinas": maquinas, "eventos": ultimos_eventos(cfg, 30),
            "config": {"notificacoes": cfg.notificacoes(),
                       "telegram": bool(cfg.tg_token and cfg.tg_chat), "convites": bool(cfg.ts_api_key)}}


def info_config(cfg, nome):
    """A config de uma ferramenta guardada no painel (tty-push): {"tem", "bytes", "em"}."""
    try:
        st = (cfg.configs / f"{nome}.tar.gz").stat()
        return {"tem": True, "bytes": st.st_size, "em": int(st.st_mtime)}
    except OSError:
        return {"tem": False, "bytes": 0, "em": 0}


def listar_ferramentas(raiz):
    """[{nome, bin, desc}] lido (sem executar) de facilitadores/ferramentas/*.sh."""
    achadas = []
    for arq in sorted((Path(raiz) / "facilitadores" / "ferramentas").glob("*.sh")):
        if arq.stem.startswith("_") or not FERRAMENTA_OK.match(arq.stem):
            continue
        txt = arq.read_text()
        b = re.search(r"^BIN=(\S+)", txt, re.M)
        d = re.search(r'^DESC="([^"\n]*)"', txt, re.M)
        achadas.append({"nome": arq.stem, "bin": b.group(1) if b else arq.stem, "desc": d.group(1) if d else ""})
    return achadas


def montar_pacote(raiz):
    """tar.gz em memória com os scripts que vão para as máquinas (sem testes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            base = Path(raiz) / pasta
            if not base.is_dir():
                continue
            for arq in sorted(base.rglob("*")):
                if arq.is_file() and not arq.name.startswith("test") and "__pycache__" not in arq.parts:
                    tar.add(arq, arcname=str(arq.relative_to(raiz)))
    return buf.getvalue()


LIMITE_ARQUIVO = 50 * 1024 * 1024  # teto do sendDocument do Telegram

# GETs sem segredos liberados para a internet: a máquina nova ainda não tem Tailscale e precisa
# baixar o instalador antes de entrar na tailnet (o resto do fluxo já é pós-`tailscale up`).
SEM_SENHA = {"/bootstrap.sh"}


def ip_isento(ip):
    """True para localhost e rede Tailscale (entram sem senha). O resto (IP público) exige PAINEL_SENHA."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
        return (addr.is_loopback
                or addr in ipaddress.ip_network("100.64.0.0/10")      # Tailscale IPv4 (CGNAT)
                or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"))  # Tailscale IPv6
    except ValueError:
        return False


def senha_confere(autorizacao, senha):
    """Basic Auth: usuário livre, senha tem que ser a PAINEL_SENHA."""
    if not autorizacao or not autorizacao.startswith("Basic "):
        return False
    try:
        decodificado = base64.b64decode(autorizacao[6:].strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, tentativa = decodificado.partition(":")
    return bool(tentativa) and hmac.compare_digest(tentativa, senha)


def criar_handler(registro, cfg):
    class Handler(BaseHTTPRequestHandler):
        def version_string(self):
            return "Tessera"

        def _negar(self, sem_senha):
            # Tailscale/localhost: nunca chega aqui. IP público sem PAINEL_SENHA: 403 fechado.
            # IP público com senha mas sem credencial: 401 (o navegador mostra o prompt de login).
            if sem_senha:
                self._json(403, {"erro": "painel fechado para a internet (sem PAINEL_SENHA no servidor)"})
                return
            ip = self.client_address[0]
            if _registrar_falha(ip):
                self._muitas_tentativas()
                return
            time.sleep(0.5)  # freio contra brute-force na senha
            self._responder(401, json.dumps({"erro": "senha do painel"}),
                            extra={"WWW-Authenticate": 'Basic realm="Tessera"', "Cache-Control": "no-store"})

        def _muitas_tentativas(self):
            self._responder(429, json.dumps({"erro": "muitas tentativas, tente mais tarde"}),
                            extra={"Cache-Control": "no-store", "Retry-After": str(BLOQUEIO_S)})

        def _autenticado(self):
            if _is_localhost(self.client_address[0]):
                return True
            if _sessao_ok(cfg, self):
                return True
            tok = _bearer(self)
            if tok and cfg.token and hmac.compare_digest(tok, cfg.token):
                return True
            if not cfg.token and _plano_agente(urlparse(self.path).path) and ip_isento(self.client_address[0]):
                return True  # ponte de migração: sem PAINEL_TOKEN, agentes antigos na tailnet ainda falam
            if _bloqueado(self.client_address[0]):
                self._muitas_tentativas()
                return False
            if not cfg.senha and not cfg.token:
                self._negar(sem_senha=True)
                return False
            if cfg.senha and senha_confere(self.headers.get("Authorization"), cfg.senha):
                _limpar_falhas(self.client_address[0])
                self._sessao_nova = _nova_sessao(cfg)
                return True
            self._negar(sem_senha=False)
            return False

        def _responder(self, codigo, corpo, tipo="application/json", extra=None):
            dados = corpo.encode() if isinstance(corpo, str) else corpo
            self.send_response(codigo)
            self.send_header("Content-Type", tipo if "/gzip" in tipo or "octet" in tipo or tipo.startswith("font/") else tipo + "; charset=utf-8")
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP_PADRAO)
            if getattr(cfg, "tls", False):
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if getattr(self, "_sessao_nova", None):
                seguro = "; Secure" if getattr(cfg, "tls", False) else ""
                self.send_header("Set-Cookie", f"Tessera={self._sessao_nova}; HttpOnly; SameSite=Lax{seguro}; Path=/; Max-Age={SESSAO_VALE_S}")
                self._sessao_nova = None
            self.end_headers()
            self.wfile.write(dados)

        def _json(self, codigo, obj):
            self._responder(codigo, json.dumps(obj, ensure_ascii=False), extra={"Cache-Control": "no-store"})

        def _nao_suportado(self):
            if not self._autenticado():
                return
            self._json(501, {"erro": "método não suportado"})

        do_PATCH = _nao_suportado
        do_OPTIONS = _nao_suportado
        do_TRACE = _nao_suportado
        do_HEAD = _nao_suportado
        do_CONNECT = _nao_suportado
        do_PROPFIND = _nao_suportado

        def _corpo(self, limite=1024 * 1024):
            tam = int(self.headers.get("Content-Length", 0))
            if tam > limite:
                raise ValueError("corpo grande demais")
            return self.rfile.read(tam)

        def _corpo_json(self):
            corpo = json.loads(self._corpo() or b"{}")
            if not isinstance(corpo, dict):
                raise ValueError("esperado um objeto JSON")
            return corpo

        def _estatico(self, rel):
            if "\x00" in rel:
                return self._json(404, {"erro": "não encontrado"})
            try:
                dec = unquote(rel)
            except Exception:
                dec = rel
            if any(seg in ("..", ".") for seg in dec.split("/")):
                return self._json(404, {"erro": "não encontrado"})
            alvo = (ESTATICO / rel).resolve()
            if ESTATICO.resolve() not in alvo.parents or not alvo.is_file():
                return self._json(404, {"erro": "não encontrado"})
            tipo = TIPOS_MIME.get(alvo.suffix, "application/octet-stream")
            self._responder(200, alvo.read_bytes(), tipo, {"Cache-Control": "no-cache"})

        def do_GET(self):
            url = urlparse(self.path)
            caminho = url.path
            if caminho not in SEM_SENHA and not self._autenticado():
                return
            if caminho.startswith("/tty/"):
                partes = caminho.split("/", 4)  # ['', 'tty', <maquina>, <projeto>, <resto>]
                if len(partes) != 5:
                    return self._json(404, {"erro": "não encontrado"})
                resto = "/" + partes[4] + (("?" + url.query) if url.query else "")
                return self._rele(partes[2], partes[3], resto)
            if caminho == "/":
                self._estatico("index.html")
            elif caminho.startswith("/static/"):
                self._estatico(caminho[len("/static/"):])
            elif caminho == "/api/estado":
                self._json(200, montar_estado(cfg, registro))
            elif caminho == "/api/maquinas":
                self._json(200, registro.listar())
            elif caminho == "/api/eventos":
                n = int(parse_qs(url.query).get("n", ["20"])[0])
                self._json(200, ultimos_eventos(cfg, max(1, min(n, 200))))
            elif caminho == "/api/config":
                self._json(200, {"notificacoes": cfg.notificacoes()})
            elif caminho == "/api/ferramentas":
                self._json(200, [{**f, "config": info_config(cfg, f["nome"])} for f in listar_ferramentas(cfg.raiz)])
            elif caminho.startswith("/api/tarefas/"):
                t = cfg.tarefas.get(caminho.rsplit("/", 1)[1])
                if not t:
                    return self._json(404, {"erro": "tarefa desconhecida"})
                self._json(200, {**{k: t[k] for k in ("id", "maquina", "tipo", "estado", "saida")},
                                 **({"dados": t["dados"]} if "dados" in t else {})})
            elif caminho == "/bootstrap.sh":
                self._servir_arquivo(cfg.raiz / "bootstrap" / "bootstrap.sh", "text/x-shellscript")
            elif caminho == "/pacote.tar.gz":
                self._responder(200, montar_pacote(cfg.raiz), "application/gzip")
            elif caminho.startswith("/api/config/"):
                nome = caminho.rsplit("/", 1)[1]
                if not NOME_OK.match(nome):
                    return self._json(400, {"erro": "nome inválido"})
                self._servir_arquivo(cfg.configs / f"{nome}.tar.gz", "application/gzip")
            else:
                self._json(404, {"erro": "não encontrado"})

        def _servir_arquivo(self, caminho, tipo):
            try:
                self._responder(200, caminho.read_bytes(), tipo)
            except FileNotFoundError:
                self._json(404, {"erro": f"{caminho.name} não existe"})

        def _rele(self, maquina, projeto, resto):
            """Relé do ttyd: o navegador (sem Tailscale) fala com o painel e o painel
            fala com a máquina pela tailnet. Vale para HTTP e para o upgrade WebSocket
            (depois do 101 os bytes passam crus nos dois sentidos)."""
            if not NOME_OK.match(maquina) or not PROJ_OK.match(projeto) or not resto.startswith("/"):
                return self._json(400, {"erro": "nome inválido"})
            _q = parse_qs(urlparse(resto).query)
            for _v in _q.get("arg", []):
                if not TERM_OK.match(_v):
                    return self._json(400, {"erro": "arg inválido"})
            if self.command != "GET":
                return self._json(405, {"erro": "só GET"})
            m = registro.listar().get(maquina)
            porta = ((m.get("projetos") or {}).get(projeto) or {}).get("porta") if m else 0
            if not m or not porta:
                return self._json(404, {"erro": "máquina ou projeto desconhecido (sem ttyd?)"})
            if not ip_isento(m.get("ip", "")):
                # o IP vem do /api/registrar (dado pela máquina): sem isto o relé virava
                # proxy aberto para qualquer endereço (SSRF, ex.: metadata da nuvem)
                return self._json(502, {"erro": "IP da máquina fora da tailnet"})
            try:
                alvo = socket.create_connection((m["ip"], int(porta)), timeout=10)
            except (OSError, ValueError):
                return self._json(502, {"erro": "não alcancei o ttyd pela tailnet"})
            try:
                pedido = [f"GET {resto} HTTP/1.1"]
                for chave, valor in self.headers.items():
                    if chave.lower() in ("authorization", "cookie", "host", "origin", "referer"):
                        continue  # credencial e origem do navegador não seguem para o ttyd
                    pedido.append(f"{chave}: {valor}")
                pedido.append(f"Host: {m['ip']}:{porta}")
                pedido.append(f"Origin: http://{m['ip']}:{porta}")
                pedido.append(f"X-Forwarded-For: {self.client_address[0]}")
                alvo.sendall(("\r\n".join(pedido) + "\r\n\r\n").encode("latin-1"))
                self.close_connection = True
                cli = self.connection
                while True:
                    prontos, _, _ = select.select([cli, alvo], [], [])
                    for origem in prontos:
                        destino = alvo if origem is cli else cli
                        try:
                            dados = origem.recv(65536)
                        except OSError:
                            dados = b""
                        if not dados:
                            return
                        try:
                            destino.sendall(dados)
                        except OSError:
                            return
            except (OSError, ValueError):
                return
            finally:
                try:
                    alvo.close()
                except OSError:
                    pass

        def do_POST(self):
            if not self._autenticado():
                return
            caminho = urlparse(self.path).path
            try:
                if caminho == "/api/registrar":
                    return self._registrar()
                if caminho == "/api/evento":
                    codigo, resp = tratar_evento(cfg, registro, self._corpo_json(), self.headers.get("Host", ""))
                    return self._json(codigo, resp)
                if caminho == "/api/arquivo":
                    return self._arquivo(urlparse(self.path).query)
                if caminho == "/api/convite":
                    return self._convite()
                if caminho == "/api/tarefas":
                    codigo, resp = criar_tarefa(cfg, registro, self._corpo_json())
                    return self._json(codigo, resp)
                if caminho.startswith("/api/agente/") and caminho.endswith("/tarefas"):
                    maquina = caminho[len("/api/agente/"):-len("/tarefas")]
                    if not NOME_OK.match(maquina):
                        return self._json(400, {"erro": "nome inválido"})
                    resp = consultar_tarefas(cfg, registro, maquina, self._corpo_json())
                    return self._json(200, resp) if resp is not None else self._json(404, {"erro": "máquina não registrada"})
                if caminho.startswith("/api/agente/"):
                    maquina = caminho.rsplit("/", 1)[1]
                    if not NOME_OK.match(maquina):
                        return self._json(400, {"erro": "nome inválido"})
                    resp = receber_heartbeat(cfg, registro, maquina, self._corpo_json())
                    return self._json(200, resp) if resp is not None else self._json(404, {"erro": "máquina não registrada"})
                if caminho.startswith("/api/config/"):
                    return self._guardar_config(caminho.rsplit("/", 1)[1])
            except (ValueError, TypeError, KeyError, AttributeError):
                return self._json(400, {"erro": "requisição inválida"})
            self._json(404, {"erro": "não encontrado"})

        def do_PUT(self):
            if not self._autenticado():
                return
            if urlparse(self.path).path != "/api/config":
                return self._json(404, {"erro": "não encontrado"})
            try:
                corpo = self._corpo_json()
                novos = corpo.get("notificacoes")
                if not isinstance(novos, dict):
                    raise ValueError
            except (ValueError, TypeError):
                return self._json(400, {"erro": "esperado {notificacoes: {motivo: bool}}"})
            self._json(200, {"notificacoes": cfg.salvar_notificacoes(novos)})

        def _arquivo(self, query):
            q = {k: v[0] for k, v in parse_qs(query).items()}
            maquina, projeto, terminal = q.get("maquina", ""), q.get("projeto", ""), q.get("terminal", "")
            if not NOME_OK.match(maquina) or (projeto and not PROJ_OK.match(projeto)) or (terminal and not TERM_OK.match(terminal)):
                return self._json(400, {"erro": "maquina, projeto ou terminal inválido"})
            try:
                dados = self._corpo(LIMITE_ARQUIVO)
            except ValueError:
                return self._json(413, {"erro": "arquivo maior que 50 MB"})
            if not dados:
                return self._json(400, {"erro": "arquivo vazio"})
            if not _quota_ok(cfg, cfg.arq_quota, maquina, LIMITE_ARQUIVOS_MIN):
                return self._json(429, {"erro": "muitos arquivos, tente mais tarde"})
            nome = q.get("nome", "arquivo")
            local = maquina + (f" · {projeto}/{terminal}" if projeto and terminal else (f" · {projeto}" if projeto else ""))
            legenda = f"📎 {local}" + (f"\n{q['legenda'][:500]}" if q.get("legenda") else "")
            self._json(200, {"telegram": enviar_telegram_arquivo(cfg, nome, dados, legenda)})

        def _registrar(self):
            corpo = self._corpo_json()
            nome, ip, porta = str(corpo["nome"]).strip(), str(corpo["ip"]).strip(), int(corpo["porta"])
            if not NOME_OK.match(nome) or not 0 < porta < 65536:
                return self._json(400, {"erro": "nome ou porta inválidos"})
            try:
                ipaddress.ip_address(ip.split("%")[0])
            except ValueError:
                return self._json(400, {"erro": "ip inválido"})
            usuario = str(corpo.get("usuario", ""))
            if usuario and not USUARIO_OK.match(usuario):
                return self._json(400, {"erro": "usuário inválido"})
            self._json(200, registro.registrar(nome, ip, porta, usuario))

        def _convite(self):
            corpo = self._corpo_json()
            nome = str(corpo.get("nome", "")).strip()
            if not NOME_OK.match(nome):
                return self._json(400, {"erro": "nome inválido"})
            porta = self.server.server_address[1]
            esquema = "https" if getattr(cfg, "tls", False) else "http"
            ts = ip_tailnet()
            if ts:  # o agente fala pela tailnet com token; o IP público exige senha
                painel = f"{esquema}://{ts}:{porta}"
            else:
                painel = f"{esquema}://{self.headers.get('Host', '')}"
            # o instalador é baixado ANTES de entrar na tailnet: usa o IP público (ou a
            # página atual), que serve o /bootstrap.sh sem senha
            origem = f"{esquema}://{cfg.publico}:{porta}" if cfg.publico else f"{esquema}://{self.headers.get('Host', '')}"
            extra_tok = f" --token {cfg.token}" if getattr(cfg, "token", "") else ""
            if corpo.get("sem_root"):  # usuário comum de uma máquina que já está na tailnet: sem sudo, sem auth key
                return self._json(200, {"comando": f"curl -fsSL {origem}/bootstrap.sh | bash -s -- --nome {nome} --painel {painel}{extra_tok}"})
            try:
                key, _ = criar_authkey(cfg, nome)
            except (RuntimeError, OSError, ValueError, KeyError) as e:
                return self._json(502, {"erro": f"não consegui gerar a auth key: {e}"})
            comando = (
                f"curl -fsSL {origem}/bootstrap.sh | "
                f"sudo TS_AUTHKEY={key} bash -s -- --nome {nome} --painel {painel}{extra_tok}"
            )
            self._json(200, {"comando": comando})

        def _guardar_config(self, nome):
            if not NOME_OK.match(nome):
                return self._json(400, {"erro": "nome inválido"})
            dados = self._corpo(LIMITE_CONFIG)
            cfg.configs.mkdir(parents=True, exist_ok=True)
            destino = cfg.configs / f"{nome}.tar.gz"
            tmp = destino.with_suffix(".tmp")
            tmp.write_bytes(dados)
            tmp.replace(destino)
            self._json(200, {"guardado": nome, "bytes": len(dados)})

        def do_DELETE(self):
            if not self._autenticado():
                return
            caminho = urlparse(self.path).path
            if not caminho.startswith("/api/maquinas/"):
                return self._json(404, {"erro": "não encontrado"})
            partes = caminho.split("/")  # ['', 'api', 'maquinas', <m>, 'projetos', <p>]
            if len(partes) == 6 and partes[4] == "projetos":
                if not NOME_OK.match(partes[3]) or not PROJ_OK.match(partes[5]):
                    return self._json(400, {"erro": "nome inválido"})
                codigo, resp = esquecer_projeto(cfg, registro, partes[3], partes[5])
                return self._json(codigo, resp)
            nome = caminho.rsplit("/", 1)[1]
            agente = parse_qs(urlparse(self.path).query).get("agente", [None])[0]
            if agente is not None:  # um usuário saiu: a máquina só some quando não resta projeto nenhum
                existe, restam = remover_agente(cfg, registro, nome, agente)
                return self._json(200 if existe else 404, {"removido": nome, "agente": agente, "projetos_restantes": restam})
            with LOCK:
                cfg.vivo.pop(nome, None)
            self._json(200 if registro.remover(nome) else 404, {"removido": nome})

        def log_message(self, fmt, *args):
            pass

    return Handler


def ip_tailnet():
    """IP Tailscale deste servidor ("" se sem Tailscale). Máquinas só falam pela tailnet,
    então o convite sempre aponta para cá — nunca para o IP público (que exige senha)."""
    try:
        saida = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        ).stdout.split()
        return saida[0] if saida else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def endereco_padrao():
    return ip_tailnet() or "127.0.0.1"


def main():
    carregar_env(AQUI / ".env")
    bind = os.environ.get("PAINEL_BIND") or "0.0.0.0"  # Tailscale + IP público; o filtro por IP decide a senha
    porta = int(os.environ.get("PAINEL_PORTA", 8787))
    dados = Path(os.environ.get("PAINEL_DADOS", AQUI / "dados"))
    cfg = Config(dados)
    registro = Registro(dados / "maquinas.json")
    servidor = ThreadingHTTPServer((bind, porta), criar_handler(registro, cfg))
    cert, chave = os.environ.get("PAINEL_TLS_CERT", ""), os.environ.get("PAINEL_TLS_KEY", "")
    if cert and chave:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, chave)
        servidor.socket = ctx.wrap_socket(servidor.socket, server_side=True)
        cfg.tls = True
    print(
        f"painel em http://{bind}:{porta}/ "
        f"(telegram: {'on' if cfg.tg_token and cfg.tg_chat else 'off'}, "
        f"tailscale api: {'on' if cfg.ts_api_key else 'off'}, "
        f"senha pública: {'on' if cfg.senha else 'OFF — internet bloqueada até definir PAINEL_SENHA'})",
        flush=True,
    )
    servidor.serve_forever()


if __name__ == "__main__":
    main()
