"""Fase 2 (TDD vermelho): tailnet exige auth, token de agente, sessão cookie, quotas, TLS/HSTS."""
import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import painel


def servir(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        dados = Path(self.tmp.name)
        self.cfg = painel.Config(
            dados, senha="segredo", token="tok-agente-123",
            sincrono=True, raiz=Path(self.tmp.name) / "raiz",
        )
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            (self.cfg.raiz / pasta).mkdir(parents=True)
        (self.cfg.raiz / "bootstrap" / "bootstrap.sh").write_text("#!/bin/bash\necho oi\n")
        self.registro = painel.Registro(dados / "m.json")
        painel._FALHAS.clear()
        self.srv, self.base = servir(painel.criar_handler(self.registro, self.cfg))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        painel._FALHAS.clear()

    def chamar(self, metodo, caminho, corpo=None, headers=None):
        dados = corpo if isinstance(corpo, bytes) else (json.dumps(corpo).encode() if corpo is not None else None)
        req = urllib.request.Request(self.base + caminho, method=metodo, data=dados, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def basic(self, senha="segredo"):
        return {"Authorization": "Basic " + base64.b64encode(f"u:{senha}".encode()).decode()}

    def bearer(self, tok="tok-agente-123"):
        return {"Authorization": f"Bearer {tok}"}


class TestTailnetExigeAuth(Base):
    def test_localhost_continua_isento(self):
        codigo, _, _ = self.chamar("GET", "/api/maquinas")
        self.assertEqual(codigo, 200)

    def test_tailnet_sem_credencial_negada(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        self.assertEqual(self.chamar("GET", "/api/estado")[0], 401)
        self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "x"})[0], 401)
        self.assertEqual(self.chamar("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}})[0], 401)

    def test_tailnet_com_senha_ou_token_passa(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.basic())[0], 200)
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.bearer())[0], 200)

    def test_agente_com_token_registra_e_pulsa(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}, self.bearer())[0], 200)
        hb = {"cores": 1, "ram_mb": 100, "projetos": {}}
        self.assertEqual(self.chamar("POST", "/api/agente/pc", hb, self.bearer())[0], 200)

    def test_sessao_cookie_evita_reenviar_senha(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        _, _, h = self.chamar("GET", "/api/maquinas", headers=self.basic())
        cookie = h.get("Set-Cookie", "")
        self.assertIn("Tessera=", cookie)
        self.assertIn("HttpOnly", cookie)
        so_cookie = {"Cookie": cookie.split(";", 1)[0]}
        self.assertEqual(self.chamar("GET", "/api/maquinas", headers=so_cookie)[0], 200)


class TestQuotasEValidacao(Base):
    def test_evento_acima_da_quota_vira_429(self):
        orig_lim = painel.LIMITE_EVENTOS_MIN
        painel.LIMITE_EVENTOS_MIN = 3
        self.addCleanup(setattr, painel, "LIMITE_EVENTOS_MIN", orig_lim)
        for i in range(3):
            codigo, _, _ = self.chamar("POST", "/api/evento",
                {"maquina": "pc", "motivo": "erro", "texto": f"m{i}"})
            self.assertEqual(codigo, 200)
        self.assertEqual(self.chamar("POST", "/api/evento",
            {"maquina": "pc", "motivo": "erro", "texto": "spam"})[0], 429)

    def test_tarefa_estrita_recusa_args(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1})
        self.assertEqual(self.chamar("POST", "/api/tarefas",
            {"maquina": "pc", "tipo": "projeto.add", "args": {"nome": "x", "dir": "/a", "usuario": "A B"}})[0], 400)
        self.assertEqual(self.chamar("POST", "/api/tarefas",
            {"maquina": "pc", "tipo": "terminal.novo", "args": {}})[0], 400)

    def test_rele_nao_repasa_origin(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        porta = srv.getsockname()[1]
        pedidos = []

        def atender():
            for _ in range(3):
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
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        threading.Thread(target=atender, daemon=True).start()
        self.addCleanup(srv.close)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        self.registro.guardar_projetos("pc", {"api": {"dir": "/x", "usuario": "u", "porta": porta, "agente": ""}}, 4, 8000)
        self.chamar("GET", "/tty/pc/api/", headers={"Origin": "http://evil.com", "Referer": "http://evil.com/"})
        self.assertTrue(pedidos)
        baixo = pedidos[0].lower()
        self.assertNotIn(b"evil", baixo)  # origin/referer do navegador nunca seguem
        self.assertNotIn(b"referer", baixo)
        self.assertIn(f"origin: http://127.0.0.1:{porta}".encode(), baixo)  # relé forja origin == host (ttyd -O)

    def test_hsts_quando_tls(self):
        self.cfg.tls = True
        _, _, h = self.chamar("GET", "/api/maquinas")
        self.assertIn("max-age", h.get("Strict-Transport-Security", ""))


if __name__ == "__main__":
    unittest.main()


class TestPonteMigracao(unittest.TestCase):
    def test_sem_token_tailnet_agente_passa_humano_nao(self):
        import tempfile as _t, threading as _th
        from http.server import ThreadingHTTPServer as _S
        from pathlib import Path as _P
        tmp = _t.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dados = _P(tmp.name)
        cfg = painel.Config(dados, senha="segredo", sincrono=True, raiz=_P(tmp.name) / "raiz")
        cfg.token = ""  # ponte: sem PAINEL_TOKEN
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            (cfg.raiz / pasta).mkdir(parents=True)
        (cfg.raiz / "bootstrap" / "bootstrap.sh").write_text("x")
        reg = painel.Registro(dados / "m.json")
        painel._FALHAS.clear()
        srv = _S(("127.0.0.1", 0), painel.criar_handler(reg, cfg))
        _th.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)

        def chamar(metodo, caminho, corpo=None, headers=None):
            import json as _j, urllib.request as _u, urllib.error as _e
            dados = _j.dumps(corpo).encode() if corpo is not None else None
            req = _u.Request(base + caminho, method=metodo, data=dados, headers=headers or {})
            try:
                with _u.urlopen(req) as r:
                    return r.status
            except _e.HTTPError as e:
                return e.code

        self.assertEqual(chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}), 200)
        self.assertEqual(chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {}}), 200)
        self.assertEqual(chamar("GET", "/pacote.tar.gz"), 200)
        self.assertEqual(chamar("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}}), 401)
        self.assertEqual(chamar("POST", "/api/convite", {"nome": "x"}), 401)
