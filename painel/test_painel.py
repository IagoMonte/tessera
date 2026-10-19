import io
import json
import socket
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import painel


def servir(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Falso(BaseHTTPRequestHandler):
    """Servidor falso para Telegram e API da Tailscale: guarda o que recebeu."""

    recebidos = []

    def _tratar(self):
        tam = int(self.headers.get("Content-Length", 0))
        corpo = self.rfile.read(tam)
        Falso.recebidos.append((self.command, self.path, self.headers.get("Authorization"), corpo))
        resp = json.dumps({"key": "tskey-auth-FALSA", "id": "kid1", "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    do_POST = do_DELETE = _tratar

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        Falso.recebidos = []
        self.tmp = tempfile.TemporaryDirectory()
        dados = Path(self.tmp.name)
        self.falso, self.url_falso = servir(Falso)
        self.cfg = painel.Config(
            dados,
            ts_api_key="tskey-api-teste",
            ts_api_base=self.url_falso,
            tg_token="TOK",
            tg_chat="42",
            tg_api=self.url_falso,
            sincrono=True,
            raiz=Path(self.tmp.name) / "raiz",
        )
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            (self.cfg.raiz / pasta).mkdir(parents=True)
        (self.cfg.raiz / "bootstrap" / "bootstrap.sh").write_text("#!/bin/bash\necho oi\n")
        (self.cfg.raiz / "notificador" / "tty-notify").write_text("x")
        (self.cfg.raiz / "notificador" / "test_x.sh").write_text("nao deve ir")
        (self.cfg.raiz / "projetos" / "tty-projeto").write_text("p")
        (self.cfg.raiz / "agente" / "agente.py").write_text("a")
        registro = painel.Registro(dados / "m.json")
        self.registro = registro
        self.srv, self.base = servir(painel.criar_handler(registro, self.cfg))

    def tearDown(self):
        for s in (self.srv, self.falso):
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    def chamar(self, metodo, caminho, corpo=None, headers=None):
        dados = corpo if isinstance(corpo, bytes) else (json.dumps(corpo).encode() if corpo is not None else None)
        req = urllib.request.Request(self.base + caminho, method=metodo, data=dados, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, *a, **k):
        codigo, corpo = self.chamar(*a, **k)
        return codigo, json.loads(corpo)


class TestRegistro(Base):
    def test_vazio(self):
        self.assertEqual(self.json("GET", "/api/maquinas"), (200, {}))

    def test_registrar_atualizar_listar(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.2", "porta": 7682})
        _, dados = self.json("GET", "/api/maquinas")
        self.assertEqual(len(dados), 1)
        self.assertEqual(dados["pc"]["ip"], "100.1.1.2")

    def test_registro_invalido(self):
        for corpo in (
            {"nome": "pc"},
            {"nome": "a/b", "ip": "1.1.1.1", "porta": 1},
            {"nome": "pc", "ip": "1.1.1.1", "porta": 70000},
        ):
            self.assertEqual(self.chamar("POST", "/api/registrar", corpo)[0], 400)

    def test_remover(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        self.assertEqual(self.chamar("DELETE", "/api/maquinas/pc")[0], 200)
        self.assertEqual(self.chamar("DELETE", "/api/maquinas/pc")[0], 404)


class TestEventos(Base):
    def evento(self, **extra):
        ev = {"maquina": "pc", "projeto": "api", "terminal": "main", "motivo": "aprovacao", "texto": "Allow Bash?"}
        ev.update(extra)
        return self.json("POST", "/api/evento", ev)

    def test_envia_telegram_com_link(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        codigo, resp = self.evento()
        self.assertEqual((codigo, resp["telegram"]), (200, True))
        metodo, caminho, _, corpo = Falso.recebidos[-1]
        self.assertEqual(caminho, "/botTOK/sendMessage")
        msg = json.loads(corpo)
        self.assertEqual(msg["chat_id"], "42")
        self.assertIn("aprovacao: Allow Bash?", msg["text"])
        self.assertIn("pc · api/main", msg["text"])
        self.assertIn("http://127.0.0.1:", msg["text"])
        self.assertIn("/#/pc/api/main", msg["text"])

    def test_arquivo_vai_como_documento(self):
        q = "maquina=pc&projeto=api&terminal=main&nome=r%C3%A9l.txt&legenda=oi"
        codigo, resp = self.json("POST", "/api/arquivo?" + q, b"conteudo-xyz")
        self.assertEqual((codigo, resp["telegram"]), (200, True))
        _, caminho, _, corpo = Falso.recebidos[-1]
        self.assertEqual(caminho, "/botTOK/sendDocument")
        self.assertIn(b"conteudo-xyz", corpo)
        self.assertIn("📎 pc · api/main\noi".encode(), corpo)

    def test_arquivo_invalido(self):
        self.assertEqual(self.chamar("POST", "/api/arquivo?maquina=a/b", b"x")[0], 400)
        self.assertEqual(self.chamar("POST", "/api/arquivo?maquina=pc", b"")[0], 400)

    def test_duplicado_em_janela_curta(self):
        self.evento()
        _, resp = self.evento()
        self.assertTrue(resp.get("duplicado"))
        self.assertEqual(len(Falso.recebidos), 1)

    def test_motivo_invalido(self):
        self.assertEqual(self.evento(motivo="xyz")[0], 400)
        self.assertEqual(self.evento(maquina="a b")[0], 400)
        self.assertEqual(self.evento(projeto="a/b")[0], 400)
        self.assertEqual(self.evento(terminal="a b")[0], 400)

    def test_sessao_antiga_vira_projeto_e_terminal(self):
        self.json("POST", "/api/evento", {"maquina": "pc", "sessao": "api/t2", "motivo": "terminou", "texto": "x"})
        _, evs = self.json("GET", "/api/eventos")
        self.assertEqual((evs[-1]["projeto"], evs[-1]["terminal"]), ("api", "t2"))

    def test_motivo_desligado_registra_mas_nao_envia(self):
        self.json("PUT", "/api/config", {"notificacoes": {"aprovacao": False}})
        _, resp = self.evento()
        self.assertEqual(resp, {"telegram": False, "silenciado": True})
        self.assertEqual(Falso.recebidos, [])
        _, evs = self.json("GET", "/api/eventos")
        self.assertEqual(evs[-1]["motivo"], "aprovacao")

    def test_sem_telegram_so_registra_log(self):
        self.cfg.tg_token = ""
        _, resp = self.evento()
        self.assertFalse(resp["telegram"])
        _, evs = self.json("GET", "/api/eventos")
        self.assertEqual((evs[-1]["motivo"], evs[-1]["projeto"], evs[-1]["terminal"]), ("aprovacao", "api", "main"))


class TestConvite(Base):
    def setUp(self):
        super().setUp()
        orig = painel.ip_tailnet
        painel.ip_tailnet = lambda: ""  # sem Tailscale: convite usa o Host (comportamento antigo, determinístico)
        self.addCleanup(setattr, painel, "ip_tailnet", orig)

    def test_convite_sai_pela_tailnet_mesmo_na_pagina_publica(self):
        painel.ip_tailnet = lambda: "100.99.99.99"
        codigo, resp = self.json("POST", "/api/convite", {"nome": "nova"}, {"Host": "203.0.113.10:8787"})
        self.assertEqual(codigo, 200)
        # o instalador baixa pelo endereço da página (a máquina nova ainda não tem Tailscale),
        # mas o agente aponta para a tailnet (sem senha)
        self.assertIn("curl -fsSL http://203.0.113.10:8787/bootstrap.sh", resp["comando"])
        self.assertIn(f"--painel http://100.99.99.99:{self.srv.server_address[1]}", resp["comando"])

    def test_convite_usa_painel_publico_no_download_quando_configurado(self):
        painel.ip_tailnet = lambda: "100.99.99.99"
        self.cfg.publico = "9.9.9.9"
        _, resp = self.json("POST", "/api/convite", {"nome": "nova", "sem_root": True}, {"Host": "100.9.9.9:8787"})
        self.assertIn("curl -fsSL http://9.9.9.9:", resp["comando"])
        self.assertIn("--painel http://100.99.99.99:", resp["comando"])

    def test_gera_comando_com_authkey(self):
        codigo, resp = self.json("POST", "/api/convite", {"nome": "srv-multi"}, {"Host": "100.9.9.9:8787"})
        self.assertEqual(codigo, 200)
        self.assertIn("TS_AUTHKEY=tskey-auth-FALSA", resp["comando"])
        self.assertIn("--nome srv-multi --painel http://100.9.9.9:8787", resp["comando"])
        metodo, caminho, auth, corpo = Falso.recebidos[-1]
        self.assertEqual(caminho, "/api/v2/tailnet/-/keys")
        self.assertTrue(auth.startswith("Basic "))
        criar = json.loads(corpo)["capabilities"]["devices"]["create"]
        self.assertEqual((criar["reusable"], criar["preauthorized"]), (False, True))
        self.assertNotIn("tags", criar)

    def test_tag_opcional(self):
        self.cfg.ts_tag = "tag:tty"
        self.json("POST", "/api/convite", {"nome": "x"})
        criar = json.loads(Falso.recebidos[-1][3])["capabilities"]["devices"]["create"]
        self.assertEqual(criar["tags"], ["tag:tty"])

    def test_sem_root_nao_usa_sudo_nem_authkey(self):
        codigo, resp = self.json("POST", "/api/convite", {"nome": "alice", "sem_root": True}, {"Host": "100.9.9.9:8787"})
        self.assertEqual(codigo, 200)
        self.assertEqual(resp["comando"], "curl -fsSL http://100.9.9.9:8787/bootstrap.sh | bash -s -- --nome alice --painel http://100.9.9.9:8787")
        self.assertNotIn("sudo", resp["comando"])
        self.assertNotIn("TS_AUTHKEY", resp["comando"])
        self.assertEqual(Falso.recebidos, [])

    def test_sem_root_funciona_sem_credencial_da_api(self):
        self.cfg.ts_api_key = ""
        self.assertEqual(self.json("POST", "/api/convite", {"nome": "x", "sem_root": True})[0], 200)
        self.assertEqual(self.json("POST", "/api/convite", {"nome": "a b", "sem_root": True})[0], 400)

    def test_sem_credencial(self):
        self.cfg.ts_api_key = ""
        self.assertEqual(self.json("POST", "/api/convite", {"nome": "x"})[0], 502)

    def test_nome_invalido(self):
        self.assertEqual(self.json("POST", "/api/convite", {"nome": "a b; rm"})[0], 400)


class TestArquivos(Base):
    def test_bootstrap(self):
        codigo, corpo = self.chamar("GET", "/bootstrap.sh")
        self.assertEqual((codigo, corpo), (200, b"#!/bin/bash\necho oi\n"))

    def test_pacote_sem_testes(self):
        _, corpo = self.chamar("GET", "/pacote.tar.gz")
        nomes = tarfile.open(fileobj=io.BytesIO(corpo)).getnames()
        self.assertIn("bootstrap/bootstrap.sh", nomes)
        self.assertIn("notificador/tty-notify", nomes)
        self.assertIn("projetos/tty-projeto", nomes)
        self.assertIn("agente/agente.py", nomes)
        self.assertNotIn("notificador/test_x.sh", nomes)

    def test_config_ida_e_volta(self):
        self.assertEqual(self.chamar("GET", "/api/config/claude")[0], 404)
        self.assertEqual(self.json("POST", "/api/config/claude", b"conteudo")[0], 200)
        self.assertEqual(self.chamar("GET", "/api/config/claude"), (200, b"conteudo"))

    def test_config_nome_invalido(self):
        self.assertEqual(self.chamar("GET", "/api/config/..%2Fx")[0] in (400, 404), True)


class TestConfig(Base):
    def test_padrao_e_persistencia(self):
        _, c = self.json("GET", "/api/config")
        self.assertEqual(c["notificacoes"], painel.NOTIF_PADRAO)
        self.assertFalse(c["notificacoes"]["ocioso"])
        _, c = self.json("PUT", "/api/config", {"notificacoes": {"ocioso": True, "terminou": False, "invalido": True}})
        self.assertTrue(c["notificacoes"]["ocioso"])
        self.assertFalse(c["notificacoes"]["terminou"])
        self.assertNotIn("invalido", c["notificacoes"])
        _, c = self.json("GET", "/api/config")
        self.assertTrue(c["notificacoes"]["ocioso"])

    def test_put_invalido(self):
        self.assertEqual(self.json("PUT", "/api/config", {"notificacoes": "x"})[0], 400)
        self.assertEqual(self.json("PUT", "/api/config", [1])[0], 400)


def heartbeat(extra=None):
    hb = {"cores": 4, "ram_mb": 8000, "mem_mb": 2000, "cpu": 12.5, "shell": "zsh",
          "projetos": {"api": {"dir": "/srv/api", "usuario": "dev", "porta": 7700, "branch": "main", "cpu": 3.0, "mem_mb": 300,
                               "terminais": {"main": {"cmd": "bash", "cpu": 0.1, "mem_mb": 10, "atividade_s": 120},
                                             "t2": {"cmd": "node", "cpu": 2.9, "mem_mb": 290, "atividade_s": 1}}},
                       "web": {"dir": "/srv/web", "usuario": "dev", "porta": 7701, "branch": "", "cpu": 0, "mem_mb": 0, "terminais": {}}}}
    hb.update(extra or {})
    return hb


class TestEstado(Base):
    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})

    def test_heartbeat_de_maquina_desconhecida(self):
        self.assertEqual(self.json("POST", "/api/agente/ninguem", heartbeat())[0], 404)

    def test_estado_com_projetos_terminais_e_estados(self):
        self.assertEqual(self.json("POST", "/api/agente/pc", heartbeat()), (200, {"tarefas": []}))
        _, est = self.json("GET", "/api/estado")
        m = est["maquinas"][0]
        self.assertEqual((m["nome"], m["online"], m["cores"], m["ram_mb"], m["cpu"]), ("pc", True, 4, 8000, 12.5))
        api = next(p for p in m["projetos"] if p["nome"] == "api")
        self.assertEqual((api["branch"], api["porta"], api["estado"]), ("main", 7700, "rodando"))
        terms = {t["nome"]: t["estado"] for t in api["terminais"]}
        self.assertEqual(terms, {"main": "ocioso", "t2": "rodando"})
        web = next(p for p in m["projetos"] if p["nome"] == "web")
        self.assertEqual(web["estado"], "ocioso")

    def test_offline_sem_heartbeat_recente_mostra_ultima_estrutura(self):
        self.json("POST", "/api/agente/pc", heartbeat())
        self.cfg.vivo["pc"][""]["em"] -= 60
        _, est = self.json("GET", "/api/estado")
        m = est["maquinas"][0]
        self.assertFalse(m["online"])
        self.assertTrue(all(p["estado"] == "offline" for p in m["projetos"]))

    def test_estrutura_persiste_para_maquina_que_nunca_mais_respondeu(self):
        self.json("POST", "/api/agente/pc", heartbeat())
        self.cfg.vivo.clear()
        _, est = self.json("GET", "/api/estado")
        m = est["maquinas"][0]
        self.assertEqual(sorted(p["nome"] for p in m["projetos"]), ["api", "web"])
        self.assertEqual(m["cores"], 4)
        self.assertEqual(m["projetos"][0]["estado"], "offline")

    def test_aguardando_acende_com_evento_e_apaga_com_atividade(self):
        self.json("POST", "/api/agente/pc", heartbeat())
        self.json("POST", "/api/evento", {"maquina": "pc", "projeto": "api", "terminal": "main",
                                           "motivo": "aprovacao", "texto": "Allow?"})
        _, est = self.json("GET", "/api/estado")
        api = next(p for p in est["maquinas"][0]["projetos"] if p["nome"] == "api")
        self.assertEqual(api["estado"], "aguardando")
        self.assertEqual({t["nome"]: t["estado"] for t in api["terminais"]}["main"], "aguardando")
        # o usuário respondeu: houve atividade depois do evento
        hb = heartbeat()
        hb["projetos"]["api"]["terminais"]["main"]["atividade_s"] = 0
        self.cfg.atencao[("pc", "api", "main")] -= 30
        self.json("POST", "/api/agente/pc", hb)
        _, est = self.json("GET", "/api/estado")
        api = next(p for p in est["maquinas"][0]["projetos"] if p["nome"] == "api")
        self.assertNotEqual({t["nome"]: t["estado"] for t in api["terminais"]}["main"], "aguardando")

    def test_motivo_terminou_nao_pede_atencao(self):
        self.json("POST", "/api/agente/pc", heartbeat())
        self.json("POST", "/api/evento", {"maquina": "pc", "projeto": "api", "terminal": "main", "motivo": "terminou", "texto": "x"})
        self.assertEqual(self.cfg.atencao, {})

    def test_payload_malformado_nao_derruba(self):
        codigo, _ = self.json("POST", "/api/agente/pc", {"projetos": {"../x": {}, "ok": "lixo", "api": {"dir": "/a", "usuario": "u", "porta": 7700,
                                                          "terminais": {"a b": {}, "m": {"cmd": "x"}}}}})
        self.assertEqual(codigo, 200)
        _, est = self.json("GET", "/api/estado")
        projs = est["maquinas"][0]["projetos"]
        self.assertEqual([p["nome"] for p in projs], ["api"])
        self.assertEqual([t["nome"] for t in projs[0]["terminais"]], ["m"])


def hb_agente(agente, projetos, **extra):
    """Heartbeat de um agente (usuário) com projetos {nome: porta}."""
    hb = {"agente": agente, "cores": 6, "ram_mb": 12000, "mem_mb": 4000, "cpu": 20.0, "shell": "bash",
          "projetos": {n: {"dir": f"/home/{agente}/{n}", "usuario": agente, "porta": porta, "branch": "main", "cpu": 1.0, "mem_mb": 50,
                           "terminais": {"main": {"cmd": "bash", "cpu": 0.1, "mem_mb": 5, "atividade_s": 500}}}
                       for n, porta in projetos.items()}}
    hb.update(extra)
    return hb


class TestVariosAgentes(Base):
    """Um servidor com vários usuários (modo sem root): UMA máquina, um projeto por usuário."""

    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "srv-multi", "ip": "100.1.1.9", "porta": 7681})
        self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}))
        self.json("POST", "/api/agente/srv-multi", hb_agente("bob", {"shell-bob": 7682}))

    def estado(self):
        _, est = self.json("GET", "/api/estado")
        return est

    def test_uma_maquina_com_um_projeto_por_usuario(self):
        est = self.estado()
        self.assertEqual([m["nome"] for m in est["maquinas"]], ["srv-multi"])
        m = est["maquinas"][0]
        self.assertEqual(sorted((p["nome"], p["usuario"], p["agente"], p["porta"]) for p in m["projetos"]),
                         [("shell-alice", "alice", "alice", 7681), ("shell-bob", "bob", "bob", 7682)])
        self.assertTrue(m["online"])
        self.assertEqual(sorted(a["id"] for a in m["agentes"]), ["alice", "bob"])

    def test_um_agente_cai_so_os_projetos_dele_ficam_offline(self):
        self.cfg.vivo["srv-multi"]["bob"]["em"] -= 60
        m = self.estado()["maquinas"][0]
        self.assertTrue(m["online"])  # o outro usuário continua respondendo
        por_nome = {p["nome"]: p["estado"] for p in m["projetos"]}
        self.assertEqual(por_nome["shell-bob"], "offline")
        self.assertNotEqual(por_nome["shell-alice"], "offline")

    def test_ferramentas_por_agente(self):
        self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}, ferramentas={"claude": True}))
        m = self.estado()["maquinas"][0]
        por_ag = {a["id"]: a["ferramentas"] for a in m["agentes"]}
        self.assertEqual(por_ag["alice"], {"claude": True})
        self.assertEqual(por_ag["bob"], {})
        self.assertEqual(m["ferramentas"], {"claude": True})

    def test_tarefa_vai_para_o_agente_dono_do_projeto(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "terminal.novo", "args": {"projeto": "shell-bob"}})
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}))
        self.assertEqual(resp["tarefas"], [])
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("bob", {"shell-bob": 7682}))
        self.assertEqual([t["id"] for t in resp["tarefas"]], [r["id"]])

    def test_tarefa_limpar_vai_para_o_agente_dono_do_projeto(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "terminal.limpar", "args": {"projeto": "shell-bob"}})
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}))
        self.assertEqual(resp["tarefas"], [])
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("bob", {"shell-bob": 7682}))
        self.assertEqual([(t["id"], t["tipo"]) for t in resp["tarefas"]], [(r["id"], "terminal.limpar")])

    def test_tarefa_por_usuario_vai_para_o_agente_do_usuario(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "ferramenta.add", "args": {"usuario": "alice", "ferramenta": "claude"}})
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("bob", {"shell-bob": 7682}))
        self.assertEqual(resp["tarefas"], [])
        _, resp = self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}))
        self.assertEqual([t["id"] for t in resp["tarefas"]], [r["id"]])

    def test_resultado_de_outro_agente_nao_fecha_a_tarefa(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "terminal.novo", "args": {"projeto": "shell-bob"}})
        self.json("POST", "/api/agente/srv-multi", hb_agente("bob", {"shell-bob": 7682}))
        self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}, resultados=[{"id": r["id"], "ok": True, "saida": "x"}]))
        self.assertEqual(self.json("GET", f"/api/tarefas/{r['id']}")[1]["estado"], "enviada")

    def test_novo_projeto_sem_usuario_e_ambiguo(self):
        codigo, resp = self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "projeto.add", "args": {"nome": "x", "dir": "/a"}})
        self.assertEqual(codigo, 400)
        self.assertIn("usuário", resp["erro"])
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "tipo": "projeto.add", "args": {"nome": "x", "dir": "/a", "usuario": "bob"}})[0], 200)

    def test_agente_explicito_precisa_existir(self):
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "agente": "nao", "tipo": "projeto.rm", "args": {"nome": "x"}})[0], 400)
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "srv-multi", "agente": "bob", "tipo": "projeto.rm", "args": {"nome": "x"}})[0], 200)

    def test_remover_um_agente_mantem_a_maquina_e_o_outro(self):
        codigo, resp = self.json("DELETE", "/api/maquinas/srv-multi?agente=bob")
        self.assertEqual((codigo, resp["projetos_restantes"]), (200, 1))
        m = self.estado()["maquinas"][0]
        self.assertEqual([p["nome"] for p in m["projetos"]], ["shell-alice"])
        self.assertEqual([a["id"] for a in m["agentes"]], ["alice"])
        codigo, resp = self.json("DELETE", "/api/maquinas/srv-multi?agente=alice")
        self.assertEqual((codigo, resp["projetos_restantes"]), (200, 0))
        self.assertEqual(self.estado()["maquinas"], [])

    def test_remover_a_maquina_inteira(self):
        self.assertEqual(self.json("DELETE", "/api/maquinas/srv-multi")[0], 200)
        self.assertEqual(self.estado()["maquinas"], [])
        self.assertNotIn("srv-multi", self.cfg.vivo)

    def test_estrutura_persistida_soma_os_agentes(self):
        self.cfg.vivo.clear()
        m = self.estado()["maquinas"][0]
        self.assertEqual(sorted(p["nome"] for p in m["projetos"]), ["shell-alice", "shell-bob"])
        self.assertFalse(m["online"])

    def test_nome_de_projeto_repetido_entre_usuarios_nao_quebra(self):
        self.json("POST", "/api/agente/srv-multi", hb_agente("zeta", {"shell-bob": 7690}))
        projs = [p for p in self.estado()["maquinas"][0]["projetos"] if p["nome"] == "shell-bob"]
        self.assertEqual(len(projs), 1)

    def test_agente_parado_nao_esconde_o_projeto_do_agente_fresco(self):
        # mesmo nome de projeto reportado por um agente velho ("") e por um novo: vale o que está respondendo
        self.json("POST", "/api/agente/srv-multi", hb_agente("", {"compartilhado": 7001}))
        self.json("POST", "/api/agente/srv-multi", hb_agente("root", {"compartilhado": 7002}))
        self.cfg.vivo["srv-multi"][""]["em"] -= 60
        p = next(x for x in self.estado()["maquinas"][0]["projetos"] if x["nome"] == "compartilhado")
        self.assertEqual((p["agente"], p["porta"]), ("root", 7002))
        self.assertNotEqual(p["estado"], "offline")

    def test_agente_sumido_ha_muito_tempo_sai_da_memoria(self):
        self.cfg.vivo["srv-multi"]["bob"]["em"] -= painel.AGENTE_VALE_S + 5
        self.json("POST", "/api/agente/srv-multi", hb_agente("alice", {"shell-alice": 7681}))
        self.assertEqual(sorted(self.cfg.vivo["srv-multi"]), ["alice"])

    def test_agente_com_id_invalido_vira_padrao(self):
        self.json("POST", "/api/agente/srv-multi", hb_agente("a b;c", {"p": 7699}))
        self.assertIn("", self.cfg.vivo["srv-multi"])


