"""Fase 2 (TDD vermelho): tailnet exige auth, token de agente, sessão cookie, quotas, TLS/HSTS."""
import base64
import json
import tempfile
import threading
import time
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
            dados, senha="segredo", token="tok-agente-123", localhost_livre=True,
            sincrono=True, raiz=Path(self.tmp.name) / "raiz",
        )
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            (self.cfg.raiz / pasta).mkdir(parents=True)
        (self.cfg.raiz / "bootstrap" / "bootstrap.sh").write_text("#!/bin/bash\necho oi\n")
        self.registro = painel.Registro(dados / "m.json")
        painel._FALHAS.clear()
        painel._NIVEL.clear()
        self.srv, self.base = servir(painel.criar_handler(self.registro, self.cfg))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        painel._FALHAS.clear()
        painel._NIVEL.clear()

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

    def bearer(self, maquina="pc"):
        return {"Authorization": f"Bearer {painel.token_maquina(self.cfg, maquina)}"}


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

    def test_tailnet_com_senha_passa_e_token_de_maquina_so_vale_no_plano_agente(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.basic())[0], 200)
        # token de máquina NÃO abre rota de humano (antes um token único abria o painel inteiro)
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.bearer())[0], 403)

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
        host = self.base.split("//")[1]
        # Origin do próprio painel (um Origin de outro site já leva 403, ver TestCsrf); Referer de fora não pode seguir
        self.chamar("GET", "/tty/pc/api/ws?arg=main", headers={"Origin": f"http://{host}", "Referer": "http://evil.com/"})
        self.assertTrue(pedidos)
        baixo = pedidos[0].lower()
        self.assertNotIn(b"evil", baixo)  # origin/referer do navegador nunca seguem
        self.assertNotIn(b"referer", baixo)
        self.assertIn(f"origin: http://127.0.0.1:{porta}".encode(), baixo)  # relé forja origin == host (ttyd -O)

    def test_hsts_quando_tls(self):
        self.cfg.tls = True
        _, _, h = self.chamar("GET", "/api/maquinas")
        self.assertIn("max-age", h.get("Strict-Transport-Security", ""))


class TestAvisosDeSeguranca(unittest.TestCase):
    def test_curinga_sem_tls_sem_token_e_sem_senha_avisam_tudo(self):
        self.assertEqual(len(painel.avisos_de_seguranca("0.0.0.0", False, "", "")), 4)

    def test_tailnet_com_token_e_senha_nao_avisa(self):
        self.assertEqual(painel.avisos_de_seguranca("100.64.0.5", False, "t", "s", ("p.exemplo.com",)), [])

    def test_curinga_com_tls_nao_avisa_de_claro(self):
        self.assertEqual(painel.avisos_de_seguranca("0.0.0.0", True, "t", "s", ("p.exemplo.com",)), [])


class TestEscopoDoToken(Base):
    """Auditoria: uma máquina comprometida não pode virar dona do painel nem de outras máquinas."""
    def setUp(self):
        super().setUp()
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)

    def test_token_de_maquina_nao_abre_rotas_de_humano(self):
        b = self.bearer("pc")
        self.assertEqual(self.chamar("POST", "/api/tarefas", {"maquina": "x", "tipo": "sistema.atualizar"}, b)[0], 403)
        self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, b)[0], 403)
        self.assertEqual(self.chamar("POST", "/api/config/claude", b"xx", b)[0], 403)  # sobrescrever a config de todas: só humano
        self.assertEqual(self.chamar("DELETE", "/api/maquinas/outra", None, b)[0], 403)
        self.assertEqual(self.chamar("PUT", "/api/config", {"notificacoes": {}}, b)[0], 403)

    def test_token_de_maquina_le_config_e_fala_o_plano_agente(self):
        b = self.bearer("pc")
        self.assertEqual(self.chamar("GET", "/api/config/claude", None, b)[0], 404)  # passou a auth (só não há tarball)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}, b)[0], 200)

    def test_token_so_age_em_nome_da_propria_maquina(self):
        b = self.bearer("pc")
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "outra", "ip": "100.1.1.1", "porta": 1}, b)[0], 403)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}, b)
        self.assertEqual(self.chamar("POST", "/api/agente/outra", {"cores": 1, "ram_mb": 1, "projetos": {}}, b)[0], 403)
        self.assertEqual(self.chamar("POST", "/api/agente/outra/tarefas", {}, b)[0], 403)
        self.assertEqual(self.chamar("POST", "/api/evento", {"maquina": "outra", "motivo": "erro", "texto": "x"}, b)[0], 403)
        self.assertEqual(self.chamar("POST", "/api/arquivo?maquina=outra&nome=a", b"x", b)[0], 403)

    def test_token_forjado_ou_o_mestre_nao_valem(self):
        self.assertEqual(self.chamar("GET", "/api/config/claude", None, {"Authorization": "Bearer pc.deadbeef"})[0], 401)
        self.assertEqual(self.chamar("GET", "/api/config/claude", None, {"Authorization": "Bearer tok-agente-123"})[0], 401)
        # token de uma máquina com o nome trocado
        nome, _, mac = painel.token_maquina(self.cfg, "pc").rpartition(".")
        self.assertEqual(self.chamar("GET", "/api/config/claude", None, {"Authorization": f"Bearer outra.{mac}"})[0], 401)


class TestLoopbackNaoEDono(Base):
    def test_sem_localhost_livre_loopback_exige_senha(self):
        # em produção qualquer processo local (ou proxy reverso) chega de 127.0.0.1: não pode ser "dono"
        self.cfg.localhost_livre = False
        self.assertEqual(self.chamar("GET", "/api/estado")[0], 401)
        self.assertEqual(self.chamar("POST", "/api/tarefas", {"maquina": "x", "tipo": "sistema.atualizar"})[0], 401)

    def test_request_via_proxy_nunca_e_loopback(self):
        self.assertEqual(self.chamar("GET", "/api/estado")[0], 200)  # dev: livre
        self.assertEqual(self.chamar("GET", "/api/estado", headers={"X-Forwarded-For": "203.0.113.9"})[0], 401)


