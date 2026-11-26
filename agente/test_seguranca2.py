"""Fase 2 agente: envia Bearer quando há token, sem quebrar sem token."""
import json
import unittest
import urllib.request

import agente


class TestEnviaToken(unittest.TestCase):
    def test_sem_token_nao_manda_authorization(self):
        a = agente.Agente("http://x", "m")
        vistos = {}
        orig = urllib.request.urlopen

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *e): return False
            def read(self): return b"{}"
        def falso(req, timeout=5):
            vistos.update(req.headers)
            return Resp()
        urllib.request.urlopen = falso
        try:
            self.assertEqual(a._enviar("/api/agente/m", {}, []), {})
        finally:
            urllib.request.urlopen = orig
        self.assertNotIn("Authorization", vistos)

    def test_com_token_manda_bearer(self):
        a = agente.Agente("http://x", "m")
        orig = agente.token_painel
        agente.token_painel = lambda: "tok-123"
        vistos = {}
        orig_url = urllib.request.urlopen

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *e): return False
            def read(self): return b"{}"
        def falso(req, timeout=5):
            vistos.update(req.headers)
            return Resp()
        urllib.request.urlopen = falso
        try:
            a._enviar("/api/agente/m", {}, [])
        finally:
            urllib.request.urlopen = orig_url
            agente.token_painel = orig
        auth = vistos.get("Authorization", "")
        self.assertEqual(auth, "Bearer tok-123")


class TestAgenteSoFalaHttps(unittest.TestCase):
    def test_painel_so_https_ou_loopback_de_dev(self):
        # o agente leva o token da máquina: em http puro (ou IP/Host errado) ele vazaria
        self.assertIsNone(agente.url_painel_insegura("https://painel.exemplo.com"))
        self.assertIsNone(agente.url_painel_insegura("http://127.0.0.1:8787"))
        for ruim in ("http://painel.exemplo.com", "http://100.64.0.5:8787", "ftp://x", "painel.exemplo.com", ""):
            self.assertIn("https://", agente.url_painel_insegura(ruim), ruim)


class TestAgenteNaoSegueRedirect(unittest.TestCase):
    def test_redirect_do_painel_nao_leva_o_token_para_outro_host(self):
        # o Authorization iria junto num redirect (painel/proxy comprometido ou TLS quebrado)
        import http.server, threading
        visto = []
        class Alvo(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                visto.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            log_message = lambda *a: None
        alvo = http.server.HTTPServer(("127.0.0.1", 0), Alvo)
        class Redir(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307); self.send_header("Location", f"http://127.0.0.1:{alvo.server_address[1]}/x"); self.end_headers()
            log_message = lambda *a: None
        redir = http.server.HTTPServer(("127.0.0.1", 0), Redir)
        for srv in (alvo, redir):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
        import os
        os.environ["TTY_TOKEN"] = "tok-secreto"
        self.addCleanup(os.environ.pop, "TTY_TOKEN", None)
        a = agente.Agente(f"http://127.0.0.1:{redir.server_address[1]}", "m")
        self.assertIsNone(a._enviar("/api/agente/m", {}, []))  # redirect = erro, não segue
        self.assertEqual(visto, [])


if __name__ == "__main__":
    unittest.main()