class TestPastaListar(Base):
    """pasta.listar (autocompletar): resultado grande em `dados` e consulta leve de tarefas (1 s)."""

    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        self.json("POST", "/api/agente/pc", hb_agente("dev", {"api": 7700}))

    def leve(self, agente="dev", resultados=None, maquina="pc"):
        return self.json("POST", f"/api/agente/{maquina}/tarefas", {"agente": agente, "resultados": resultados or []})

    def test_tarefa_roteada_pelo_usuario_e_dados_voltam(self):
        codigo, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/srv/"}})
        self.assertEqual(codigo, 200)
        codigo, resp = self.leve()
        self.assertEqual(resp["tarefas"], [{"id": r["id"], "tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/srv/"}}])
        dados = {"ok": True, "dir": "/srv", "home": "/home/dev", "pastas": ["a", "b"], "total": 2, "truncado": False}
        self.leve(resultados=[{"id": r["id"], "ok": True, "saida": "", "dados": dados}])
        _, t = self.json("GET", f"/api/tarefas/{r['id']}")
        self.assertEqual((t["estado"], t["dados"]), ("ok", dados))

    def test_dados_grandes_demais_sao_descartados_mas_o_resultado_vale(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/"}})
        self.leve()
        enorme = {"ok": True, "pastas": ["x" * 100] * 400}
        self.leve(resultados=[{"id": r["id"], "ok": True, "saida": "", "dados": enorme}])
        _, t = self.json("GET", f"/api/tarefas/{r['id']}")
        self.assertEqual(t["estado"], "ok")
        self.assertNotIn("dados", t)

    def test_tarefa_sem_dados_nao_tem_o_campo(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "terminal.novo", "args": {"projeto": "api"}})
        self.leve(resultados=[])
        self.assertNotIn("dados", self.json("GET", f"/api/tarefas/{r['id']}")[1])

    def test_consulta_leve_so_entrega_as_tarefas_do_agente(self):
        self.json("POST", "/api/agente/pc", hb_agente("outro", {"web": 7701}))
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "pasta.listar", "args": {"usuario": "outro", "dir": "/"}})
        self.assertEqual(self.leve("dev")[1]["tarefas"], [])
        self.assertEqual([t["id"] for t in self.leve("outro")[1]["tarefas"]], [r["id"]])
        self.assertEqual(self.leve("outro")[1]["tarefas"], [])  # já foi entregue (enviada)

    def test_agente_desconhecido_nao_pega_tarefa(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "terminal.novo", "args": {"projeto": "api"}})
        self.assertEqual(self.leve("intruso")[1], {"tarefas": []})
        self.assertEqual(self.json("GET", f"/api/tarefas/{r['id']}")[1]["estado"], "pendente")

    def test_maquina_desconhecida_e_nome_invalido(self):
        self.assertEqual(self.leve(maquina="nao-existe")[0], 404)
        self.assertEqual(self.json("POST", "/api/agente/a%20b/tarefas", {"agente": "dev"})[0], 400)

    def test_heartbeat_continua_entregando_tarefas_sem_duplicar(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/"}})
        _, hb = self.json("POST", "/api/agente/pc", hb_agente("dev", {"api": 7700}))
        self.assertEqual([t["id"] for t in hb["tarefas"]], [r["id"]])
        self.assertEqual(self.leve()[1]["tarefas"], [])


class TestTarefas(Base):
    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})

    def test_ciclo_completo(self):
        codigo, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "terminal.novo", "args": {"projeto": "api"}})
        self.assertEqual(codigo, 200)
        tid = r["id"]
        self.assertEqual(self.json("GET", f"/api/tarefas/{tid}")[1]["estado"], "pendente")
        _, resp = self.json("POST", "/api/agente/pc", heartbeat())
        self.assertEqual(resp["tarefas"], [{"id": tid, "tipo": "terminal.novo", "args": {"projeto": "api"}}])
        self.assertEqual(self.json("GET", f"/api/tarefas/{tid}")[1]["estado"], "enviada")
        _, resp = self.json("POST", "/api/agente/pc", heartbeat({"resultados": [{"id": tid, "ok": True, "saida": "criado"}]}))
        self.assertEqual(resp["tarefas"], [])  # não reenvia
        self.assertEqual(self.json("GET", f"/api/tarefas/{tid}")[1], {"id": tid, "maquina": "pc", "tipo": "terminal.novo", "estado": "ok", "saida": "criado"})

    def test_falha_do_agente(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}})
        self.json("POST", "/api/agente/pc", heartbeat())
        self.json("POST", "/api/agente/pc", heartbeat({"resultados": [{"id": r["id"], "ok": False, "saida": "recusada"}]}))
        self.assertEqual(self.json("GET", f"/api/tarefas/{r['id']}")[1]["estado"], "erro")

    def test_validacao(self):
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "nao", "tipo": "projeto.rm", "args": {}})[0], 404)
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "shell.exec", "args": {"cmd": "id"}})[0], 400)
        self.assertEqual(self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": "x"})[0], 400)
        self.assertEqual(self.json("GET", "/api/tarefas/naoexiste")[0], 404)

    def test_tarefa_so_vai_para_a_maquina_certa(self):
        self.chamar("POST", "/api/registrar", {"nome": "outra", "ip": "100.1.1.2", "porta": 7681})
        self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}})
        _, resp = self.json("POST", "/api/agente/outra", heartbeat())
        self.assertEqual(resp["tarefas"], [])

    def test_tarefa_antiga_expira(self):
        _, r = self.json("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}})
        self.cfg.tarefas[r["id"]]["criada_em"] -= painel.TAREFA_VALE_S + 5
        _, resp = self.json("POST", "/api/agente/pc", heartbeat())
        self.assertEqual(resp["tarefas"], [])
        self.assertEqual(self.json("GET", f"/api/tarefas/{r['id']}")[1]["estado"], "expirada")


class TestFerramentas(Base):
    def test_lista_ferramentas_sem_modelo(self):
        pasta = self.cfg.raiz / "facilitadores" / "ferramentas"
        pasta.mkdir(parents=True)
        (pasta / "claude.sh").write_text('BIN=claude\nDESC="CLI da Anthropic"\ninstalar() { :; }\n')
        (pasta / "_modelo.sh").write_text('BIN=x\nDESC="modelo"\n')
        (pasta / "semdesc.sh").write_text("BIN=semdesc\n")
        (pasta / "Mai.sh").write_text("BIN=x\n")
        _, lista = self.json("GET", "/api/ferramentas")
        sem = {"tem": False, "bytes": 0, "em": 0}
        self.assertEqual(lista, [{"nome": "claude", "bin": "claude", "desc": "CLI da Anthropic", "config": sem},
                                 {"nome": "semdesc", "bin": "semdesc", "desc": "", "config": sem}])

    def test_estado_traz_ferramentas_do_agente(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        self.json("POST", "/api/agente/pc", heartbeat({"ferramentas": {"claude": True, "opencode": False, "../x": True}}))
        _, est = self.json("GET", "/api/estado")
        self.assertEqual(est["maquinas"][0]["ferramentas"], {"claude": True, "opencode": False})


class TestConfigDasFerramentas(Base):
    def test_ferramentas_dizem_se_o_painel_tem_a_config(self):
        pasta = self.cfg.raiz / "facilitadores" / "ferramentas"
        pasta.mkdir(parents=True)
        (pasta / "claude.sh").write_text('BIN=claude\nDESC="x"\n')
        (pasta / "opencode.sh").write_text('BIN=opencode\nDESC="y"\n')
        self.chamar("POST", "/api/config/claude", b"conteudo")
        _, lista = self.json("GET", "/api/ferramentas")
        por = {f["nome"]: f["config"] for f in lista}
        self.assertTrue(por["claude"]["tem"])
        self.assertEqual(por["claude"]["bytes"], 8)
        self.assertGreater(por["claude"]["em"], 0)
        self.assertEqual(por["opencode"], {"tem": False, "bytes": 0, "em": 0})


class TestEsquecerProjeto(Base):
    """Projetos fantasma: o agente dono está offline, então o projeto.rm não tem quem execute."""

    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "srv", "ip": "100.1.1.5", "porta": 7681})
        self.json("POST", "/api/agente/srv", hb_agente("a", {"vivo": 7700}))
        self.json("POST", "/api/agente/srv", hb_agente("b", {"fantasma": 7701}))
        self.cfg.vivo["srv"]["b"]["em"] -= 60  # o agente b parou

    def nomes(self):
        _, est = self.json("GET", "/api/estado")
        return sorted(p["nome"] for p in est["maquinas"][0]["projetos"])

    def test_esquece_projeto_de_agente_offline(self):
        self.assertEqual(self.nomes(), ["fantasma", "vivo"])
        self.assertEqual(self.json("DELETE", "/api/maquinas/srv/projetos/fantasma"), (200, {"esquecido": "fantasma"}))
        self.assertEqual(self.nomes(), ["vivo"])
        _, reg = self.json("GET", "/api/maquinas")
        self.assertNotIn("fantasma", reg["srv"]["projetos"])
        self.assertIn("vivo", reg["srv"]["projetos"])

    def test_recusa_se_o_agente_esta_online(self):
        codigo, resp = self.json("DELETE", "/api/maquinas/srv/projetos/vivo")
        self.assertEqual(codigo, 409)
        self.assertIn("online", resp["erro"])
        self.assertEqual(self.nomes(), ["fantasma", "vivo"])

    def test_so_estrutura_persistida_sem_nenhum_agente(self):
        self.cfg.vivo.clear()
        self.assertEqual(self.json("DELETE", "/api/maquinas/srv/projetos/fantasma")[0], 200)
        self.assertEqual(self.nomes(), ["vivo"])

    def test_erros(self):
        self.assertEqual(self.json("DELETE", "/api/maquinas/nao/projetos/x")[0], 404)
        self.assertEqual(self.json("DELETE", "/api/maquinas/srv/projetos/naoexiste")[0], 404)
        self.assertEqual(self.json("DELETE", "/api/maquinas/srv/projetos/a%20b")[0], 400)

    def test_o_que_nao_e_fantasma_nao_some_se_o_agente_voltar(self):
        self.json("DELETE", "/api/maquinas/srv/projetos/fantasma")
        self.json("POST", "/api/agente/srv", hb_agente("b", {"fantasma": 7701}))  # o agente voltou e o projeto ainda existe
        self.assertEqual(self.nomes(), ["fantasma", "vivo"])


