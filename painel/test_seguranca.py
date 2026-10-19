"""Regressão de segurança (TDD): hardening sem mudar funcionalidade/UI.

Cada teste aqui falha antes do fix e passa depois. Nenhum muda fluxo
legítimo: só headers, IDs imprevisíveis, validação estrita e freio de
brute-force em falha de auth (caminho 401 existente).
"""
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
            dados, senha="", sincrono=True, raiz=Path(self.tmp.name) / "raiz",
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

    def chamar(self, metodo, caminho, corpo=None, headers=None):
        dados = corpo if isinstance(corpo, bytes) else (json.dumps(corpo).encode() if corpo is not None else None)
        req = urllib.request.Request(self.base + caminho, method=metodo, data=dados, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers


class TestHigieneHTTP(Base):
    def test_server_neutro_sem_versao(self):
        _, _, h = self.chamar("GET", "/bootstrap.sh")
        server = h.get("Server", "")
        self.assertNotIn("BaseHTTP", server)
        self.assertNotIn("Python", server)

    def test_headers_seguranca_em_api_e_static(self):
        painel.ESTATICO.mkdir(exist_ok=True)
        arq = painel.ESTATICO / "_seg.js"
        arq.write_text("x")
        try:
            _, _, h1 = self.chamar("GET", "/api/maquinas")
            _, _, h2 = self.chamar("GET", "/static/_seg.js")
        finally:
            arq.unlink()
        for h in (h1, h2):
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
            csp = h.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors", csp)
            # regressão: o CSP não pode quebrar o app (WS direto, fontes, tema)
            self.assertIn("ws:", csp)
            self.assertIn("https://fonts.googleapis.com", csp)
            self.assertIn("sha256-", csp)
            self.assertIn("'unsafe-inline'", csp)  # xterm.js injeta <style> da paleta; sem isso o tty fica P&B
        self.assertEqual(h1.get("Cache-Control"), "no-store")

    def test_verbo_desconhecido_exige_auth_antes_do_501(self):
        self.cfg.senha = "segredo"
        orig = painel.ip_isento
        orig_local = painel._is_localhost
        painel.ip_isento = lambda ip: False
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "ip_isento", orig)
        self.addCleanup(setattr, painel, "_is_localhost", orig_local)
        for verbo in ("PATCH", "OPTIONS", "TRACE"):
            with self.subTest(verbo=verbo):
                codigo, _, _ = self.chamar(verbo, "/api/estado")
                self.assertEqual(codigo, 401)


class TestBruteForce(Base):
    def test_falhas_repetidas_bloqueiam_com_429_e_retry_after(self):
        self.cfg.senha = "segredo"
        orig = painel.ip_isento
        orig_local = painel._is_localhost
        painel.ip_isento = lambda ip: False
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "ip_isento", orig)
        self.addCleanup(setattr, painel, "_is_localhost", orig_local)
        orig_lim, orig_jan = painel.LIMITE_FALHAS, painel.JANELA_FALHAS_S
        painel.LIMITE_FALHAS, painel.JANELA_FALHAS_S = 3, 60
        self.addCleanup(setattr, painel, "LIMITE_FALHAS", orig_lim)
        self.addCleanup(setattr, painel, "JANELA_FALHAS_S", orig_jan)
        codigos = [self.chamar("GET", "/api/estado")[0] for _ in range(5)]
        self.assertEqual(codigos[:3], [401, 401, 401])
        self.assertIn(429, codigos[3:])
        _, _, h = self.chamar("GET", "/api/estado")
        self.assertEqual(h.get("Retry-After") is not None, True)


class TestTarefaId(Base):
    def test_id_longo_imprevisivel(self):
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681})
        _, r, _ = self.chamar("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}})
        tid = json.loads(r)["id"]
        self.assertGreaterEqual(len(tid), 16)
        int(tid, 16)  # hex


class TestValidacaoEntrada(Base):
    def test_registrar_recusa_ip_e_usuario_malformados(self):
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "nao-ip", "porta": 7681})[0], 400)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681, "usuario": "a b"})[0], 400)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 7681, "usuario": ""})[0], 200)

    def test_evento_remove_controles_sem_mudar_texto_legitimo(self):
        codigo, resp = painel.tratar_evento(
            self.cfg, self.registro,
            {"maquina": "pc", "projeto": "api", "terminal": "main", "motivo": "erro", "texto": "a\x00b\rc"},
        )
        self.assertEqual(codigo, 200)
        ev = json.loads((self.cfg.eventos_log).read_text().splitlines()[-1])
        self.assertNotIn("\x00", ev["texto"])
        self.assertNotIn("\r", ev["texto"])
        # texto legítimo passa intacto
        codigo, _ = painel.tratar_evento(
            self.cfg, self.registro,
            {"maquina": "pc2", "projeto": "api", "terminal": "main", "motivo": "erro", "texto": "Allow Bash?"},
        )
        self.assertEqual(codigo, 200)

    def test_rele_recusa_arg_invalido_antes_de_conectar(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        porta = srv.getsockname()[1]

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
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        threading.Thread(target=atender, daemon=True).start()
        self.addCleanup(srv.close)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        self.registro.guardar_projetos("pc", {"api": {"dir": "/x", "usuario": "u", "porta": porta, "agente": ""}}, 4, 8000)
        self.assertEqual(self.chamar("GET", "/tty/pc/api/ws?arg=a/b")[0], 400)
        self.assertEqual(self.chamar("GET", "/tty/pc/api/ws?arg=main")[0], 200)


if __name__ == "__main__":
    unittest.main()
