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


if __name__ == "__main__":
    unittest.main()