class TestEstatico(Base):
    def test_traversal_bloqueado(self):
        for caminho in ("/static/../painel.py", "/static/%2e%2e/painel.py", "/static/..%2fpainel.py", "/static//etc/passwd"):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.chamar("GET", caminho)[0], 404)

    def test_arquivo_estatico_e_tipo(self):
        painel.ESTATICO.mkdir(exist_ok=True)
        arq = painel.ESTATICO / "_teste.js"
        arq.write_text("x")
        try:
            req = urllib.request.urlopen(self.base + "/static/_teste.js")
            self.assertEqual((req.status, req.headers["Content-Type"].split(";")[0]), (200, "text/javascript"))
        finally:
            arq.unlink()


class TestRele(Base):
    """GET /tty/<maquina>/<projeto>/<resto>: o painel repassa ao ttyd pela tailnet."""

    def ttyd_falso(self, resposta=b"oi-do-ttyd"):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        porta = srv.getsockname()[1]
        pedidos = []

        def atender():
            for _ in range(5):
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                with c:
                    req = b""
                    while b"\r\n\r\n" not in req:
                        parte = c.recv(4096)
                        if not parte:
                            break
                        req += parte
                    pedidos.append(req)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(resposta) + resposta)

        threading.Thread(target=atender, daemon=True).start()
        self.addCleanup(srv.close)
        return porta, pedidos

    def test_http_pelo_rele_e_sem_vazar_authorization(self):
        porta, pedidos = self.ttyd_falso()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        self.registro.guardar_projetos("pc", {"api": {"dir": "/x", "usuario": "u", "porta": porta, "agente": ""}}, 4, 8000)
        codigo, corpo = self.chamar("GET", "/tty/pc/api/", headers={"Authorization": "Basic dXNlcjpzZW5oYQ=="})
        self.assertEqual((codigo, corpo), (200, b"oi-do-ttyd"))
        self.assertTrue(pedidos)
        self.assertNotIn(b"authorization", pedidos[0].lower())
        self.assertIn(b"Host: 127.0.0.1:", pedidos[0])

    def test_erros(self):
        self.assertEqual(self.chamar("GET", "/tty/")[0], 404)
        self.assertEqual(self.chamar("GET", "/tty/pc")[0], 404)
        self.assertEqual(self.chamar("GET", "/tty/a%20b/api/")[0], 400)
        self.assertEqual(self.chamar("GET", "/tty/pc/naoexiste/")[0], 404)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        # projeto sem porta registrada (ttyd fora do ar ou sem heartbeat): 404, não 500
        self.registro.guardar_projetos("pc", {"api": {"dir": "/x", "usuario": "u", "porta": 1, "agente": ""}}, 4, 8000)
        self.assertEqual(self.chamar("GET", "/tty/pc/outro/")[0], 404)
        # porta fechada (ttyd caiu): 502, não 500
        self.assertEqual(self.chamar("GET", "/tty/pc/api/")[0], 502)

    def test_rele_recusa_ip_fora_da_tailnet(self):
        # SSRF: máquina registrada com IP público não vira proxy para a internet
        self.chamar("POST", "/api/registrar", {"nome": "evil", "ip": "8.8.8.8", "porta": 80})
        self.registro.guardar_projetos("evil", {"x": {"dir": "/x", "usuario": "u", "porta": 80, "agente": ""}}, 1, 100)
        orig = socket.create_connection
        chamadas = []
        def espiar(endereco, *a, **k):
            chamadas.append(endereco)
            if endereco[0] == "8.8.8.8":
                raise OSError("bloqueado no teste")  # o código tentou o alvo proibido
            return orig(endereco, *a, **k)
        socket.create_connection = espiar
        try:
            self.assertEqual(self.chamar("GET", "/tty/evil/x/")[0], 502)
        finally:
            socket.create_connection = orig
        self.assertNotIn("8.8.8.8", [c[0] for c in chamadas])  # recusado antes de tentar conectar


class TestBootstrapPublico(Base):
    """A máquina nova ainda não tem Tailscale: o /bootstrap.sh é aberto na internet.
    Todo o resto continua exigindo senha fora da tailnet."""

    def setUp(self):
        super().setUp()
        self.cfg.senha = "segredo"
        orig = painel.ip_isento
        orig_local = painel._is_localhost
        painel.ip_isento = lambda ip: False
        painel._is_localhost = lambda ip: False  # finge internet (nada isento)
        self.addCleanup(setattr, painel, "ip_isento", orig)
        self.addCleanup(setattr, painel, "_is_localhost", orig_local)

    def test_bootstrap_aberto_e_resto_fechado(self):
        self.assertEqual(self.chamar("GET", "/bootstrap.sh")[0], 200)
        self.assertEqual(self.chamar("GET", "/pacote.tar.gz")[0], 401)
        self.assertEqual(self.chamar("GET", "/")[0], 401)
        self.assertEqual(self.chamar("GET", "/api/maquinas")[0], 401)
        _, corpo = self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "1.2.3.4", "porta": 1})
        self.assertEqual(corpo, b'{"erro": "senha do painel"}')


if __name__ == "__main__":
    unittest.main()