class TestCsrfEHost(Base):
    def test_post_de_outro_origin_e_barrado(self):
        # form cross-site (enctype text/plain) com a credencial já aceita pelo navegador: o navegador manda Origin
        c = self.chamar("POST", "/api/tarefas", b'{"maquina":"x","tipo":"sistema.atualizar"}', {**self.basic(), "Origin": "http://evil.example"})
        self.assertEqual(c[0], 403)
        self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, {**self.basic(), "Origin": "null"})[0], 403)
        self.assertEqual(self.chamar("DELETE", "/api/maquinas/x", None, {**self.basic(), "Origin": "http://evil.example"})[0], 403)

    def test_origin_do_proprio_painel_e_cliente_sem_origin_passam(self):
        host = self.base.split("//")[1]
        self.assertNotEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, {**self.basic(), "Origin": f"http://{host}"})[0], 403)
        self.assertNotEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, self.basic())[0], 403)

    def test_websocket_do_rele_de_outro_origin_e_barrado(self):
        self.assertEqual(self.chamar("GET", "/tty/pc/api/ws?arg=main", None, {**self.basic(), "Origin": "http://evil.example"})[0], 403)

    def test_so_o_dominio_passa_qualquer_outro_host_leva_421(self):
        # entrada única https://<domínio>: IP, localhost e outros nomes são bloqueados, inclusive o loopback e o bootstrap
        self.cfg.dominio = "painel.exemplo.com"
        for host in ("127.0.0.1:1", "100.64.0.5:8787", "evil.example", "painel.exemplo.com.evil.example", "painel.exemplo.com:9999"):
            self.assertEqual(self.chamar("GET", "/api/estado", headers={**self.basic(), "Host": host})[0], 421, host)
        self.assertEqual(self.chamar("GET", "/bootstrap.sh", headers={"Host": "100.64.0.5:8787"})[0], 421)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "x", "ip": "100.1.1.1", "porta": 1}, {"Host": "100.64.0.5"})[0], 421)
        self.assertEqual(self.chamar("GET", "/api/estado", headers={**self.basic(), "Host": "Painel.Exemplo.com"})[0], 200)

    def test_sem_dominio_so_em_dev_e_o_painel_de_producao_nao_sobe(self):
        class C:  # só os campos que a checagem lê
            localhost_livre = False; dominio = ""; totp_secret = "JBSWY3DPEHPK3PXP"; senha = "x" * 20
        self.assertIn("PAINEL_DOMINIO", painel.entrada_insegura(C, True))
        C.dominio = "painel.exemplo.com"
        self.assertIn("TLS", painel.entrada_insegura(C, False))  # nada de HTTP puro
        self.assertIsNone(painel.entrada_insegura(C, True))
        C.localhost_livre, C.dominio = True, ""
        self.assertIsNone(painel.entrada_insegura(C, False))  # dev

    def test_em_producao_agente_so_registra_ip_da_tailnet(self):
        # token de máquina não pode apontar o relé para 127.0.0.1 (serviços locais do painel) nem para a internet
        self.cfg.dominio = "painel.exemplo.com"
        h = {"Host": "painel.exemplo.com", **self.bearer("pc")}
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        for ip in ("127.0.0.1", "203.0.113.7", "10.0.0.5"):
            self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": ip, "porta": 1}, h)[0], 400, ip)
        self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.100.1.1", "porta": 1}, h)[0], 200)

    def test_faixa_da_tailnet(self):
        for ip in ("100.64.0.1", "100.100.5.5", "100.127.255.255", "fd7a:115c:a1e0::1"):
            self.assertTrue(painel.da_tailnet(ip), ip)
        for ip in ("100.63.255.255", "100.128.0.1", "127.0.0.1", "203.0.113.7", "lixo"):
            self.assertFalse(painel.da_tailnet(ip), ip)

    def test_conexao_da_tailnet_e_descartada_sem_resposta_mesmo_autenticada(self):
        # a tailnet é só o canal painel -> ttyd; nada entra no painel por ela, nem com senha ou token válidos
        import http.client
        host, porta = self.base.split("//")[1].split(":")
        self.cfg.dominio = "painel.exemplo.com"
        orig = painel.da_tailnet
        painel.da_tailnet = lambda ip: True  # simula par da tailnet / conexão no IP da tailnet do servidor
        self.addCleanup(setattr, painel, "da_tailnet", orig)
        for metodo, caminho, corpo, cab in (
            ("GET", "/api/estado", None, self.basic()),
            ("GET", "/bootstrap.sh", None, {}),
            ("POST", "/api/registrar", b'{"nome":"pc","ip":"100.1.1.1","porta":1}', self.bearer("pc")),
            ("GET", "/", None, {}),
        ):
            c = http.client.HTTPConnection(host, int(porta), timeout=5)
            c.request(metodo, caminho, body=corpo, headers={**cab, "Host": "painel.exemplo.com"})
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                c.getresponse()
        painel.da_tailnet = orig
        self.assertEqual(self.chamar("GET", "/api/estado", headers={**self.basic(), "Host": "painel.exemplo.com"})[0], 200)

    def test_convite_com_dominio_usa_so_https_dominio(self):
        self.cfg.dominio = "painel.exemplo.com"
        painel.ip_tailnet = lambda: "100.99.99.99"
        c, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova", "sem_root": True}, {**self.basic(), "Host": "painel.exemplo.com"})
        cmd = json.loads(corpo)["comando"]
        self.assertIn("curl -fsSL https://painel.exemplo.com/bootstrap.sh", cmd)
        self.assertIn("--painel https://painel.exemplo.com ", cmd + " ")
        self.assertNotIn("http://", cmd)
        self.assertNotIn("http://100.99.99.99", cmd)  # o agente nunca aponta para a tailnet: só https://<domínio>
        # o IP do painel na tailnet vai só como o ÚNICO par autorizado a alcançar os ttyd da máquina (firewall)
        self.assertIn("--painel-ip 100.99.99.99", cmd)

    def test_convite_nao_manda_painel_ip_fora_da_faixa_da_tailnet(self):
        self.cfg.dominio = "painel.exemplo.com"
        painel.ip_tailnet = lambda: "203.0.113.9"
        _, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova", "sem_root": True}, {**self.basic(), "Host": "painel.exemplo.com"})
        self.assertNotIn("--painel-ip", json.loads(corpo)["comando"])

    def test_cookie_de_sessao_com_tls_e_host_secure(self):
        self.cfg.tls = True
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        _, _, h = self.chamar("GET", "/api/maquinas", headers=self.basic())
        cookie = h.get("Set-Cookie", "")
        self.assertTrue(cookie.startswith("__Host-Tessera="), cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn("Domain", cookie)
        self.assertEqual(self.chamar("GET", "/api/maquinas", headers={"Cookie": cookie.split(";", 1)[0]})[0], 200)
        # cookie com o nome antigo (que um subdomínio poderia plantar) não vale
        self.assertEqual(self.chamar("GET", "/api/maquinas", headers={"Cookie": "Tessera=" + cookie.split("=", 1)[1].split(";", 1)[0]})[0], 401)

    def test_convite_nao_ecoa_host_arbitrario(self):
        c = self.chamar("POST", "/api/convite", {"nome": "x", "sem_root": True}, {**self.basic(), "Host": "evil.com/x;rm"})
        self.assertEqual(c[0], 400)

    def test_convite_leva_token_da_maquina_e_nunca_o_mestre(self):
        c, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova", "sem_root": True}, self.basic())
        cmd = json.loads(corpo)["comando"]
        self.assertIn(painel.token_maquina(self.cfg, "nova"), cmd)
        self.assertNotIn("tok-agente-123", cmd)


class TestAuditoriaESenha(Base):
    def test_acoes_sensiveis_ficam_no_log_sem_segredos(self):
        self.chamar("POST", "/api/convite", {"nome": "nova", "sem_root": True}, self.basic())
        self.chamar("POST", "/api/tarefas", {"maquina": "x", "tipo": "sistema.atualizar"}, self.basic())
        log = (self.cfg.dados / "auditoria.log").read_text()
        self.assertIn('"acao": "convite"', log)
        self.assertIn('"acao": "tarefa"', log)
        self.assertNotIn("segredo", log)
        self.assertNotIn("tok-agente-123", log)

    def test_login_falho_e_auditado(self):
        orig = painel._is_localhost
        painel._is_localhost = lambda ip: False
        self.addCleanup(setattr, painel, "_is_localhost", orig)
        self.chamar("GET", "/api/estado", headers=self.basic("errada"))
        self.assertIn("login-falhou", (self.cfg.dados / "auditoria.log").read_text())

    def test_senha_curta_e_recusada(self):
        self.assertIsNotNone(painel.senha_fraca("curta123"))
        self.assertIsNone(painel.senha_fraca("x" * 20))
        self.assertIsNone(painel.senha_fraca(""))  # vazia = internet bloqueada, tratada à parte

    def test_content_length_negativo_nao_pendura_a_thread(self):
        import http.client
        host, porta = self.base.split("//")[1].split(":")
        c = http.client.HTTPConnection(host, int(porta), timeout=5)
        c.request("POST", "/api/convite", body=b"", headers={**self.basic(), "Content-Length": "-1"})
        self.assertEqual(c.getresponse().status, 400)


class TestTotpSessao(Base):
    """Login = senha + código do autenticador; sessão de 30 min só renovada por atividade explícita."""
    SEG = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def setUp(self):
        super().setUp()
        self.cfg.totp_secret = self.SEG
        self.cfg.localhost_livre = False
        self.ua = {"User-Agent": "navegador-do-dono"}

    def codigo(self, deslocamento=0):
        return painel.totp_codigo(self.SEG, int(time.time() // 30) + deslocamento)

    def login(self, senha="segredo", codigo=None, headers=None):
        return self.chamar("POST", "/api/login", {"senha": senha, "codigo": self.codigo() if codigo is None else codigo}, {**self.ua, **(headers or {})})

    def cookie(self, h):
        return {"Cookie": h["Set-Cookie"].split(";", 1)[0], **self.ua}

    def test_vetor_rfc6238(self):
        # RFC 6238 apêndice B (SHA1, segredo ASCII "12345678901234567890"), T=59 => 94287082 (6 dígitos: 287082)
        seg = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(painel.totp_codigo(seg, 59 // 30), "287082")
        self.assertEqual(painel.totp_codigo(seg, 1111111109 // 30), "081804")

    def test_login_ok_com_senha_e_codigo(self):
        c, corpo, h = self.login()
        self.assertEqual(c, 200)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.cookie(h))[0], 200)

    def test_so_a_senha_ou_so_o_codigo_nao_entram_e_o_erro_nao_diz_qual(self):
        a = self.login(codigo="000000")
        b = self.login(senha="errada", codigo=self.codigo(1))
        self.assertEqual((a[0], b[0]), (401, 401))
        self.assertEqual(a[1], b[1])  # mesma mensagem: sem oráculo de qual fator falhou
        self.assertNotIn("Set-Cookie", a[2])

    def test_basic_com_senha_certa_nao_vale_com_2fa(self):
        # a senha sozinha (Basic, que o navegador reenvia sozinho) não abre nada quando há 2FA
        self.assertEqual(self.chamar("GET", "/api/estado", headers=self.basic())[0], 401)
        self.assertEqual(self.chamar("POST", "/api/tarefas", {"maquina": "x", "tipo": "sistema.atualizar"}, self.basic())[0], 401)

    def test_codigo_nao_pode_ser_reusado(self):
        cod = self.codigo()
        self.assertEqual(self.login(codigo=cod)[0], 200)
        self.assertEqual(self.login(codigo=cod)[0], 401)  # replay do mesmo código
        self.assertEqual(self.login(codigo=self.codigo(-1))[0], 401)  # passo anterior também queimado

    def test_codigo_nao_queima_com_senha_errada(self):
        cod = self.codigo()
        self.assertEqual(self.login(senha="errada", codigo=cod)[0], 401)
        self.assertEqual(self.login(codigo=cod)[0], 200)  # o dono ainda consegue usar o código

    def test_codigo_malformado_e_longo_sao_recusados(self):
        for ruim in ("", "12345", "1234567", "abcdef", "12 345", "٣٣٣٣٣٣"):
            self.cfg.totp_erros.clear()  # (senha certa + código errado conta para a pausa do 2FA; aqui o foco é o formato)
            self.assertEqual(self.login(codigo=ruim)[0], 401, ruim)

    def test_forca_bruta_do_login_leva_429(self):
        for _ in range(12):
            self.login(codigo="000000")
        self.assertEqual(self.login()[0], 429)  # mesmo com credenciais certas, enquanto bloqueado

    def test_anonimo_nao_trava_o_login_do_dono(self):
        # achado do red team: a "pausa global" deixava 1 IP anônimo bloqueando o login de todos para sempre.
        # Só o IP que erra é bloqueado; o dono (outro IP) entra com senha + código certos.
        painel._FALHAS["203.0.113.9"] = [time.time()] * 50
        self.assertEqual(self.login()[0], 200)

    def test_sessao_vence_em_30_min_e_so_renovacao_explicita_estende(self):
        _, _, h = self.login()
        ck = self.cookie(h)
        tok = ck["Cookie"].split("=", 1)[1]
        self.assertEqual(painel.SESSAO_VALE_S, 30 * 60)
        # polling (/api/estado) NÃO renova: 29 min depois ainda vale, mas o vencimento não andou
        exp0 = self.cfg.sessoes[tok]["exp"]
        self.assertEqual(self.chamar("GET", "/api/estado", headers=ck)[0], 200)
        self.assertEqual(self.cfg.sessoes[tok]["exp"], exp0)
        # renovação explícita empurra o vencimento
        self.cfg.sessoes[tok]["exp"] = time.time() + 10
        self.assertEqual(self.chamar("POST", "/api/sessao/renovar", None, ck)[0], 200)
        self.assertGreater(self.cfg.sessoes[tok]["exp"], time.time() + 25 * 60)

    def test_passou_de_30_min_qualquer_acao_nova_pede_login_inclusive_renovar(self):
        _, _, h = self.login()
        ck = self.cookie(h)
        self.cfg.sessoes[ck["Cookie"].split("=", 1)[1]]["exp"] = time.time() - 1  # 30 min sem atividade
        for metodo, caminho, corpo in (("GET", "/api/estado", None), ("GET", "/", None), ("POST", "/api/sessao/renovar", None),
                                       ("POST", "/api/tarefas", {"maquina": "x", "tipo": "sistema.atualizar"}),
                                       ("GET", "/tty/pc/api/ws?arg=main", None)):  # terminal NOVO
            c, resp, _ = self.chamar(metodo, caminho, corpo, ck)
            if caminho == "/":
                self.assertIn(b"Senha", resp)  # F5 depois de expirar cai na tela de login, não no app
            else:
                self.assertEqual(c, 401, caminho)
                self.assertEqual(json.loads(resp)["erro"], "login")

    def test_nao_da_para_renovar_sessao_ja_vencida(self):
        _, _, h = self.login()
        ck = self.cookie(h)
        tok = ck["Cookie"].split("=", 1)[1]
        self.cfg.sessoes[tok]["exp"] = time.time() - 1
        self.assertEqual(self.chamar("POST", "/api/sessao/renovar", None, ck)[0], 401)
        self.assertNotIn(tok, self.cfg.sessoes)

    def test_teto_absoluto_mesmo_renovando(self):
        _, _, h = self.login()
        ck = self.cookie(h)
        tok = ck["Cookie"].split("=", 1)[1]
        self.cfg.sessoes[tok]["max"] = time.time() + 5
        self.chamar("POST", "/api/sessao/renovar", None, ck)
        self.assertLessEqual(self.cfg.sessoes[tok]["exp"], self.cfg.sessoes[tok]["max"])

    def test_cookie_roubado_com_outro_user_agent_nao_vale(self):
        _, _, h = self.login()
        roubado = {"Cookie": h["Set-Cookie"].split(";", 1)[0], "User-Agent": "outro-navegador"}
        self.assertEqual(self.chamar("GET", "/api/estado", headers=roubado)[0], 401)

    def test_logout_invalida_a_sessao(self):
        _, _, h = self.login()
        ck = self.cookie(h)
        self.assertEqual(self.chamar("POST", "/api/logout", None, ck)[0], 200)
        self.assertEqual(self.chamar("GET", "/api/estado", headers=ck)[0], 401)

    def test_sem_sessao_so_a_tela_de_login_e_publica(self):
        c, corpo, _ = self.chamar("GET", "/", None, self.ua)
        self.assertEqual(c, 200)
        self.assertIn(b"Senha", corpo)
        self.assertNotIn(b"tessera.js", corpo)  # o app (e o xterm) só depois do login
        self.assertEqual(self.chamar("GET", "/static/login.js", None, self.ua)[0], 200)
        self.assertEqual(self.chamar("GET", "/static/tessera.js", None, self.ua)[0], 401)
        self.assertEqual(self.chamar("GET", "/static/vendor/xterm.js", None, self.ua)[0], 401)

    def test_login_exige_origin_do_proprio_painel(self):
        host = self.base.split("//")[1]
        c = self.chamar("POST", "/api/login", {"senha": "segredo", "codigo": self.codigo()}, {**self.ua, "Origin": "http://evil.example"})
        self.assertEqual(c[0], 403)
        self.assertEqual(self.chamar("POST", "/api/login", {"senha": "segredo", "codigo": self.codigo()}, {**self.ua, "Origin": f"http://{host}"})[0], 200)

    def test_senha_certa_e_codigo_errado_pausa_o_2fa_de_qualquer_ip_e_alerta(self):
        # red team M4: sem teto global, quem vaza a senha chuta o TOTP de milhares de IPs. Só ESTE sinal pausa (anônimo não dispara).
        self.cfg.tg_token, self.cfg.tg_chat = "T", "1"
        enviados = []
        orig = painel.enviar_telegram
        painel.enviar_telegram = lambda cfg, msg: enviados.append(msg) or True
        self.addCleanup(setattr, painel, "enviar_telegram", orig)
        for i in range(painel.TOTP_ERROS_MAX):
            painel._FALHAS.clear()
            painel._NIVEL.clear()
            self.assertEqual(self.login(codigo="000000")[0], 401)
        self.assertEqual(len(enviados), 1)  # alerta no Telegram, uma vez
        self.assertIn("SENHA CORRETA", enviados[0])
        painel._FALHAS.clear()
        painel._NIVEL.clear()
        self.assertEqual(self.login()[0], 429)  # até o código certo espera: é o preço de ter a senha vazada
        self.assertIn("2fa-forca-bruta", (self.cfg.dados / "auditoria.log").read_text())
        # N1 (oráculo de senha): pausado, senha ERRADA recebe o MESMO 429 (e a mesma resposta), senão o 429 revelaria a senha certa
        c_certa, corpo_certa, _ = self.login()
        c_errada, corpo_errada, _ = self.login(senha="errada", codigo="000000")
        self.assertEqual((c_certa, c_errada), (429, 429))
        self.assertEqual(corpo_certa, corpo_errada)
        self.cfg.totp_erros.clear()
        self.assertEqual(self.login()[0], 200)

    def test_anonimo_com_senha_errada_nunca_alimenta_a_pausa_do_2fa(self):
        for _ in range(painel.TOTP_ERROS_MAX * 3):
            painel._FALHAS.clear()
            painel._NIVEL.clear()
            self.assertEqual(self.login(senha="errada", codigo="000000")[0], 401)
        self.assertEqual(self.cfg.totp_erros, [])
        self.assertEqual(self.login()[0], 200)  # o dono entra normalmente

    def test_ipv6_conta_por_64(self):
        self.assertEqual(painel._norm_ip("2001:db8::1"), painel._norm_ip("2001:db8::ffff:2"))
        self.assertNotEqual(painel._norm_ip("2001:db8::1"), painel._norm_ip("2001:db9::1"))
        self.assertEqual(painel._norm_ip("::ffff:203.0.113.9"), "203.0.113.9")
        self.assertEqual(painel._norm_ip("203.0.113.9"), "203.0.113.9")

    def test_sem_totp_configurado_login_nao_existe(self):
        self.cfg.totp_secret = ""
        self.assertEqual(self.login()[0], 404)

    def test_producao_nao_sobe_sem_totp_nem_senha(self):
        class C:
            localhost_livre = False; dominio = "p.exemplo.com"; totp_secret = ""; senha = "x" * 20
        self.assertIn("PAINEL_TOTP_SECRET", painel.entrada_insegura(C, True))
        C.totp_secret, C.senha = self.SEG, ""
        self.assertIn("PAINEL_SENHA", painel.entrada_insegura(C, True))
        C.senha = "x" * 20
        self.assertIsNone(painel.entrada_insegura(C, True))

    def test_gerador_de_segredo(self):
        seg = painel.totp_novo_segredo()
        self.assertRegex(seg, r"^[A-Z2-7]{32}$")
        self.assertIn("otpauth://totp/Tessera:admin?secret=" + seg, painel.totp_uri(seg))


class TestAchadosDoRedTeam(Base):
    """Cada teste reproduz um achado da auditoria adversarial (rodada 1)."""

    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1})

    def hb(self, corpo):
        return self.chamar("POST", "/api/agente/pc", corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode(), self.bearer("pc"))

    def test_main_sobe_sem_painel_bind(self):
        # o main() estourava UnboundLocalError (cfg usado antes de existir) e nenhum teste chamava main()
        import os, subprocess, sys
        env = {"PATH": os.environ["PATH"], "PAINEL_DADOS": self.tmp.name + "/m", "PAINEL_LOCALHOST_LIVRE": "1", "PAINEL_PORTA": "0",
               "PAINEL_SENHA": "", "PAINEL_TOKEN": ""}
        p = subprocess.Popen([sys.executable, "-I", str(Path(painel.__file__))], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            linha = p.stdout.readline()
            self.assertIn("painel em http://127.0.0.1", linha)
        finally:
            p.kill()
            p.wait()

    def test_main_recusa_producao_sem_dominio(self):
        import os, subprocess, sys
        env = {"PATH": os.environ["PATH"], "PAINEL_DADOS": self.tmp.name + "/m", "PAINEL_PORTA": "0", "PAINEL_SENHA": "x" * 24}
        r = subprocess.run([sys.executable, "-I", str(Path(painel.__file__))], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PAINEL_DOMINIO", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_heartbeat_hostil_nao_derruba_o_estado_dos_humanos(self):
        hostis = [
            {"projetos": {"p": {"dir": "/x", "usuario": "u", "porta": 1, "terminais": {"main": {"cmd": ["x"]}}}}},
            {"projetos": {"p": {"dir": "/x", "usuario": "u", "porta": 1, "terminais": {"main": {"atividade_s": "x", "cpu": "nan"}}}}},
            {"cpu": "nan", "cores": 4, "ram_mb": 1},
            {"projetos": {"p": {"porta": 99999, "cpu": float("inf"), "terminais": "lixo"}}},
            {"projetos": ["lixo"], "ferramentas": ["a"], "cores": "x"},
        ]
        for corpo in hostis:
            self.assertEqual(self.hb(corpo)[0], 200, corpo)
            c, resp = self.chamar("GET", "/api/estado")[:2]
            self.assertEqual(c, 200)
            json.loads(resp.decode(), parse_constant=lambda x: (_ for _ in ()).throw(AssertionError("NaN/Infinity no JSON: " + x)))
        self.assertEqual(self.hb(b'{"cores": 1e999, "ram_mb": -1e999}')[0], 200)  # OverflowError no int()
        self.assertEqual(painel._num(float("nan"), 0, 10), 0)
        self.assertEqual(painel._num("x", 0, 10, int, 7), 7)
        self.assertEqual(painel._num(10**9, 0, 10, int), 10)

    def test_heartbeat_limita_projetos_terminais_e_agentes(self):
        grande = {f"p{i}": {"dir": "/x", "usuario": "u", "porta": 1, "terminais": {f"t{j}": {} for j in range(80)}} for i in range(80)}
        self.hb({"projetos": grande})
        proj = self.cfg.vivo["pc"][""]["payload"]["projetos"]
        self.assertLessEqual(len(proj), painel.MAX_PROJETOS)
        self.assertLessEqual(max(len(p["terminais"]) for p in proj.values()), painel.MAX_TERMINAIS)
        for i in range(painel.MAX_AGENTES_POR_MAQUINA + 10):
            self.hb({"agente": f"a{i}", "projetos": {}})
        self.assertLessEqual(len(self.cfg.vivo["pc"]), painel.MAX_AGENTES_POR_MAQUINA)

    def test_heartbeat_acima_do_limite_e_recusado(self):
        c = self.hb(b'{"projetos":{}, "lixo":"' + b"a" * (painel.LIMITE_HEARTBEAT + 10) + b'"}')
        self.assertEqual(c[0], 400)

    def test_evento_com_sessao_enorme_nao_enche_log_nem_estado(self):
        self.chamar("POST", "/api/evento", {"maquina": "pc", "motivo": "erro", "texto": "x", "sessao": "a" * 900_000})
        self.assertLess(self.cfg.eventos_log.stat().st_size, 2000)
        _, est = self.chamar("GET", "/api/eventos?n=30")[:2]
        self.assertLess(len(est), 5000)

    def test_ultimos_eventos_le_so_o_fim_e_ignora_linha_corrompida(self):
        self.cfg.dados.mkdir(exist_ok=True)
        with self.cfg.eventos_log.open("w") as f:
            f.write("lixo{\n" + "".join(json.dumps({"em": i, "maquina": "pc", "motivo": "erro", "texto": "t"}) + "\n" for i in range(5000)))
        ev = painel.ultimos_eventos(self.cfg, 3)
        self.assertEqual([e["em"] for e in ev], [4997, 4998, 4999])

    def test_logs_tem_teto_e_rotacionam(self):
        self.cfg.dados.mkdir(exist_ok=True)
        log = self.cfg.dados / "auditoria.log"
        log.write_text("x" * (painel.LIMITE_LOG + 1))
        self.chamar("POST", "/api/convite", {"nome": "n", "sem_root": True})
        self.assertTrue((self.cfg.dados / "auditoria.log.1").exists())
        self.assertLess(log.stat().st_size, 1000)

    def test_ruido_anonimo_nao_enche_a_auditoria(self):
        self.cfg.dominio = "painel.exemplo.com"
        for _ in range(100):
            self.chamar("GET", "/api/estado", headers={"Host": "errado.example"})
        linhas = (self.cfg.dados / "auditoria.log").read_text().splitlines()
        self.assertLessEqual(len(linhas), 30)

    def test_regex_nao_aceitam_quebra_de_linha_no_fim(self):
        for rx in (painel.NOME_OK, painel.PROJ_OK, painel.TERM_OK, painel.CRED_OK, painel.AGENTE_OK, painel.USUARIO_OK, painel.FERRAMENTA_OK):
            self.assertIsNone(rx.match("a\n"), rx.pattern)
        self.assertIsNone(painel.CRED_OK.match("a" * 32 + "\n"))
        self.assertEqual(self.chamar("GET", "/tty/pc/api/ws?arg=main%0a")[0], 400)

    def test_ip_registrado_sem_zona_e_sem_lixo(self):
        for ip in ("100.64.0.5%x", "100.64.0.5%", "100.64.0.5%x\r\nX-Evil: 1", "nao-e-ip", "100.64.0.5 \r\nX: 1"):
            self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": ip, "porta": 1})[0], 400, repr(ip))
        c, corpo, _ = self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.64.0.5", "porta": 1})
        self.assertEqual(json.loads(corpo)["ip"], "100.64.0.5")

    def test_login_com_corpo_invalido_responde_400(self):
        self.cfg.totp_secret, self.cfg.localhost_livre = "JBSWY3DPEHPK3PXP", False
        for corpo in (b"{nope", b"[]", b"\xff\xfe"):
            c = self.chamar("POST", "/api/login", corpo, {"User-Agent": "u"})
            self.assertEqual(c[0], 400, corpo)

    def test_eventos_com_n_invalido_nao_derruba(self):
        self.assertEqual(self.chamar("GET", "/api/eventos?n=abc")[0], 200)

    def test_excecao_inesperada_vira_500_limpo_e_nao_conexao_muda(self):
        orig = painel.montar_estado
        painel.montar_estado = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, painel, "montar_estado", orig)
        c, corpo = self.chamar("GET", "/api/estado")[:2]
        self.assertEqual(c, 500)
        self.assertEqual(json.loads(corpo), {"erro": "interno"})

    def test_da_tailnet_ipv4_mapeado_em_ipv6(self):
        self.assertTrue(painel.da_tailnet("::ffff:100.64.0.1"))
        self.assertFalse(painel.da_tailnet("::ffff:203.0.113.1"))

    def test_origin_compara_o_esquema(self):
        host = self.base.split("//")[1]
        h = {"Origin": f"https://{host}"}  # painel em http: um Origin https não é o mesmo
        self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, h)[0], 403)
        self.cfg.tls = True
        self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, {"Origin": f"http://{host}"})[0], 403)
        self.assertNotEqual(self.chamar("POST", "/api/convite", {"nome": "x"}, {"Origin": f"https://{host}"})[0], 403)

    def test_totp_so_um_sucesso_em_logins_paralelos_e_sobrevive_a_restart(self):
        seg = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        self.cfg.totp_secret, self.cfg.localhost_livre = seg, False
        cod = painel.totp_codigo(seg, int(time.time() // 30))
        res = []
        def tenta():
            res.append(self.chamar("POST", "/api/login", {"senha": "segredo", "codigo": cod}, {"User-Agent": "u"})[0])
        ts = [threading.Thread(target=tenta) for _ in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(res.count(200), 1, res)
        novo = painel.Config(self.cfg.dados, senha="segredo", totp_secret=seg)  # "restart"
        self.assertGreaterEqual(novo.totp_ultimo, int(time.time() // 30) - 1)
        self.assertIsNone(painel.totp_confere(novo, cod))  # o código usado não vale de novo

    def test_producao_valida_segredo_totp_e_tamanho_do_token(self):
        class C:
            localhost_livre = False; dominio = "p.exemplo.com"; totp_secret = "isso nao e base32!"; senha = "x" * 20; token = "t" * 40
        self.assertIn("inválido", painel.entrada_insegura(C, True))
        C.totp_secret = "JBSWY3DPEHPK3PXP"
        C.token = "curto"
        self.assertIn("PAINEL_TOKEN", painel.entrada_insegura(C, True))
        C.token = "t" * 32
        self.assertIsNone(painel.entrada_insegura(C, True))

    def test_logout_derruba_os_reles_abertos_da_sessao(self):
        import socket as _s
        a1, a2 = _s.socketpair(); b1, b2 = _s.socketpair()
        self.cfg.reles["TOK"] = [(a1, b1)]
        class H:
            headers = {"Cookie": f"{painel.nome_cookie(self.cfg)}=TOK"}
        painel.encerrar_sessao(self.cfg, H())
        self.assertEqual(a2.recv(10), b"")  # a ponta do navegador foi fechada
        self.assertEqual(b2.recv(10), b"")
        self.assertNotIn("TOK", self.cfg.reles)

    def test_pacote_de_alertas_telegram_usa_https(self):
        self.cfg.dominio = "painel.exemplo.com"
        self.cfg.tg_token, self.cfg.tg_chat = "T", "1"
        enviados = []
        orig = painel.enviar_telegram
        painel.enviar_telegram = lambda cfg, msg: enviados.append(msg) or True
        self.addCleanup(setattr, painel, "enviar_telegram", orig)
        painel.tratar_evento(self.cfg, self.registro, {"maquina": "pc", "motivo": "erro", "texto": "x"}, "painel.exemplo.com")
        self.assertIn("https://painel.exemplo.com/", enviados[0])
        self.assertNotIn("http://", enviados[0])


class TestAchadosRodada2(Base):
    def setUp(self):
        super().setUp()
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})

    def test_rele_so_aceita_o_websocket_e_nunca_devolve_o_token_do_ttyd(self):
        # M1: GET /tty/<m>/<p>/token devolvia base64("tessera:CRED"): o rewrite do frame era cosmético
        import socket as _s
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); porta = srv.getsockname()[1]
        conectou = []
        def ttyd():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                conectou.append(1)
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n{"token": "dGVzc2VyYTpjY2NjY2NjY2NjY2NjY2M="}')
                c.close()
        threading.Thread(target=ttyd, daemon=True).start()
        self.addCleanup(srv.close)
        self.chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {"api": {"dir": "/x", "usuario": "u", "porta": porta, "cred": "cc" * 16, "terminais": {}}}})
        for caminho in ("/tty/pc/api/token", "/tty/pc/api/", "/tty/pc/api/index.html", "/tty/pc/api/ws2", "/tty/pc/api//ws", "/tty/pc/api/../ws"):
            c, corpo, _ = self.chamar("GET", caminho)
            self.assertEqual(c, 404, caminho)
            self.assertNotIn(b"dGVzc2Vy", corpo)
        self.assertEqual(conectou, [])  # nem chegou a conectar no ttyd

    def test_porta_de_projeto_abaixo_de_1024_e_descartada(self):
        self.chamar("POST", "/api/agente/pc", {"projetos": {"api": {"porta": 22, "terminais": {}}, "web": {"porta": 7700, "terminais": {}}}})
        proj = self.cfg.vivo["pc"][""]["payload"]["projetos"]
        self.assertEqual((proj["api"]["porta"], proj["web"]["porta"]), (0, 7700))

    def test_ipv6_com_zona_e_recusado(self):
        for ip in ("fd7a:115c:a1e0::1%x", "fd7a:115c:a1e0::1%x\r\nX-Evil: 1", "fe80::1%eth0"):
            self.assertEqual(self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": ip, "porta": 1})[0], 400, repr(ip))

    def test_auditoria_nao_fica_cega_resume_o_que_suprimiu(self):
        # M5: com o teto de 30/min um brute-force de 1000 tentativas deixava 30 linhas e nenhum sinal
        self.cfg.dominio = "painel.exemplo.com"
        for _ in range(100):
            self.chamar("GET", "/api/estado", headers={"Host": "errado.example"})
        self.cfg.aud_quota.clear()  # passou 1 min
        self.chamar("GET", "/api/estado", headers={"Host": "errado.example"})
        linhas = [json.loads(x) for x in (self.cfg.dados / "auditoria.log").read_text().splitlines()]
        self.assertEqual(sum(l.get("suprimidas", 0) for l in linhas), 100 - 30)

    def test_falhas_nao_criam_entrada_por_ip_que_so_fez_get(self):
        painel._FALHAS.clear()
        painel._NIVEL.clear()
        for i in range(50):
            painel._bloqueado(f"203.0.113.{i}")
        self.assertEqual(len(painel._FALHAS), 0)

    def test_reescrita_do_frame_sobrevive_a_json_profundo(self):
        profundo = ("[" * 1500 + "]" * 1500).encode()
        m = b"\x01\x02\x03\x04"
        frame = bytes([0x82, 0x80 | 126]) + len(profundo).to_bytes(2, "big") + m + bytes(c ^ m[i % 4] for i, c in enumerate(profundo))
        novo, resto = painel.reescrever_primeiro_frame(frame, "T")  # RecursionError não pode vazar
        self.assertEqual(novo, frame)

    def test_convite_leva_o_token_por_env_nao_por_argv(self):
        self.cfg.token = "t" * 32
        c, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova", "sem_root": True})
        cmd = json.loads(corpo)["comando"]
        self.assertIn("TTY_TOKEN=" + painel.token_maquina(self.cfg, "nova") + " bash", cmd)
        self.assertNotIn("--token", cmd)

    def test_convite_com_root_nao_poe_segredo_no_argv_do_sudo(self):
        # L1: "sudo TS_AUTHKEY=... TTY_TOKEN=... bash" deixava os dois no argv do sudo (ps de qualquer usuário local)
        self.cfg.token = "t" * 32
        self.cfg.ts_api_key = "tskey-api-teste"
        orig = painel.criar_authkey
        painel.criar_authkey = lambda cfg, nome: ("tskey-auth-FALSA", "id")
        self.addCleanup(setattr, painel, "criar_authkey", orig)
        c, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova"})
        cmd = json.loads(corpo)["comando"]
        depois_do_sudo = cmd.split("sudo", 1)[1]
        self.assertNotIn("tskey-auth-FALSA", depois_do_sudo)
        self.assertNotIn(painel.token_maquina(self.cfg, "nova"), depois_do_sudo)
        self.assertIn("--preserve-env=TS_AUTHKEY,TTY_TOKEN", cmd)
        self.assertLess(cmd.index("export TS_AUTHKEY=tskey-auth-FALSA"), cmd.index("sudo"))

    def test_servidor_tem_teto_de_conexoes_e_fila_maior(self):
        import socket as _s
        self.assertGreaterEqual(painel.Servidor.request_queue_size, 128)
        srv = painel.Servidor(("127.0.0.1", 0), painel.criar_handler(self.registro, self.cfg))
        srv._vagas = threading.BoundedSemaphore(1)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        porta = srv.server_address[1]
        a = _s.create_connection(("127.0.0.1", porta), timeout=3)  # ocupa a única vaga (conexão parada)
        time.sleep(0.3)
        b = _s.create_connection(("127.0.0.1", porta), timeout=3)
        b.sendall(b"GET /api/maquinas HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(b.recv(100), b"")  # sem vaga: fechada de imediato, não vira thread
        a.close(); b.close()

    def test_rele_com_sessao_inexistente_e_fechado_sem_encaminhar_nada(self):
        # B7: logout concorrente (a sessão já saiu de cfg.sessoes quando o relé vai se registrar) => o relé não pode ficar vivo
        import socket as _s
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); porta = srv.getsockname()[1]
        def ttyd():
            try:
                c, _ = srv.accept()
                c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                time.sleep(1)
                c.close()
            except OSError:
                pass
        threading.Thread(target=ttyd, daemon=True).start()
        self.addCleanup(srv.close)
        self.chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {"api": {"dir": "/x", "usuario": "u", "porta": porta, "cred": "cd" * 16, "terminais": {}}}})
        host, p = self.base.split("//")[1].split(":")
        c = _s.create_connection((host, int(p)), timeout=5)
        c.sendall(f"GET /tty/pc/api/ws?arg=main HTTP/1.1\r\nHost: {host}:{p}\r\nCookie: Tessera=SESSAO-QUE-NAO-EXISTE\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        self.assertEqual(c.recv(4096), b"")  # fechado: nem o 101 do ttyd foi repassado
        self.assertNotIn("SESSAO-QUE-NAO-EXISTE", self.cfg.reles)


class TestPrimeiroFrameDoRele(Base):
    """O AuthToken do ttyd é posto pelo painel no 1º frame do navegador (o segredo nunca vai ao navegador)."""

    @staticmethod
    def frame(obj, opcode=2, mascara=b"\x01\x02\x03\x04", forcar_len=None):
        carga = json.dumps(obj).encode()
        n = len(carga)
        cab = bytes([0x80 | opcode])
        if n < 126 and forcar_len is None:
            cab += bytes([0x80 | n])
        else:
            cab += bytes([0x80 | 126]) + __import__("struct").pack(">H", n)
        return cab + mascara + bytes(c ^ mascara[i % 4] for i, c in enumerate(carga))

    @staticmethod
    def decodifica(frame):
        n, pos = frame[1] & 0x7F, 2
        if n == 126:
            n, pos = __import__("struct").unpack(">H", frame[2:4])[0], 4
        m = frame[pos:pos + 4]
        return json.loads(bytes(c ^ m[i % 4] for i, c in enumerate(frame[pos + 4:pos + 4 + n])))

    def test_troca_o_authtoken_e_preserva_colunas_e_linhas(self):
        novo, resto = painel.reescrever_primeiro_frame(self.frame({"AuthToken": "", "columns": 80, "rows": 24}), "TOKEN123")
        self.assertEqual(self.decodifica(novo), {"AuthToken": "TOKEN123", "columns": 80, "rows": 24})
        self.assertEqual(resto, b"")

    def test_token_do_navegador_e_sobrescrito_nao_somado(self):
        novo, _ = painel.reescrever_primeiro_frame(self.frame({"AuthToken": "forjado-pelo-navegador", "columns": 1, "rows": 1}), "REAL")
        self.assertEqual(self.decodifica(novo)["AuthToken"], "REAL")

    def test_frames_seguintes_colados_no_mesmo_pacote_passam_intactos(self):
        extra = b"\x82\x81ABCDxyz"  # teclas já enviadas junto
        novo, resto = painel.reescrever_primeiro_frame(self.frame({"AuthToken": "", "columns": 80, "rows": 24}) + extra, "T")
        self.assertEqual(resto, extra)

    def test_frame_incompleto_espera_mais_bytes_em_qualquer_corte(self):
        f = self.frame({"AuthToken": "", "columns": 80, "rows": 24})
        for corte in range(0, len(f)):
            self.assertIsNone(painel.reescrever_primeiro_frame(f[:corte], "T"), corte)
        self.assertIsNotNone(painel.reescrever_primeiro_frame(f, "T"))

    def test_comprimento_de_16_bits_funciona(self):
        novo, _ = painel.reescrever_primeiro_frame(self.frame({"AuthToken": "", "columns": 80, "rows": 24}, forcar_len=True), "T")
        self.assertEqual(self.decodifica(novo)["AuthToken"], "T")

    def test_frame_que_nao_e_json_ou_nao_mascarado_passa_sem_mexer(self):
        sem_mascara = bytes([0x82, 0x03]) + b"abc"
        self.assertEqual(painel.reescrever_primeiro_frame(sem_mascara, "T"), (sem_mascara, b""))
        lixo = bytes([0x82, 0x83]) + b"\x00\x00\x00\x00" + b"abc"
        self.assertEqual(painel.reescrever_primeiro_frame(lixo, "T")[0], lixo)

    def test_frame_gigante_nao_e_bufferizado(self):
        gigante = bytes([0x82, 0x80 | 127]) + (10**6).to_bytes(8, "big") + b"\x00" * 4
        self.assertEqual(painel.reescrever_primeiro_frame(gigante, "T"), (gigante, b""))

    def test_integracao_ttyd_recebe_o_authtoken_e_o_navegador_nunca_o_viu(self):
        import socket as _s
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); porta = srv.getsockname()[1]
        visto = {}
        def ttyd():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            visto["req"] = req
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            dados = b""
            c.settimeout(3)
            try:
                while len(dados) < 30:
                    p = c.recv(4096)
                    if not p:
                        break
                    dados += p
            except OSError:
                pass
            visto["frame"] = dados
            c.close()
        threading.Thread(target=ttyd, daemon=True).start()
        self.addCleanup(srv.close)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        cred = "cd" * 16
        self.chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {"api": {"dir": "/x", "usuario": "u", "porta": porta, "cred": cred, "terminais": {}}}})
        host, p = self.base.split("//")[1].split(":")
        c = _s.create_connection((host, int(p)), timeout=5)
        c.sendall(f"GET /tty/pc/api/ws?arg=main HTTP/1.1\r\nHost: {host}:{p}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += c.recv(4096)
        self.assertIn(b"101", resp)
        c.sendall(TestPrimeiroFrameDoRele.frame({"AuthToken": "", "columns": 80, "rows": 24}))
        time.sleep(1)
        c.close()
        esperado = base64.b64encode(("tessera:" + cred).encode()).decode()
        self.assertEqual(TestPrimeiroFrameDoRele.decodifica(visto["frame"])["AuthToken"], esperado)
        self.assertIn(b"Authorization: Basic " + esperado.encode(), visto["req"])


class TestDosDoServidor(Base):
    """Rodada 4: o teto global de conexões deu a UM atacante o poder de derrubar o painel (gotejando bytes). Camadas: teto por IP,
    prazo total da requisição e vaga devolvida sempre."""

    def servidor(self, **attrs):
        srv = painel.Servidor(("127.0.0.1", 0), painel.criar_handler(self.registro, self.cfg))
        for k, v in attrs.items():
            setattr(srv, k, v)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return srv, srv.server_address[1]

    def test_um_ip_nao_esgota_as_vagas_dos_outros(self):
        import socket as _s
        srv, porta = self.servidor(MAX_POR_IP=3)
        presos = [_s.create_connection(("127.0.0.1", porta), timeout=3) for _ in range(3)]
        time.sleep(0.3)
        extra = _s.create_connection(("127.0.0.1", porta), timeout=3)
        extra.sendall(b"GET /api/maquinas HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(extra.recv(100), b"")  # 4ª conexão do MESMO IP: fechada na hora
        self.assertGreater(srv._vagas._value, 200)  # e o resto do painel segue com vagas de sobra (o teto global não foi consumido)
        for p in presos:
            p.close()
        time.sleep(0.6)
        self.assertEqual(srv._por_ip, {})  # as vagas voltam quando as conexões caem
        ok = _s.create_connection(("127.0.0.1", porta), timeout=3)
        ok.sendall(b"GET /api/maquinas HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"HTTP/", ok.recv(100))  # o dono volta a ser atendido

    def test_gotejamento_de_bytes_nao_segura_a_vaga(self):
        import socket as _s
        orig = painel.PRAZO_CABECALHO_S
        painel.PRAZO_CABECALHO_S = 1
        self.addCleanup(setattr, painel, "PRAZO_CABECALHO_S", orig)
        srv, porta = self.servidor()
        c = _s.create_connection(("127.0.0.1", porta), timeout=6)
        t0 = time.time()
        fechou = False
        try:
            for b in b"GET /api/maquinas HT":  # 1 byte a cada 0,4 s: o timeout POR OPERAÇÃO (30 s) nunca dispararia
                c.sendall(bytes([b]))
                time.sleep(0.4)
        except OSError:
            fechou = True
        if not fechou:
            fechou = c.recv(10) == b""
        self.assertTrue(fechou)
        self.assertLess(time.time() - t0, 4)  # derrubado pelo prazo TOTAL (~1 s), não pelos 30 s por operação
        time.sleep(0.3)
        self.assertEqual(srv._por_ip, {})

    def test_requisicao_normal_nao_e_cortada_pelo_prazo(self):
        import socket as _s
        srv, porta = self.servidor()
        c = _s.create_connection(("127.0.0.1", porta), timeout=3)
        c.sendall(b"GET /api/maquinas HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"200", c.recv(100))

    def test_rele_de_terminal_nao_tem_prazo_total(self):
        import socket as _s
        a, b = painel.PRAZO_CABECALHO_S, painel.PRAZO_CORPO_S
        painel.PRAZO_CABECALHO_S = painel.PRAZO_CORPO_S = 1
        self.addCleanup(lambda: (setattr(painel, "PRAZO_CABECALHO_S", a), setattr(painel, "PRAZO_CORPO_S", b)))
        ttyd = _s.socket(); ttyd.bind(("127.0.0.1", 0)); ttyd.listen(1); porta_ttyd = ttyd.getsockname()[1]
        visto = {}
        def servidor_ttyd():
            c, _ = ttyd.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            c.settimeout(5)
            try:
                visto["dados"] = c.recv(100)
            except OSError:
                visto["dados"] = b""
            c.close()
        threading.Thread(target=servidor_ttyd, daemon=True).start()
        self.addCleanup(ttyd.close)
        self.chamar("POST", "/api/registrar", {"nome": "pc", "ip": "127.0.0.1", "porta": 1})
        self.chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {"api": {"dir": "/x", "usuario": "u", "porta": porta_ttyd, "terminais": {}}}})
        srv, porta = self.servidor()
        c = _s.create_connection(("127.0.0.1", porta), timeout=6)
        c.sendall(b"GET /tty/pc/api/ws?arg=main HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += c.recv(4096)
        time.sleep(2.5)  # bem mais que os prazos de 1 s: o terminal aberto segue vivo
        c.sendall(b"\x82\x85abcdexxxxx")
        time.sleep(0.5)
        self.assertEqual(visto.get("dados"), b"\x82\x85abcdexxxxx")

    def test_vaga_volta_se_a_thread_nao_puder_ser_criada(self):
        # B4: "can't start new thread" depois do acquire vazava a vaga
        import socket as _s
        srv = painel.Servidor(("127.0.0.1", 0), painel.criar_handler(self.registro, self.cfg))
        self.addCleanup(srv.server_close)
        srv._vagas = threading.BoundedSemaphore(1)
        orig = painel.ThreadingHTTPServer.process_request
        painel.ThreadingHTTPServer.process_request = lambda self_, r, a: (_ for _ in ()).throw(RuntimeError("can't start new thread"))
        self.addCleanup(setattr, painel.ThreadingHTTPServer, "process_request", orig)
        a, b = _s.socketpair()
        with self.assertRaises(RuntimeError):
            srv.process_request(a, ("203.0.113.5", 1))
        self.assertTrue(srv._vagas.acquire(blocking=False))  # a vaga voltou
        self.assertEqual(srv._por_ip, {})

    def test_corte_pelo_prazo_nao_infla_o_journal_com_traceback(self):
        import io, contextlib, socket as _s
        orig = painel.PRAZO_CABECALHO_S
        painel.PRAZO_CABECALHO_S = 0.5
        self.addCleanup(setattr, painel, "PRAZO_CABECALHO_S", orig)
        srv, porta = self.servidor()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(5):
                c = _s.create_connection(("127.0.0.1", porta), timeout=3)
                c.sendall(b"GET /api/mac")
                time.sleep(0.9)
                c.close()
            time.sleep(0.3)
        self.assertNotIn("Traceback", err.getvalue())

    def test_erro_de_verdade_do_servidor_continua_com_traceback(self):
        import io, contextlib
        srv = painel.Servidor(("127.0.0.1", 0), painel.criar_handler(self.registro, self.cfg))
        self.addCleanup(srv.server_close)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise KeyError("bug nosso")
            except KeyError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("KeyError", err.getvalue())  # só OSError/ValueError são silenciados; bug nosso aparece

    def test_prazo_cresce_com_o_upload_e_o_teto_por_ip_comporta_os_terminais(self):
        self.assertGreaterEqual(painel.MAX_CONEXOES_POR_IP, 64)
        armados = []
        h = type("H", (), {})()
        # o prazo para o corpo = base + Content-Length / 100000 (50 MB => ~620 s)
        cls = painel.criar_handler(self.registro, self.cfg)
        inst = cls.__new__(cls)
        inst.headers = {"Content-Length": str(50 * 1024 * 1024)}
        inst._armar = lambda seg: armados.append(seg)
        inst.close_connection = True
        orig = painel.BaseHTTPRequestHandler.parse_request
        painel.BaseHTTPRequestHandler.parse_request = lambda self_: True
        self.addCleanup(setattr, painel.BaseHTTPRequestHandler, "parse_request", orig)
        self.assertTrue(inst.parse_request())
        self.assertGreater(armados[-1], painel.PRAZO_CORPO_S + 500)

    def test_pausa_do_2fa_nao_enche_a_auditoria(self):
        self.assertIn("2fa-forca-bruta-pausado", painel.AUDITORIA_RUIDO)

    def test_convite_valida_a_key_da_tailscale_e_nao_deixa_segredo_no_shell(self):
        self.cfg.token = "t" * 32
        self.cfg.ts_api_key = "tskey-api-teste"
        orig = painel.criar_authkey
        self.addCleanup(setattr, painel, "criar_authkey", orig)
        painel.criar_authkey = lambda cfg, nome: ("tskey-auth-abc123", "id")
        _, corpo, _ = self.chamar("POST", "/api/convite", {"nome": "nova"})
        cmd = json.loads(corpo)["comando"]
        self.assertTrue(cmd.rstrip().endswith("; unset TS_AUTHKEY TTY_TOKEN"), cmd[-60:])  # nada exportado depois de rodar
        for ruim in ("tskey-x; rm -rf ~", "tskey-x$(id)", "x y", "`id`", "tskey-"):
            painel.criar_authkey = lambda cfg, nome, k=ruim: (k, "id")
            self.assertEqual(self.chamar("POST", "/api/convite", {"nome": "nova"})[0], 502, ruim)


if __name__ == "__main__":
    unittest.main()


class TestPlanoAgenteFechado(unittest.TestCase):
    """Antes era uma "ponte de migração" (sem PAINEL_TOKEN o plano-agente na tailnet passava). Foi fechada: não há mais agentes antigos."""
    def test_sem_token_plano_agente_e_401_e_com_token_passa(self):
        import tempfile as _t, threading as _th
        from http.server import ThreadingHTTPServer as _S
        from pathlib import Path as _P
        tmp = _t.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dados = _P(tmp.name)
        cfg = painel.Config(dados, senha="segredo", sincrono=True, localhost_livre=True, raiz=_P(tmp.name) / "raiz")
        cfg.token = ""  # sem PAINEL_TOKEN configurado
        for pasta in ("bootstrap", "facilitadores", "notificador", "projetos", "agente"):
            (cfg.raiz / pasta).mkdir(parents=True)
        (cfg.raiz / "bootstrap" / "bootstrap.sh").write_text("x")
        reg = painel.Registro(dados / "m.json")
        painel._FALHAS.clear()
        painel._NIVEL.clear()
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

        # sem token configurado: nenhum endpoint do plano-agente abre na tailnet (era 200 na ponte)
        self.assertEqual(chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}), 401)
        self.assertEqual(chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {}}), 401)
        self.assertEqual(chamar("POST", "/api/evento", {}), 401)
        self.assertEqual(chamar("GET", "/pacote.tar.gz"), 401)
        # Bearer só vale quando o painel TEM token e ele confere
        cfg.token = "t" * 32
        auth = {"Authorization": "Bearer " + painel.token_maquina(cfg, "pc")}
        self.assertEqual(chamar("POST", "/api/registrar", {"nome": "pc", "ip": "100.1.1.1", "porta": 1}, auth), 200)
        self.assertEqual(chamar("POST", "/api/agente/pc", {"cores": 1, "ram_mb": 1, "projetos": {}}, {"Authorization": "Bearer errado"}), 401)
        cfg.token = ""
        painel._FALHAS.clear()  # o foco aqui é o token; o limite por IP (5) já foi gasto pelas 401 acima
        painel._NIVEL.clear()
        self.assertEqual(chamar("POST", "/api/tarefas", {"maquina": "pc", "tipo": "projeto.rm", "args": {"nome": "x"}}), 401)
        self.assertEqual(chamar("POST", "/api/convite", {"nome": "x"}), 401)


