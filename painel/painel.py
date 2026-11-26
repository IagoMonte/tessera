#!/usr/bin/env python3
"""Painel único do tty-orquestrador.

Interface Tessera (static/), estado das máquinas/projetos/terminais (heartbeat dos agentes),
fila de tarefas para os agentes, eventos (com envio ao Telegram), convites (auth key da Tailscale)
e distribuição do bootstrap, do pacote de scripts e das configurações das ferramentas.

Só biblioteca padrão. Roda no PC durante o desenvolvimento e, em produção, num servidor.

Configuração (variáveis de ambiente ou arquivo .env ao lado deste script):
  PAINEL_BIND    endereço de escuta (padrão: 0.0.0.0 em produção, 127.0.0.1 em dev). Conexões da tailnet são descartadas pelo código
  PAINEL_PORTA   porta (padrão 8787)
  PAINEL_TOTP_SECRET  2º fator (base32; gere com --totp-novo). Obrigatório em produção; o login é senha + código do app.
  PAINEL_SENHA   senha de humanos (navegador), 20+ caracteres. Só https://<PAINEL_DOMINIO>; a tailnet nunca acessa o painel.
  PAINEL_TOKEN   segredo MESTRE (não sai do painel): cada máquina recebe um token derivado dele no convite. Sem ele, o plano-agente é 401.
  PAINEL_ATRAS_PROXY=1  entrada ÚNICA = proxy reverso local (Caddy): só escuta em loopback, só aceita conexão do loopback COM
                 X-Forwarded-For (o IP do cliente passa a ser o último valor dele); IP:porta direto nunca atende
  PAINEL_DOMINIO único Host aceito (ex.: painel.exemplo.com); obrigatório em produção, junto com PAINEL_TLS_CERT/KEY
  PAINEL_LOCALHOST_LIVRE=1  só dev/testes: loopback entra sem senha
  PAINEL_TLS_CERT/PAINEL_TLS_KEY  ativa HTTPS (HSTS + cookie Secure)
  PAINEL_PUBLICO IP ou host público usado no download do instalador do convite (opcional; padrão: a página atual)
  PAINEL_DADOS   pasta de dados: maquinas.json, eventos.log, configs/ (padrão: dados/ ao lado)
  TS_API_KEY     credencial da API da Tailscale (gera as auth keys dos convites)
  TS_TAG         tag opcional para as máquinas convidadas (ex.: tag:tty)
  TG_TOKEN       token do bot do Telegram
  TG_CHAT        chat id que recebe os avisos
"""
import base64
import hashlib
import math
import secrets
import struct
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
NOME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PROJ_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
TERM_OK = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
CRED_OK = re.compile(r"^[0-9a-f]{32}\Z")  # segredo do ttyd de cada projeto (CRED= no .conf; usuário fixo "tessera")
AGENTE_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\Z")
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
USUARIO_OK = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
FERRAMENTA_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
# Freio de brute-force na senha pública (só conta falha de auth, nunca o uso legítimo):
# após LIMITE_FALHAS 401 em JANELA_FALHAS_S, o IP leva 429 com Retry-After por BLOQUEIO_S; reincidente dobra o bloqueio
# (até BLOQUEIO_MAX_S) e só volta ao nível 0 depois de BLOQUEIO_MAX_S sem nova falha ou de um login ok.
LIMITE_FALHAS = 5
JANELA_FALHAS_S = 60
BLOQUEIO_S = 60
BLOQUEIO_MAX_S = 3600
# CSP que permite exatamente o que o Tessera usa (sem mudar a UI):
# - connect só 'self' (o terminal passa pelo relé /tty/, mesmo host), fontes do Google, hash do <script> de tema.
# - style 'unsafe-inline': o xterm.js (renderizador DOM) injeta um <style> com a paleta;
#   sem isso todo terminal vira preto-e-branco. Scripts seguem sem 'unsafe-inline'.
CSP_TEMA_HASH = "sha256-0mivrbvGuI+6CBwI4dtn8IsEKJ7B6nwKDAp01TbkzJk="
CSP_PADRAO = (f"default-src 'self'; script-src 'self' '{CSP_TEMA_HASH}'; "
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
              "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
LIMITE_EVENTOS_MIN = 30  # por máquina
LIMITE_ARQUIVOS_MIN = 6  # por máquina
MAX_AGENTES_POR_MAQUINA, MAX_PROJETOS, MAX_TERMINAIS = 16, 64, 64
LIMITE_HEARTBEAT = 256 * 1024
LIMITE_LOG = 5 * 1024 * 1024
SESSAO_VALE_S = 30 * 60      # a sessão vence 30 min depois do login ou da última RENOVAÇÃO explícita (atividade do usuário)
SESSAO_MAX_S = 8 * 3600      # teto absoluto: nem renovando a sessão passa disso
_FALHAS = {}  # {ip: [epoch das falhas]}
_NIVEL = {}   # {ip: (nível de reincidência, bloqueado até, epoch da última falha)}


def _norm_ip(ip):
    """IPv6 conta por /64 (um atacante tem milhões de endereços na mesma sub-rede); IPv4 por endereço."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(f"{a}/64", strict=False))
        return str(a)
    except ValueError:
        return str(ip)


def _falhas_recentes(ip, agora):
    ip = _norm_ip(ip)
    with LOCK:
        ts = [t for t in _FALHAS.get(ip, []) if agora - t < JANELA_FALHAS_S]
        if ts:
            _FALHAS[ip] = ts
        else:
            _FALHAS.pop(ip, None)  # nada de entrada vazia por IP que só fez um GET (a tabela crescia sem limite)
        return ts


def _bloqueado(ip, agora=None):
    agora = time.time() if agora is None else agora
    return len(_falhas_recentes(ip, agora)) > LIMITE_FALHAS or _bloqueio_restante(ip, agora) > 0


def _bloqueio_restante(ip, agora=None):
    agora = time.time() if agora is None else agora
    with LOCK:
        return max(0, _NIVEL.get(_norm_ip(ip), (0, 0, 0))[1] - agora)


def _registrar_falha(ip, agora=None):
    agora = time.time() if agora is None else agora
    ip = _norm_ip(ip)
    with LOCK:
        if len(_FALHAS) > 10000:  # IPv6 /64: sem poda a tabela cresceria sem limite
            for k in [k for k, v in _FALHAS.items() if not v or agora - v[-1] > JANELA_FALHAS_S]:
                del _FALHAS[k]
        ts = [t for t in _FALHAS.get(ip, []) if agora - t < JANELA_FALHAS_S]
        ts.append(agora)
        _FALHAS[ip] = ts
        if len(ts) <= LIMITE_FALHAS:
            return False
        nivel, ate, ultima = _NIVEL.get(ip, (0, 0, 0))
        if agora - ultima > BLOQUEIO_MAX_S:
            nivel = 0
        if ate <= agora:  # novo bloqueio: dobra a cada reincidência
            _NIVEL[ip] = (nivel + 1, agora + min(BLOQUEIO_S * 2 ** nivel, BLOQUEIO_MAX_S), agora)
            if len(_NIVEL) > 10000:
                for k in [k for k, v in _NIVEL.items() if agora - v[2] > BLOQUEIO_MAX_S]:
                    del _NIVEL[k]
        return True


def _limpar_falhas(ip):
    with LOCK:
        _FALHAS.pop(_norm_ip(ip), None)
        _NIVEL.pop(_norm_ip(ip), None)


def _is_localhost(ip):
    """Só loopback entra sem senha (dev/testes). Tailnet e internet exigem credencial."""
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_loopback
    except ValueError:
        return False


def token_maquina(cfg, nome):
    """Token de UMA máquina: nome.HMAC(PAINEL_TOKEN, nome). O PAINEL_TOKEN mestre nunca sai do painel; um token roubado
    só vale para o plano-agente e para a máquina dele (antes, um token único abria o painel inteiro)."""
    return nome + "." + hmac.new(cfg.token.encode(), nome.encode(), "sha256").hexdigest()


def _maquina_do_bearer(cfg, handler):
    """Nome da máquina dona do Bearer, ou "" se ausente/inválido."""
    nome, _, mac = _bearer(handler).rpartition(".")
    if not cfg.token or not nome or not NOME_OK.match(nome):
        return ""
    esperado = hmac.new(cfg.token.encode(), nome.encode(), "sha256").hexdigest()
    return nome if hmac.compare_digest(mac, esperado) else ""


def _via_proxy(handler):
    """Request que passou por um proxy: o IP de origem não é confiável, então nunca conta como loopback."""
    return any(handler.headers.get(h) for h in ("X-Forwarded-For", "Forwarded", "X-Real-IP"))


AUDITORIA_RUIDO = {"host-bloqueado", "origem-bloqueada", "login-falhou", "token-fora-do-escopo", "2fa-forca-bruta-pausado"}
PRAZO_CABECALHO_S = 10    # prazo TOTAL (não por byte) para o navegador mandar linha + headers: gotejar 1 byte/10 s não segura vaga
PRAZO_CORPO_S = 120       # depois dos headers: corpo (até 50 MB) e processamento. O relé de terminal não tem prazo (dura horas)
MAX_CONEXOES_POR_IP = 64  # por IPv4 ou /64 IPv6 (1 terminal aberto = 1 conexão): um atacante sozinho não esgota as vagas do painel
TOTP_ERROS_MAX, TOTP_JANELA_S = 5, 300   # 5 códigos errados com a senha certa em 5 min => 2FA pausado (e alerta)  # disparáveis sem login: 30/min no total


def auditar(cfg, handler, acao, alvo=""):
    """Uma linha JSON por ação sensível em dados/auditoria.log (sem segredos)."""
    if acao in AUDITORIA_RUIDO and not _quota_ok(cfg, cfg.aud_quota, acao, 30):  # anônimo não enche o disco pela internet
        with LOCK:
            cfg.aud_supr[acao] = cfg.aud_supr.get(acao, 0) + 1  # mas o que foi suprimido NÃO some: vira resumo na próxima linha
        return
    reg = {"t": int(time.time()), "ip": handler.client_address[0], "quem": getattr(handler, "_maquina", "") or "humano",
           "acao": acao, "alvo": str(alvo)[:120]}
    with LOCK:
        if cfg.aud_supr.get(acao):
            reg["suprimidas"] = cfg.aud_supr.pop(acao)
    try:
        with LOCK:
            _rotacionar(cfg.dados / "auditoria.log")
            with open(cfg.dados / "auditoria.log", "a") as f:
                f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except OSError:
        pass


def senha_fraca(senha):
    """Mensagem se a senha do painel é curta demais para ficar exposta ao brute-force (None se ok)."""
    if senha and len(senha) < 20:
        return "PAINEL_SENHA tem menos de 20 caracteres: gere uma longa (ex.: openssl rand -base64 24)"
    return None


def _bearer(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return (handler.headers.get("X-Painel-Token") or "").strip()


def nome_cookie(cfg):
    """Com TLS o cookie é __Host- (Secure, sem Domain, Path=/): não vaza para subdomínios nem é sobrescrito por eles."""
    return "__Host-Tessera" if getattr(cfg, "tls", False) else "Tessera"


def _cookie_sessao(cfg, handler):
    for parte in handler.headers.get("Cookie", "").split(";"):
        k, _, v = parte.strip().partition("=")
        if k.strip() == nome_cookie(cfg) and v.strip():
            return v.strip()
    return ""


def _ua(handler):
    return hashlib.sha256(handler.headers.get("User-Agent", "").encode()).hexdigest()[:16]


def _sessao_ok(cfg, handler, renovar=False):
    """Sessão válida? NÃO renova sozinha (polling não estende o login): só `renovar=True` (POST /api/sessao/renovar,
    chamado pelo front quando o usuário age) empurra o vencimento. Cookie roubado de outro navegador (User-Agent diferente) não vale."""
    tok = _cookie_sessao(cfg, handler)
    if not tok:
        return False
    agora = time.time()
    with LOCK:
        s = cfg.sessoes.get(tok)
        if s and s["exp"] > agora and s["max"] > agora and hmac.compare_digest(s["ua"], _ua(handler)):
            if renovar:
                s["exp"] = min(agora + SESSAO_VALE_S, s["max"])
            return True
        cfg.sessoes.pop(tok, None)
    return False


def _nova_sessao(cfg, handler=None):
    tok = secrets.token_urlsafe(32)
    agora = time.time()
    with LOCK:
        cfg.sessoes[tok] = {"exp": agora + SESSAO_VALE_S, "max": agora + SESSAO_MAX_S, "ua": _ua(handler) if handler else ""}
        if len(cfg.sessoes) > 5000:  # teto: expira as mais velhas
            for k in sorted(cfg.sessoes, key=lambda k: cfg.sessoes[k]["exp"])[:1000]:
                del cfg.sessoes[k]
    return tok


def encerrar_sessao(cfg, handler):
    """Logout: apaga a sessão E derruba os terminais (relés) abertos com ela. (Expirar por tempo NÃO derruba: é a regra do dono.)"""
    tok = _cookie_sessao(cfg, handler)
    with LOCK:
        cfg.sessoes.pop(tok, None)
        socks = cfg.reles.pop(tok, [])
    for cli, alvo in socks:
        for sk in (cli, alvo):
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------- 2º fator: TOTP (RFC 6238, app autenticador) ----------

def totp_codigo(segredo_b32, contador):
    chave = base64.b32decode(segredo_b32.replace(" ", "").upper() + "=" * (-len(segredo_b32.replace(" ", "")) % 8))
    h = hmac.new(chave, struct.pack(">Q", contador), "sha1").digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_confere(cfg, codigo, agora=None):
    """Aceita o passo atual e ±1 (relógios diferentes). Cada código só vale UMA vez (anti-replay): o passo usado e os anteriores
    ficam queimados. Sempre calcula os 3 candidatos (tempo constante). Não consome se a senha estiver errada (ver _login)."""
    if not cfg.totp_secret or not re.fullmatch(r"[0-9]{6}", codigo or ""):  # só ASCII: \d aceitaria dígitos Unicode e compare_digest estouraria
        return None
    passo = int((time.time() if agora is None else agora) // 30)
    achado = None
    for c in (passo - 1, passo, passo + 1):
        if hmac.compare_digest(totp_codigo(cfg.totp_secret, c), codigo) and c > cfg.totp_ultimo:
            achado = c
    return achado


def totp_novo_segredo():
    return base64.b32encode(secrets.token_bytes(20)).decode()  # 160 bits


def totp_uri(segredo, conta="admin", emissor="Tessera"):
    from urllib.parse import quote
    return f"otpauth://totp/{quote(emissor)}:{quote(conta)}?secret={segredo}&issuer={quote(emissor)}&algorithm=SHA1&digits=6&period=30"


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
        # loopback sem senha só para dev/testes (PAINEL_LOCALHOST_LIVRE=1): em produção qualquer processo da máquina
        # (ou um proxy reverso local) viraria "dono" do painel
        self.localhost_livre = os.environ.get("PAINEL_LOCALHOST_LIVRE") == "1"
        self.atras_proxy = os.environ.get("PAINEL_ATRAS_PROXY") == "1"
        # ÚNICA porta de entrada: https://<PAINEL_DOMINIO>. Qualquer outro Host (IP, localhost, outro nome) leva 421; vale também
        # para agentes e para o bootstrap. Em produção o painel nem sobe sem isso (ver main). Vazio só em dev/testes.
        self.dominio = os.environ.get("PAINEL_DOMINIO", "").strip().lower()
        self.sessoes = {}        # {sessao: {"exp", "max", "ua"}}
        # 2º fator (app autenticador). Em produção é obrigatório; sem ele (dev) o login continua sendo só a senha (Basic).
        self.totp_secret = os.environ.get("PAINEL_TOTP_SECRET", "").replace(" ", "")
        self.totp_ultimo = 0      # último passo TOTP aceito (anti-replay)
        try:
            self.totp_ultimo = int((self.dados / "totp_ultimo").read_text())
        except (OSError, ValueError):
            pass
        self.aud_quota = {}      # {acao: [epoch]}
        self.aud_supr = {}       # {acao: quantas linhas a quota de ruído suprimiu desde a última gravada}
        self.totp_erros = []     # epoch dos logins com SENHA CERTA e código errado (sinal de senha vazada: um anônimo não consegue disparar)
        self.reles = {}          # {sessao: [(socket do navegador, socket do agente)]}: o logout derruba os relés dela
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
        with LOCK:
            atual = self.notificacoes()
            atual.update({k: bool(v) for k, v in novos.items() if k in NOTIF_PADRAO})
            self.dados.mkdir(parents=True, exist_ok=True)
            tmp = self.arquivo_config.with_suffix(f".{threading.get_ident()}.tmp")
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
    sessao = re.sub(r"[^\w/.\-]", "", str(ev.get("sessao", "")))[:100]
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
    with LOCK:
        _rotacionar(cfg.eventos_log)
        with cfg.eventos_log.open("a") as f:
            f.write(json.dumps(registro_ev, ensure_ascii=False) + "\n")
    if not cfg.notificacoes().get(motivo, True):
        return 200, {"telegram": False, "silenciado": True}
    local = maquina + (f" · {projeto}/{terminal}" if projeto and terminal else (f" · {projeto}" if projeto else (f" · {sessao}" if sessao else "")))
    msg = f"{MOTIVOS[motivo]} {local}\n{motivo}" + (f": {texto}" if texto else "")
    if host:
        msg += f"\n{'https' if (cfg.dominio or cfg.tls) else 'http'}://{host}/#/{maquina}" + (f"/{projeto}/{terminal}" if projeto and terminal else "")
    if cfg.sincrono:
        enviado = enviar_telegram(cfg, msg)
    else:
        threading.Thread(target=enviar_telegram, args=(cfg, msg), daemon=True).start()
        enviado = bool(cfg.tg_token and cfg.tg_chat)
    return 200, {"telegram": enviado}


def _rotacionar(caminho, limite=LIMITE_LOG):
    """(com o LOCK) log que passou do limite vira .1 (o antigo .1 é descartado): nenhum log cresce sem teto."""
    try:
        if caminho.stat().st_size > limite:
            caminho.replace(caminho.with_name(caminho.name + ".1"))
    except FileNotFoundError:
        pass


def ultimos_eventos(cfg, n=20):
    """Só o fim do log (nada de reler o arquivo inteiro a cada poll); linha corrompida é ignorada."""
    try:
        with cfg.eventos_log.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            linhas = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except FileNotFoundError:
        return []
    saida = []
    for x in linhas:
        try:
            saida.append(json.loads(x))
        except ValueError:
            continue
    return saida


# ---------- agentes: heartbeat, tarefas, estado ----------

def _uniao_projetos(agentes, agora):
    """Projetos de todos os agentes de uma máquina. Em caso de nome repetido ganha o agente mais fresco
    (um agente parado não pode esconder o projeto de quem está respondendo)."""
    uniao = {}
    for ag in sorted(agentes, key=lambda a: (agora - agentes[a]["em"] > ONLINE_S, a)):
        for nome, p in agentes[ag]["payload"]["projetos"].items():
            uniao.setdefault(nome, {**p, "agente": ag})
    return uniao


def reescrever_primeiro_frame(buf, token):
    """O primeiro frame do navegador para o ttyd é o JSON {AuthToken, columns, rows}. Quem põe o AuthToken é o PAINEL (o segredo
    do ttyd nunca vai ao navegador). Devolve (frame_novo, resto) quando o frame inteiro chegou, None se ainda falta, ou
    (buf, b"") sem mexer se não for um frame de dados simples (o ttyd decide)."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    if not (b0 & 0x80) or (b0 & 0x0F) not in (1, 2) or not (b1 & 0x80):  # FIN, texto/binário, e cliente sempre mascara
        return bytes(buf), b""
    n, pos = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack(">Q", buf[2:10])[0], 10
    if n > 4096:  # o primeiro frame é um JSON minúsculo
        return bytes(buf), b""
    if len(buf) < pos + 4 + n:
        return None
    mascara, carga = buf[pos:pos + 4], buf[pos + 4:pos + 4 + n]
    resto = bytes(buf[pos + 4 + n:])
    try:
        obj = json.loads(bytes(c ^ mascara[i % 4] for i, c in enumerate(carga)))
        if not isinstance(obj, dict):
            raise ValueError
    except (ValueError, RecursionError):  # JSON de 1500 colchetes estoura a recursão do parser
        return bytes(buf[:pos + 4 + n]), resto
    obj["AuthToken"] = token
    novo = json.dumps(obj, separators=(",", ":")).encode()
    m = os.urandom(4)
    cab = bytes([0x80 | (b0 & 0x0F)])
    cab += bytes([0x80 | len(novo)]) if len(novo) < 126 else bytes([0x80 | 126]) + struct.pack(">H", len(novo))
    return cab + m + bytes(c ^ m[i % 4] for i, c in enumerate(novo)), resto


def cred_do_projeto(cfg, maquina, projeto):
    """Segredo do ttyd do projeto, vindo do heartbeat (fica só em memória; vazio se o agente ainda não mandou)."""
    for ag in cfg.vivo.get(maquina, {}).values():
        c = (ag["payload"].get("projetos") or {}).get(projeto, {}).get("cred", "")
        if c:
            return c
    return ""




def _num(v, lo, hi, tipo=float, padrao=0):
    """Número finito dentro de [lo, hi]; qualquer lixo (str, lista, NaN, inf, 1e999) vira o padrão. Heartbeat é dado de agente
    (token roubado = dado hostil) e vai direto para o /api/estado dos humanos."""
    try:
        x = tipo(v)
    except (TypeError, ValueError, OverflowError):
        return padrao
    if isinstance(x, float) and not math.isfinite(x):
        return padrao
    return max(lo, min(hi, x))


def _terminal_limpo(v):
    cmd = v.get("cmd")
    return {"cmd": cmd[:64] if isinstance(cmd, str) else "", "cpu": _num(v.get("cpu"), 0, 100000),
            "mem_mb": _num(v.get("mem_mb"), 0, 10**7, int), "atividade_s": _num(v.get("atividade_s"), 0, 10**9, int, 999)}


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
    for nome, p in list(projetos.items())[:MAX_PROJETOS]:
        if not isinstance(nome, str) or not PROJ_OK.match(nome) or not isinstance(p, dict):
            continue
        brutos = p.get("terminais") if isinstance(p.get("terminais"), dict) else {}
        terminais = {t: _terminal_limpo(v) for t, v in list(brutos.items())[:MAX_TERMINAIS]
                     if isinstance(t, str) and TERM_OK.match(t) and isinstance(v, dict)}
        cred = p.get("cred")
        limpos[nome] = {
            "dir": str(p.get("dir", ""))[:300], "usuario": str(p.get("usuario", ""))[:32], "porta": (lambda x: x if x >= 1024 else 0)(_num(p.get("porta"), 0, 65535, int, 0)),  # <1024 = lixo/SSRF a serviços do sistema
            "cred": cred if isinstance(cred, str) and CRED_OK.match(cred) else "",
            "branch": str(p.get("branch", ""))[:80], "cpu": _num(p.get("cpu"), 0, 100000), "mem_mb": _num(p.get("mem_mb"), 0, 10**7, int),
            "terminais": terminais,
        }
    ferr = payload.get("ferramentas") if isinstance(payload.get("ferramentas"), dict) else {}
    vivo = {"cores": _num(payload.get("cores"), 0, 100000, int), "ram_mb": _num(payload.get("ram_mb"), 0, 10**8, int),
            "mem_mb": _num(payload.get("mem_mb"), 0, 10**8, int), "cpu": _num(payload.get("cpu"), 0, 100000),
            "shell": str(payload.get("shell", ""))[:20], "projetos": limpos,
            "ferramentas": {str(k): bool(v) for k, v in list(ferr.items())[:50] if isinstance(k, str) and FERRAMENTA_OK.match(k)}}
    with LOCK:
        agentes = cfg.vivo.setdefault(maquina, {})
        if agente not in agentes and len(agentes) >= MAX_AGENTES_POR_MAQUINA:  # um token não pode inflar a memória com agentes inventados
            return {"tarefas": []}
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
LOGIN_PUBLICO = {"/login", "/static/login.js", "/static/login.css"}   # a tela de login precisa carregar antes de haver sessão


def ip_isento(ip):
    """True para localhost e rede Tailscale (entram sem senha). O resto (IP público) exige PAINEL_SENHA."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
        return (addr.is_loopback
                or addr in ipaddress.ip_network("100.64.0.0/10")      # Tailscale IPv4 (CGNAT)
                or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"))  # Tailscale IPv6
    except ValueError:
        return False


def da_tailnet(ip):
    """IP dentro da faixa da Tailscale. A tailnet é só o canal interno painel -> ttyd das máquinas: NADA entra no painel por ela."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
        if addr.version == 6 and addr.ipv4_mapped:  # ::ffff:100.64.0.1 é o mesmo par
            addr = addr.ipv4_mapped
        return addr in ipaddress.ip_network("100.64.0.0/10") or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48")
    except ValueError:
        return False


def ip_de_agente_ok(cfg, ip):
    """Onde o painel aceita alcançar um ttyd (relé): em produção SÓ a tailnet (loopback do próprio painel e a internet, não),
    senão um token de máquina registraria 127.0.0.1:<porta> e o relé viraria SSRF para os serviços locais."""
    if not cfg.dominio:
        return ip_isento(ip)
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
        return addr in ipaddress.ip_network("100.64.0.0/10") or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48")
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


def _guardado(f):
    """Nenhuma exceção vira conexão muda com traceback: resposta 500 limpa (ou fecha, se o cliente já foi embora)."""
    def g(self):
        try:
            f(self)
        except OSError:  # cliente sumiu / handshake TLS / timeout
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            print(f"erro interno em {self.command} {urlparse(self.path).path}: {type(e).__name__}", file=sys.stderr, flush=True)
            self.close_connection = True
            try:
                self._json(500, {"erro": "interno"})
            except OSError:
                pass
    return g


class Servidor(ThreadingHTTPServer):
    """ThreadingHTTPServer com fila maior e tetos de conexões simultâneas: global e POR IP (/64 em IPv6). Acima do teto a conexão
    é fechada na hora (não vira thread). A vaga volta SEMPRE: fim normal, exceção ou falha ao criar a thread."""
    request_queue_size = 128
    daemon_threads = True
    MAX_CONEXOES = 256
    MAX_POR_IP = MAX_CONEXOES_POR_IP

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._vagas = threading.BoundedSemaphore(self.MAX_CONEXOES)
        self._por_ip = {}
        self._trava = threading.Lock()

    def _liberar(self, chave):
        with self._trava:
            n = self._por_ip.get(chave, 0) - 1
            if n > 0:
                self._por_ip[chave] = n
            else:
                self._por_ip.pop(chave, None)
        self._vagas.release()

    def process_request(self, request, client_address):
        chave = _norm_ip(client_address[0])
        with self._trava:
            if self._por_ip.get(chave, 0) >= self.MAX_POR_IP:
                self.shutdown_request(request)
                return
            if not self._vagas.acquire(blocking=False):
                self.shutdown_request(request)
                return
            self._por_ip[chave] = self._por_ip.get(chave, 0) + 1
        try:
            super().process_request(request, client_address)
        except BaseException:  # ex.: "can't start new thread": sem isto a vaga vazava
            self._liberar(chave)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._liberar(_norm_ip(client_address[0]))

    def handle_error(self, request, client_address):
        """Conexão cortada pelo prazo, cliente que some ou lixo no handshake: uma linha só (o traceback por conexão deixava um
        anônimo inflar o journal). Erro de verdade (bug nosso) continua com traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (OSError, ValueError)):  # inclui BrokenPipe, ssl.SSLError, timeout
            return
        super().handle_error(request, client_address)


def criar_handler(registro, cfg):
    class Handler(BaseHTTPRequestHandler):
        _maquina = ""
        timeout = 30  # por operação de socket (o relé usa select, não é afetado)
        _vigia = None

        def _armar(self, segundos):
            """Prazo TOTAL da requisição: passou, o socket é derrubado (o timeout por operação não pega gotejamento)."""
            if self._vigia:
                self._vigia.cancel()
            self._vigia = None
            if segundos:
                self._vigia = threading.Timer(segundos, self._matar)
                self._vigia.daemon = True
                self._vigia.start()

        def _matar(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def handle_one_request(self):
            self._armar(PRAZO_CABECALHO_S)  # cobre também o handshake TLS (que acontece na 1ª leitura)
            try:
                super().handle_one_request()
            finally:
                self._armar(None)

        def parse_request(self):
            ok = super().parse_request()
            if ok:
                try:
                    tam = max(0, int(self.headers.get("Content-Length", 0)))
                except ValueError:
                    tam = 0
                self._armar(PRAZO_CORPO_S + tam / 100000)  # headers chegaram: corpo + processamento; upload grande ganha tempo (~0,8 Mbit/s mínimo)
            return ok

        def version_string(self):
            return "Tessera"

        def _negar(self, sem_senha):
            # Tailscale/localhost: nunca chega aqui. IP público sem PAINEL_SENHA: 403 fechado.
            # IP público com senha mas sem credencial: 401 (o navegador mostra o prompt de login).
            if sem_senha:
                self._json(403, {"erro": "painel fechado para a internet (sem PAINEL_SENHA no servidor)"})
                return
            ip = self.client_address[0]
            auditar(cfg, self, "login-falhou")
            if _registrar_falha(ip):
                self._muitas_tentativas()
                return
            time.sleep(0.5)  # freio contra brute-force na senha
            self._responder(401, json.dumps({"erro": "senha do painel"}),
                            extra={"WWW-Authenticate": 'Basic realm="Tessera"', "Cache-Control": "no-store"})

        def _muitas_tentativas(self):
            espera = int(_bloqueio_restante(self.client_address[0])) + 1 if _bloqueio_restante(self.client_address[0]) else BLOQUEIO_S
            self._responder(429, json.dumps({"erro": "muitas tentativas, tente mais tarde"}),
                            extra={"Cache-Control": "no-store", "Retry-After": str(espera)})

        def _pre(self, mutante):
            """Host e Origin: DNS-rebinding e CSRF/CSWSH (o navegador sempre manda Origin em POST/PUT/DELETE e em WebSocket).
            Cliente sem Origin (curl, agentes) passa; com Origin de outro host, não."""
            # blindagem da tailnet: conexão vinda de um par da tailnet OU que chegou no IP da tailnet deste servidor é descartada
            # sem resposta (nem 4xx), antes de qualquer rota ou autenticação. O painel só existe em https://<domínio>.
            if cfg.atras_proxy:
                # só o proxy local fala com o painel: conexão de fora do loopback, ou sem o cabeçalho que o proxy sempre põe, é
                # descartada sem resposta. Com o proxy, o IP de verdade é o ÚLTIMO do X-Forwarded-For (o que o proxy anexou).
                xff = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
                try:
                    real = str(ipaddress.ip_address(xff))
                except ValueError:
                    real = ""
                if not real or not _is_localhost(self.request.getpeername()[0]):  # par do SOCKET: client_address já pode ter sido reescrito (keep-alive)
                    self.close_connection = True
                    return False
                self.client_address = (real, self.client_address[1])
            if da_tailnet(self.client_address[0]) or da_tailnet(self.connection.getsockname()[0]):
                self.close_connection = True
                return False
            host = self.headers.get("Host", "")
            if cfg.dominio and host.strip().lower() != cfg.dominio:  # sem isenção nem para loopback
                auditar(cfg, self, "host-bloqueado", host)
                self._json(421, {"erro": "host não permitido"})
                return False
            origem = self.headers.get("Origin")
            esperado = f"{'https' if getattr(cfg, 'tls', False) else 'http'}://{host}"
            if origem is not None and (mutante or urlparse(self.path).path.startswith("/tty/")) \
                    and origem.rstrip("/").lower() != esperado.lower():
                auditar(cfg, self, "origem-bloqueada", origem)
                self._json(403, {"erro": "origem não permitida"})
                return False
            return True

        def _dono_ok(self, maquina):
            """Token de máquina só age em nome da própria máquina."""
            if self._maquina and self._maquina != maquina:
                self._json(403, {"erro": "token de outra máquina"})
                return False
            return True

        def _autenticado(self):
            self._maquina = ""
            if cfg.localhost_livre and not _via_proxy(self) and _is_localhost(self.client_address[0]):
                return True
            if _sessao_ok(cfg, self):
                return True
            maq = _maquina_do_bearer(cfg, self)
            if maq:
                caminho = urlparse(self.path).path
                if _plano_agente(caminho) or (self.command == "GET" and caminho.startswith("/api/config/")):
                    self._maquina = maq
                    return True
                auditar(cfg, self, "token-fora-do-escopo", caminho)
                self._json(403, {"erro": "token de máquina não vale nesta rota"})
                return False
            if _bloqueado(self.client_address[0]):
                self._muitas_tentativas()
                return False
            if cfg.totp_secret:  # sem sessão: 401 simples (não conta como tentativa; só /api/login errado conta)
                self._responder(401, json.dumps({"erro": "login"}), extra={"Cache-Control": "no-store"})
                return False
            if not cfg.senha and not cfg.token:
                self._negar(sem_senha=True)
                return False
            # com 2FA configurado o Basic (senha sozinha, que o navegador reenvia sem ser pedido) NÃO existe: só /api/login
            if not cfg.totp_secret and cfg.senha and senha_confere(self.headers.get("Authorization"), cfg.senha):
                _limpar_falhas(self.client_address[0])
                self._sessao_nova = _nova_sessao(cfg, self)
                auditar(cfg, self, "login")
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
            seguro = "; Secure" if getattr(cfg, "tls", False) else ""
            if getattr(self, "_sessao_nova", None):
                self.send_header("Set-Cookie", f"{nome_cookie(cfg)}={self._sessao_nova}; HttpOnly; SameSite=Strict{seguro}; Path=/")  # cookie de sessão: o prazo é do servidor
                self._sessao_nova = None
            if getattr(self, "_expirar_cookie", False):
                self.send_header("Set-Cookie", f"{nome_cookie(cfg)}=; HttpOnly; SameSite=Strict{seguro}; Path=/; Max-Age=0")
                self._expirar_cookie = False
            self.end_headers()
            self.wfile.write(dados)

        def _json(self, codigo, obj):
            self._responder(codigo, json.dumps(obj, ensure_ascii=False, allow_nan=False), extra={"Cache-Control": "no-store"})

        def _nao_suportado(self):
            if not self._pre(True) or not self._autenticado():
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
            if tam < 0 or tam > limite:
                raise ValueError("corpo grande demais")
            return self.rfile.read(tam)

        def _corpo_json(self, limite=1024 * 1024):
            corpo = json.loads(self._corpo(limite) or b"{}")
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

        @_guardado
        def do_GET(self):
            url = urlparse(self.path)
            caminho = url.path
            if not self._pre(False):
                return
            if caminho in LOGIN_PUBLICO or (caminho == "/" and cfg.totp_secret and not _sessao_ok(cfg, self)):
                return self._estatico({"/": "login.html", "/login": "login.html"}.get(caminho, caminho[len("/static/"):]))
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
                try:
                    n = int(parse_qs(url.query).get("n", ["20"])[0])
                except ValueError:
                    n = 20
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
            if urlparse(resto).path != "/ws":  # só o WebSocket do terminal: /token devolveria o CRED do ttyd, / a página dele
                return self._json(404, {"erro": "não encontrado"})
            _q = parse_qs(urlparse(resto).query)
            for _v in _q.get("arg", []):
                if not TERM_OK.match(_v):
                    return self._json(400, {"erro": "arg inválido"})
            if self.command != "GET":
                return self._json(405, {"erro": "só GET"})
            self._armar(None)  # terminal dura horas: sem prazo total (o logout derruba)
            m = registro.listar().get(maquina)
            porta = ((m.get("projetos") or {}).get(projeto) or {}).get("porta") if m else 0
            if not m or not porta:
                return self._json(404, {"erro": "máquina ou projeto desconhecido (sem ttyd?)"})
            if not ip_de_agente_ok(cfg, m.get("ip", "")):
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
                cred = cred_do_projeto(cfg, maquina, projeto)
                if cred:  # o ttyd roda com -c: o painel prova que o cliente já passou pela senha do painel
                    pedido.append("Authorization: Basic " + base64.b64encode(f"tessera:{cred}".encode()).decode())
                pedido.append(f"Host: {m['ip']}:{porta}")
                pedido.append(f"Origin: http://{m['ip']}:{porta}")
                pedido.append(f"X-Forwarded-For: {self.client_address[0]}")
                alvo.sendall(("\r\n".join(pedido) + "\r\n\r\n").encode("latin-1"))
                self.close_connection = True
                cli = self.connection
                tok_sessao = _cookie_sessao(cfg, self)
                with LOCK:
                    if tok_sessao and tok_sessao not in cfg.sessoes:  # logout concorrente entre a autenticação e este registro
                        return
                    cfg.reles.setdefault(tok_sessao, []).append((cli, alvo))  # o logout derruba estes sockets
                token_ws = base64.b64encode(f"tessera:{cred}".encode()).decode() if cred else None
                pendente = bytearray() if (token_ws and resto.startswith("/ws")) else None  # 1º frame do navegador ainda não passou
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
                        if origem is cli and pendente is not None:
                            pendente += dados
                            r = reescrever_primeiro_frame(pendente, token_ws)
                            if r is None:
                                if len(pendente) > 8192:
                                    return
                                continue
                            dados, resto_b = r[0] + r[1], None
                            pendente = None
                        try:
                            destino.sendall(dados)
                        except OSError:
                            return
            except (OSError, ValueError):
                return
            finally:
                with LOCK:
                    lista = cfg.reles.get(_cookie_sessao(cfg, self), [])
                    if (self.connection, alvo) in lista:
                        lista.remove((self.connection, alvo))
                try:
                    alvo.close()
                except OSError:
                    pass

        def _login(self):
            ip = self.client_address[0]
            if not cfg.totp_secret or not cfg.senha:
                return self._json(404, {"erro": "não encontrado"})
            if _bloqueado(ip):  # só o IP que erra é bloqueado: um anônimo nunca trava o login do dono (vindo de outro IP)
                return self._muitas_tentativas()
            try:
                corpo = self._corpo_json()
            except (ValueError, TypeError):
                return self._json(400, {"erro": "requisição inválida"})
            senha, codigo = str(corpo.get("senha", "")), str(corpo.get("codigo", "")).strip()
            ok_senha = hmac.compare_digest(senha.encode(), cfg.senha.encode())
            agora = time.time()
            with LOCK:
                cfg.totp_erros = [t for t in cfg.totp_erros if agora - t < TOTP_JANELA_S]
                pausado = len(cfg.totp_erros) >= TOTP_ERROS_MAX
            if pausado:
                # Só "senha certa + código errado" alimenta o contador (anônimo não consegue travar o dono). Pausado, TODO login leva o mesmo
                # 429, com senha certa ou errada e sem olhar o corpo: nenhum oráculo de senha (nem de tempo).
                auditar(cfg, self, "2fa-forca-bruta-pausado")
                return self._muitas_tentativas()
            with LOCK:  # confere E queima o código na mesma seção crítica: 30 logins paralelos com o mesmo código dão 1 sucesso
                passo = totp_confere(cfg, codigo)  # sempre avalia os dois: o erro não diz qual falhou
                sucesso = ok_senha and passo is not None
                if sucesso:
                    cfg.totp_ultimo = passo
                    cfg.totp_erros.clear()
                elif ok_senha:
                    cfg.totp_erros.append(agora)
                    alerta = len(cfg.totp_erros) == TOTP_ERROS_MAX
                else:
                    alerta = False
            if not sucesso and ok_senha and alerta:
                auditar(cfg, self, "2fa-forca-bruta")
                msg = "⚠️ Tessera: 5 códigos 2FA errados com a SENHA CORRETA em 5 min. A senha pode ter vazado: troque-a. Logins pausados por 5 min."
                if cfg.sincrono:
                    enviar_telegram(cfg, msg)
                else:
                    threading.Thread(target=enviar_telegram, args=(cfg, msg), daemon=True).start()
            if sucesso:
                try:
                    (cfg.dados / "totp_ultimo").write_text(str(passo))  # sobrevive ao restart: o código usado não vale de novo
                except OSError:
                    pass
                _limpar_falhas(ip)
                self._sessao_nova = _nova_sessao(cfg, self)
                auditar(cfg, self, "login")
                return self._json(200, {"ok": True, "expira_em": SESSAO_VALE_S})
            auditar(cfg, self, "login-falhou")
            _registrar_falha(ip)
            time.sleep(0.5)
            self._json(401, {"erro": "credenciais inválidas"})

        @_guardado
        def do_POST(self):
            caminho = urlparse(self.path).path
            if caminho == "/api/login":
                if self._pre(True):
                    self._login()
                return
            if not self._pre(True) or not self._autenticado():
                return
            if caminho == "/api/logout":
                encerrar_sessao(cfg, self)
                auditar(cfg, self, "logout")
                self._expirar_cookie = True
                return self._json(200, {"ok": True})
            if caminho == "/api/sessao/renovar":  # o front chama quando o usuário age; polling NÃO renova
                if not _sessao_ok(cfg, self, renovar=True):
                    return self._json(401, {"erro": "login"})
                return self._json(200, {"ok": True, "expira_em": SESSAO_VALE_S})
            try:
                if caminho == "/api/registrar":
                    return self._registrar()
                if caminho == "/api/evento":
                    ev = self._corpo_json()
                    if not self._dono_ok(str(ev.get("maquina", "")).strip()):
                        return
                    codigo, resp = tratar_evento(cfg, registro, ev, self.headers.get("Host", ""))
                    return self._json(codigo, resp)
                if caminho == "/api/arquivo":
                    return self._arquivo(urlparse(self.path).query)
                if caminho == "/api/convite":
                    return self._convite()
                if caminho == "/api/tarefas":
                    corpo = self._corpo_json()
                    codigo, resp = criar_tarefa(cfg, registro, corpo)
                    auditar(cfg, self, "tarefa", f"{corpo.get('maquina')}:{corpo.get('tipo')} -> {codigo}")
                    return self._json(codigo, resp)
                if caminho.startswith("/api/agente/") and caminho.endswith("/tarefas"):
                    maquina = caminho[len("/api/agente/"):-len("/tarefas")]
                    if not NOME_OK.match(maquina):
                        return self._json(400, {"erro": "nome inválido"})
                    if not self._dono_ok(maquina):
                        return
                    resp = consultar_tarefas(cfg, registro, maquina, self._corpo_json())
                    return self._json(200, resp) if resp is not None else self._json(404, {"erro": "máquina não registrada"})
                if caminho.startswith("/api/agente/"):
                    maquina = caminho.rsplit("/", 1)[1]
                    if not NOME_OK.match(maquina):
                        return self._json(400, {"erro": "nome inválido"})
                    if not self._dono_ok(maquina):
                        return
                    resp = receber_heartbeat(cfg, registro, maquina, self._corpo_json(LIMITE_HEARTBEAT))
                    return self._json(200, resp) if resp is not None else self._json(404, {"erro": "máquina não registrada"})
                if caminho.startswith("/api/config/"):
                    return self._guardar_config(caminho.rsplit("/", 1)[1])
            except (ValueError, TypeError, KeyError, AttributeError):
                return self._json(400, {"erro": "requisição inválida"})
            self._json(404, {"erro": "não encontrado"})

        @_guardado
        def do_PUT(self):
            if not self._pre(True) or not self._autenticado():
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
            if not self._dono_ok(maquina):
                return
            if not _quota_ok(cfg, cfg.arq_quota, maquina, LIMITE_ARQUIVOS_MIN):  # ANTES de ler o corpo: N conexões não seguram N x 50 MB
                return self._json(429, {"erro": "muitos arquivos, tente mais tarde"})
            try:
                dados = self._corpo(LIMITE_ARQUIVO)
            except ValueError:
                return self._json(413, {"erro": "arquivo maior que 50 MB"})
            if not dados:
                return self._json(400, {"erro": "arquivo vazio"})
            nome = q.get("nome", "arquivo")
            local = maquina + (f" · {projeto}/{terminal}" if projeto and terminal else (f" · {projeto}" if projeto else ""))
            legenda = f"📎 {local}" + (f"\n{q['legenda'][:500]}" if q.get("legenda") else "")
            self._json(200, {"telegram": enviar_telegram_arquivo(cfg, nome, dados, legenda)})

        def _registrar(self):
            corpo = self._corpo_json()
            nome, ip, porta = str(corpo["nome"]).strip(), str(corpo["ip"]).strip(), int(corpo["porta"])
            if not NOME_OK.match(nome) or not 0 < porta < 65536:
                return self._json(400, {"erro": "nome ou porta inválidos"})
            if not self._dono_ok(nome):
                return
            try:
                a = ipaddress.ip_address(ip)
                if getattr(a, "scope_id", None):
                    raise ValueError("zona")
                ip = str(a)  # sem sufixo de zona (%...): o valor vai para um header e para o disco
            except ValueError:
                return self._json(400, {"erro": "ip inválido"})
            if cfg.dominio and not ip_de_agente_ok(cfg, ip):  # produção: só IP da tailnet (dev/testes registram IPs de exemplo)
                return self._json(400, {"erro": "ip fora da tailnet"})
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
            if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?|\[[0-9a-fA-F:]+\](:\d+)?", self.headers.get("Host", "")):
                return self._json(400, {"erro": "Host inválido"})  # o comando do convite leva o token: não ecoa Host arbitrário
            ts = ip_tailnet()  # IP do painel na tailnet: a máquina só libera os ttyd para ele (--painel-ip)
            if cfg.dominio:  # entrada única: instalador e agente falam com https://<domínio> (nunca IP, nunca o Host da requisição)
                origem = painel = f"https://{cfg.dominio}"
            else:  # dev/testes
                painel = f"{esquema}://{ts}:{porta}" if ts else f"{esquema}://{self.headers.get('Host', '')}"
                # o instalador é baixado ANTES de entrar na tailnet: usa o IP público (ou a página atual)
                origem = f"{esquema}://{cfg.publico}:{porta}" if cfg.publico else f"{esquema}://{self.headers.get('Host', '')}"
            tok_env = f"TTY_TOKEN={token_maquina(cfg, nome)} " if getattr(cfg, "token", "") else ""  # env (não argv): fora do ps do bash
            extra_tok = f" --painel-ip {ts}" if ts and da_tailnet(ts) else ""
            auditar(cfg, self, "convite", nome)
            try:
                sha = hashlib.sha256((cfg.raiz / "bootstrap" / "bootstrap.sh").read_bytes()).hexdigest()
            except OSError:
                return self._json(500, {"erro": "bootstrap.sh ausente no painel"})
            # o instalador vem por HTTP (a máquina nova ainda não tem tailnet) e roda como root: o hash, que chega pelo
            # painel autenticado, impede que alguém no caminho troque o script (MITM) — sem isso era RCE por curl|bash
            baixar = f'b=$(mktemp) && curl -fsSL {origem}/bootstrap.sh -o "$b" && echo "{sha}  $b" | sha256sum -c - >/dev/null'
            if corpo.get("sem_root"):  # usuário comum de uma máquina que já está na tailnet: sem sudo, sem auth key
                return self._json(200, {"comando": f'{baixar} && {tok_env}bash "$b" --nome {nome} --painel {painel}{extra_tok}'})
            try:
                key, _ = criar_authkey(cfg, nome)
            except (RuntimeError, OSError, ValueError, KeyError) as e:
                return self._json(502, {"erro": f"não consegui gerar a auth key: {e}"})
            if not re.fullmatch(r"tskey-[A-Za-z0-9_-]+", str(key)):
                return self._json(502, {"erro": "auth key inesperada da API da Tailscale"})  # vai interpolada num comando de shell
            exporta = f"export TS_AUTHKEY={key}{' ' + tok_env.strip() if tok_env else ''}"
            preserva = "TS_AUTHKEY" + (",TTY_TOKEN" if tok_env else "")
            # segredos exportados no shell de quem cola o comando e repassados com --preserve-env: ficam fora do argv do sudo (ps)
            comando = f'{baixar} && {exporta} && sudo --preserve-env={preserva} bash "$b" --nome {nome} --painel {painel}{extra_tok}; unset TS_AUTHKEY{" TTY_TOKEN" if tok_env else ""}'
            self._json(200, {"comando": comando})

        def _guardar_config(self, nome):
            if not NOME_OK.match(nome):
                return self._json(400, {"erro": "nome inválido"})
            auditar(cfg, self, "config-guardada", nome)
            dados = self._corpo(LIMITE_CONFIG)
            cfg.configs.mkdir(parents=True, exist_ok=True)
            destino = cfg.configs / f"{nome}.tar.gz"
            tmp = destino.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(dados)
            tmp.replace(destino)
            self._json(200, {"guardado": nome, "bytes": len(dados)})

        @_guardado
        def do_DELETE(self):
            if not self._pre(True) or not self._autenticado():
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
            auditar(cfg, self, "maquina-removida", nome)
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


def entrada_insegura(cfg, tem_tls, bind="127.0.0.1"):
    """Produção = só https://<domínio>. Sem domínio ou sem TLS o painel não sobe (None se ok; dev/testes isentos)."""
    if cfg.localhost_livre:
        return None
    if not cfg.dominio:
        return "defina PAINEL_DOMINIO (único Host aceito, ex.: painel.exemplo.com)"
    if getattr(cfg, "atras_proxy", False) and bind not in ("127.0.0.1", "::1"):
        return "PAINEL_ATRAS_PROXY=1 exige PAINEL_BIND=127.0.0.1: o painel nunca atende IP:porta, só o proxy local"
    if not tem_tls:
        return "defina PAINEL_TLS_CERT e PAINEL_TLS_KEY: o painel só atende HTTPS"
    if not cfg.totp_secret:
        return "defina PAINEL_TOTP_SECRET (2º fator, app autenticador). Gere com: python3 painel.py --totp-novo"
    try:
        base64.b32decode(cfg.totp_secret.upper() + "=" * (-len(cfg.totp_secret) % 8))
    except ValueError:
        return "PAINEL_TOTP_SECRET inválido (base32); gere com: python3 painel.py --totp-novo"
    if not cfg.senha:
        return "defina PAINEL_SENHA (20+ caracteres)"
    if getattr(cfg, "token", "") and len(cfg.token) < 32:
        return "PAINEL_TOKEN com menos de 32 caracteres: os tokens das máquinas derivam dele (ex.: openssl rand -hex 32)"
    return None


def avisos_de_seguranca(bind, tls, token, senha, dominio=""):
    """Alertas de configuração insegura, impressos no start (ex.: tudo em 0.0.0.0 sem proteção)."""
    av = []
    if bind in ("0.0.0.0", "::", "") and not tls:
        av.append("escutando em endereço curinga SEM TLS: senha e terminais trafegam em claro na internet "
                  "(defina PAINEL_TLS_CERT/KEY)")
    if not token:
        av.append("sem PAINEL_TOKEN: nenhum agente remoto consegue falar com o painel (todos recebem 401)")
    if not dominio:
        av.append("sem PAINEL_DOMINIO: o painel aceita qualquer Host (só dev/testes; em produção ele nem sobe assim)")
    if not senha:
        av.append("sem PAINEL_SENHA: a internet fica bloqueada e só localhost entra")
    return av


def main():
    if sys.argv[1:2] == ["--totp-novo"]:  # gera o segredo do 2º fator e o QR/URI para o app autenticador
        seg = totp_novo_segredo()
        print(f"PAINEL_TOTP_SECRET={seg}\n\nAdicione ao app autenticador (URI): {totp_uri(seg)}")
        try:
            subprocess.run(["qrencode", "-t", "ANSIUTF8", totp_uri(seg)], check=False)
        except OSError:
            print("(instale 'qrencode' para ver o QR no terminal; ou cole o segredo no app)")
        return
    carregar_env(AQUI / ".env")
    # produção: escuta na interface pública (TLS + domínio + login, e a tailnet é descartada no código); dev: só loopback
    porta = int(os.environ.get("PAINEL_PORTA", 8787))
    dados = Path(os.environ.get("PAINEL_DADOS", AQUI / "dados"))
    cfg = Config(dados)
    bind = os.environ.get("PAINEL_BIND") or ("127.0.0.1" if cfg.localhost_livre else "0.0.0.0")
    erro_senha = senha_fraca(cfg.senha)
    if erro_senha:
        sys.exit(f"ERRO: {erro_senha}")
    erro_entrada = entrada_insegura(cfg, bool(os.environ.get("PAINEL_TLS_CERT") and os.environ.get("PAINEL_TLS_KEY")), bind)
    if erro_entrada:
        sys.exit(f"ERRO: {erro_entrada}")
    registro = Registro(dados / "maquinas.json")
    servidor = Servidor((bind, porta), criar_handler(registro, cfg))
    cert, chave = os.environ.get("PAINEL_TLS_CERT", ""), os.environ.get("PAINEL_TLS_KEY", "")
    if cert and chave:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, chave)
        # handshake só na thread da conexão (com timeout): no accept() um cliente parado travaria o painel inteiro
        servidor.socket = ctx.wrap_socket(servidor.socket, server_side=True, do_handshake_on_connect=False)
        cfg.tls = True
    for a in avisos_de_seguranca(bind, cfg.tls, cfg.token, cfg.senha, cfg.dominio):
        print(f"AVISO: {a}", file=sys.stderr, flush=True)
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