class TestAtrasDoProxy(Base):
    """Produção na VPS: a ÚNICA entrada é Caddy -> painel em loopback. IP:porta direto nunca pode atender, e os limites por IP
    precisam enxergar o cliente real (senão um atacante trava o login do dono, que viria do mesmo 127.0.0.1)."""

    def setUp(self):
        super().setUp()
        self.cfg.atras_proxy = True
        self.cfg.localhost_livre = False

    def test_sem_x_forwarded_for_conexao_e_descartada_nada_atende_direto(self):
        with self.assertRaises((urllib.error.URLError, ConnectionError)):
            urllib.request.urlopen(self.base + "/api/estado", timeout=5)

    def _erra_login(self, xff, n=15):
        self.cfg.totp_secret = "JBSWY3DPEHPK3PXP"
        self.cfg.senha = "s" * 24
        return [self.chamar("POST", "/api/login", {"senha": "errada", "codigo": "000000"}, {"X-Forwarded-For": xff})[0] for _ in range(n)]

    def test_com_proxy_o_login_errado_bloqueia_so_o_ip_do_cliente_real(self):
        self.assertIn(429, self._erra_login("198.51.100.7"))  # o atacante é travado...
        # ...mas outro cliente real, pelo mesmo proxy (mesmo 127.0.0.1), não herda o bloqueio
        self.assertNotEqual(self.chamar("POST", "/api/login", {"senha": "x", "codigo": "1"}, {"X-Forwarded-For": "203.0.113.9"})[0], 429)

    def test_xff_forjado_pelo_cliente_vale_so_o_ultimo_valor_do_proxy(self):
        # o atacante manda "X-Forwarded-For: 203.0.113.9" (a vítima); o Caddy anexa o IP real do atacante no fim
        self.assertIn(429, self._erra_login("203.0.113.9, 198.51.100.7"))
        self.assertNotEqual(self.chamar("POST", "/api/login", {"senha": "x", "codigo": "1"}, {"X-Forwarded-For": "203.0.113.9"})[0], 429)

    def test_conexao_keepalive_do_proxy_atende_varios_clientes_em_sequencia(self):
        # o Caddy reaproveita a conexão com o painel entre clientes diferentes: o 2º pedido não pode ser descartado
        # (a checagem de loopback olha o par do socket, não o client_address já reescrito pelo pedido anterior)
        import http.client
        self.srv.RequestHandlerClass.protocol_version = "HTTP/1.1"  # hoje o painel é HTTP/1.0; o teste vale se um dia ligarem keep-alive
        c = http.client.HTTPConnection(self.base.split("//")[1], timeout=5)
        for ip in ("198.51.100.7", "203.0.113.9", "198.51.100.8"):
            c.request("GET", "/api/estado", headers={"X-Forwarded-For": ip})
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 401, ip)

    def test_producao_atras_do_proxy_recusa_bind_publico(self):
        class C:
            localhost_livre = False; atras_proxy = True; dominio = "t.exemplo.com"; totp_secret = "JBSWY3DPEHPK3PXP"; senha = "x" * 20
        self.assertIn("127.0.0.1", painel.entrada_insegura(C, True, "0.0.0.0"))
        self.assertIsNone(painel.entrada_insegura(C, True, "127.0.0.1"))
