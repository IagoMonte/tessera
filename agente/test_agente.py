import os
import tempfile
import unittest
from pathlib import Path

import agente


def pid_stat(proc, pid, comm, ppid, utime, stime, rss_pages):
    d = Path(proc) / str(pid)
    d.mkdir(parents=True)
    campos = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 8 + [str(rss_pages)]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(campos) + " 0 0\n")


def escreve_stat_global(proc, ocupado, ocioso):
    (Path(proc) / "stat").write_text(f"cpu {ocupado} 0 0 {ocioso} 0 0 0 0 0 0\ncpu0 1 1 1 1\n")


class TestProc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)
        pid_stat(self.proc, 100, "tmux: server", 1, 10, 0, 1000)
        pid_stat(self.proc, 101, "bash (x) y", 100, 5, 5, 500)       # comm com espaço e parênteses
        pid_stat(self.proc, 102, "node", 101, 100, 0, 2000)
        pid_stat(self.proc, 200, "outro", 1, 1, 1, 100)
        (self.proc / "meminfo").write_text("MemTotal:       8192000 kB\nMemFree: 1000 kB\nMemAvailable:   2048000 kB\n")
        escreve_stat_global(self.proc, 100, 900)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_com_comm_estranho(self):
        snap = agente.snapshot_proc(self.proc)
        self.assertEqual(snap[101][0], 100)          # ppid
        self.assertEqual(snap[101][1], 10)           # utime+stime
        self.assertEqual(snap[101][2], 500 * agente.PAGINA_KB)

    def test_arvore(self):
        snap = agente.snapshot_proc(self.proc)
        self.assertEqual(agente.arvore(snap, 100), {100, 101, 102})
        self.assertEqual(agente.arvore(snap, 999), set())

    def test_meminfo(self):
        total, usado = agente.ler_meminfo(self.proc)
        self.assertEqual((total, usado), (8000, 6000))

    def test_cpu_por_diferenca(self):
        med = agente.Medidor(self.proc)
        med.amostrar(agora=0.0)
        pid_stat_atualiza = self.proc / "102" / "stat"
        txt = pid_stat_atualiza.read_text().replace(" 100 0 ", f" {100 + agente.CLK_TCK} 0 ", 1)
        pid_stat_atualiza.write_text(txt)               # +1 s de CPU em 2 s => 50%
        escreve_stat_global(self.proc, 300, 1700)       # +200 ocupado de +1000 total => 20%
        cpu_maq = med.amostrar(agora=2.0)
        self.assertEqual(cpu_maq, 20.0)
        cpu, mem = med.uso(100)
        self.assertAlmostEqual(cpu, 50.0, places=1)
        self.assertEqual(mem, round((1000 + 500 + 2000) * agente.PAGINA_KB / 1024))

    def test_coletar_agrupa_por_projeto_e_terminal(self):
        med = agente.Medidor(self.proc)
        med.amostrar(agora=0.0)
        med.amostrar(agora=1.0)
        projetos = {"api": {"dir": "/srv/api", "usuario": "u1", "porta": 7700},
                    "web": {"dir": "/srv/web", "usuario": "u2", "porta": 7701}}
        sessoes = {"u1": [("api/main", 101, "bash", 990), ("api/t2", 102, "node", 999), ("outro/x", 200, "zsh", 1), ("api/ruim.nome", 200, "sh", 1)],
                   "u2": [("web/main", 200, "zsh", 0)]}
        r = agente.coletar(med, projetos, sessoes_fn=lambda u: sessoes[u], branch_fn=lambda d, u: "main", agora=1000)
        api = r["projetos"]["api"]
        self.assertEqual(sorted(api["terminais"]), ["main", "t2"])
        self.assertEqual(api["terminais"]["t2"]["cmd"], "node")
        self.assertEqual(api["terminais"]["main"]["atividade_s"], 10)
        self.assertEqual(api["branch"], "main")
        self.assertEqual(r["projetos"]["web"]["terminais"]["main"]["atividade_s"], 1000)
        self.assertEqual(r["ram_mb"], 8000)


class TestTarefas(unittest.TestCase):
    P = {"api": {"dir": "/srv/api", "usuario": "dev", "porta": 7700}}

    def mc(self, tipo, **args):
        return agente.montar_comando({"tipo": tipo, "args": args}, self.P, sessoes=("api/main", "api/t2"))

    def test_validas(self):
        self.assertEqual(self.mc("projeto.add", nome="x", dir="/srv/x", usuario="dev", porta=7705)[0],
                         ["tty-projeto", "add", "x", "--dir", "/srv/x", "--usuario", "dev", "--porta", "7705"])
        self.assertEqual(self.mc("projeto.rm", nome="x")[0], ["tty-projeto", "rm", "x"])
        self.assertEqual(self.mc("terminal.novo", projeto="api"),
                         (["tmux", "new-session", "-d", "-s", "api/t3", "-c", "/srv/api"], "dev"))
        self.assertEqual(self.mc("terminal.novo", projeto="api", terminal="logs")[0][4], "api/logs")
        self.assertEqual(self.mc("terminal.encerrar", projeto="api", terminal="t2"),
                         (["tmux", "kill-session", "-t", "=api/t2"], "dev"))
        self.assertEqual(self.mc("terminal.limpar", projeto="api"),
                         (["tmux", "limpar", "api"], "dev"))
        self.assertEqual(self.mc("ferramenta.add", usuario="dev", ferramenta="claude", sem_config=True),
                         (["tty-add", "claude", "--sem-config"], "dev"))
        self.assertEqual(self.mc("ferramenta.push", usuario="dev", ferramenta="opencode"),
                         (["tty-push", "opencode"], "dev"))

    def test_recusa_injecao_e_abuso(self):
        invalidas = [
            ("projeto.add", dict(nome="x; rm -rf /", dir="/srv/x")),
            ("projeto.add", dict(nome="x", dir="relativo")),
            ("projeto.add", dict(nome="x", dir="/srv/x\nrm")),
            ("projeto.add", dict(nome="x", dir="/srv/x", porta=80)),
            ("projeto.add", dict(nome="x", dir="/srv/x", porta=True)),
            ("projeto.rm", dict(nome="../x")),
            ("terminal.novo", dict(projeto="nao-existe")),
            ("terminal.novo", dict(projeto="api", terminal="a;b")),
            ("terminal.encerrar", dict(projeto="api", terminal="$(id)")),
            ("terminal.limpar", dict(projeto="nao-existe")),
            ("terminal.limpar", dict(projeto="api; rm")),
            ("terminal.limpar", dict()),
            ("ferramenta.add", dict(usuario="root", ferramenta="claude")),
            ("ferramenta.add", dict(usuario="dev", ferramenta="../x")),
            ("ferramenta.add", dict(usuario="dev", ferramenta="a b")),
            ("shell.exec", dict(cmd="id")),
        ]
        for tipo, args in invalidas:
            with self.subTest(tipo=tipo, args=args):
                with self.assertRaises(agente.TarefaInvalida):
                    self.mc(tipo, **args)

    def test_args_nao_dict(self):
        with self.assertRaises(agente.TarefaInvalida):
            agente.montar_comando({"tipo": "projeto.rm", "args": ["x"]}, self.P)

    def test_executar_recusada_nao_levanta(self):
        r = agente.executar({"id": 7, "tipo": "shell.exec", "args": {}}, self.P)
        self.assertEqual((r["id"], r["ok"]), (7, False))
        self.assertIn("recusada", r["saida"])


class TestLimpar(unittest.TestCase):
    P = {"api": {"dir": "/srv/api", "usuario": "dev", "porta": 7700}}

    def _projetos_meus(self):
        return {"api": {"dir": "/srv/api", "usuario": agente.usuario_atual(), "porta": 7700}}

    def test_mata_uma_a_uma_e_resume(self):
        chamadas = []
        antes = (agente.como, agente.rodar)
        agente.como = lambda u, argv: ["COMO:" + u] + argv
        agente.rodar = lambda argv, timeout=10: (chamadas.append(argv) or (1, "can't find session"))
        try:
            r = agente.executar({"id": "L", "tipo": "terminal.limpar", "args": {"projeto": "api"}},
                                self._projetos_meus(), sessoes=["api/main", "api/t2", "web/main"])
        finally:
            agente.como, agente.rodar = antes
        eu = "COMO:" + agente.usuario_atual()
        self.assertEqual((r["id"], r["ok"]), ("L", True))  # sessão ausente não é falha
        self.assertEqual(chamadas, [[eu, "tmux", "kill-session", "-t", "=api/main"],
                                    [eu, "tmux", "kill-session", "-t", "=api/t2"]])

    def test_falha_real_marca_erro(self):
        antes = (agente.como, agente.rodar)
        agente.como = lambda u, argv: argv
        agente.rodar = lambda argv, timeout=10: (1, "permission denied")
        try:
            r = agente.executar({"id": "L", "tipo": "terminal.limpar", "args": {"projeto": "api"}},
                                self._projetos_meus(), sessoes=["api/main"])
        finally:
            agente.como, agente.rodar = antes
        self.assertEqual((r["id"], r["ok"]), ("L", False))
        self.assertIn("api/main", r["saida"])

    def test_sem_sessoes_nada_a_limpar(self):
        r = agente.executar({"id": "L", "tipo": "terminal.limpar", "args": {"projeto": "api"}}, self._projetos_meus(), sessoes=[])
        self.assertEqual((r["ok"], r["saida"]), (True, "nada a limpar"))


class TestFerramentas(unittest.TestCase):
    def test_disponiveis_le_bin_sem_executar_e_ignora_modelo(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "claude.sh").write_text('BIN=claude\nDESC="x"\ninstalar() { touch /tmp/NAO_DEVE_EXECUTAR; }\n')
            (Path(t) / "_modelo.sh").write_text("BIN=modelo\n")
            (Path(t) / "ruim.sh").write_text("BIN=a;b\n")
            (Path(t) / "Maiuscula.sh").write_text("BIN=x\n")
            self.assertEqual(agente.ferramentas_disponiveis(t), {"claude": "claude"})
            self.assertFalse(Path("/tmp/NAO_DEVE_EXECUTAR").exists())

    def test_instaladas_no_path_do_usuario(self):
        r = agente.ferramentas_instaladas(agente.usuario_atual(), {"sh": "sh", "nada": "binario-que-nao-existe-xyz"})
        self.assertEqual(r, {"sh": True, "nada": False})
        self.assertEqual(agente.ferramentas_instaladas(agente.usuario_atual(), {}), {})


class TestPastas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        for n in ("a", "b", ".oculta"):
            (self.raiz / n).mkdir()
        (self.raiz / "arquivo.txt").write_text("x")
        (self.raiz / "link").symlink_to(self.raiz / "a", target_is_directory=True)
        (self.raiz / "link-arquivo").symlink_to(self.raiz / "arquivo.txt")
        (self.raiz / "quebrado").symlink_to(self.raiz / "nao-existe")

    def tearDown(self):
        self.tmp.cleanup()

    def test_so_pastas_sem_ocultas_e_segue_link_para_pasta(self):
        r = agente.listar_subpastas(str(self.raiz))
        self.assertTrue(r["ok"])
        self.assertEqual(r["pastas"], ["a", "b", "link"])  # sem arquivo, sem link de arquivo, sem link quebrado, sem .oculta
        self.assertEqual((r["total"], r["truncado"], r["dir"]), (3, False, str(self.raiz)))

    def test_ocultas_quando_pedido(self):
        self.assertEqual(agente.listar_subpastas(str(self.raiz), ocultas=True)["pastas"], [".oculta", "a", "b", "link"])

    def test_limite_e_tamanho(self):
        self.assertEqual(agente.listar_subpastas(str(self.raiz), maximo=2)["pastas"], ["a", "b"])
        self.assertTrue(agente.listar_subpastas(str(self.raiz), maximo=2)["truncado"])
        antigo = agente.MAX_DADOS_BYTES
        agente.MAX_DADOS_BYTES = 120
        try:
            for i in range(40):
                (self.raiz / f"pasta-com-nome-longo-{i:03d}").mkdir()
            r = agente.listar_subpastas(str(self.raiz))
            self.assertTrue(r["truncado"])
            self.assertLessEqual(len(__import__("json").dumps(r)), 120)
        finally:
            agente.MAX_DADOS_BYTES = antigo

    def test_erros_claros(self):
        self.assertIn("absoluto", agente.listar_subpastas("relativo/x")["erro"])
        self.assertIn("inválido", agente.listar_subpastas("/tmp/a\0b")["erro"])
        self.assertIn("inválido", agente.listar_subpastas("")["erro"])
        self.assertIn("não existe", agente.listar_subpastas(str(self.raiz / "nao-existe"))["erro"])
        self.assertIn("não é uma pasta", agente.listar_subpastas(str(self.raiz / "arquivo.txt"))["erro"])

    @unittest.skipIf(os.geteuid() == 0, "root lê tudo")
    def test_sem_permissao(self):
        (self.raiz / "a").chmod(0)
        try:
            self.assertIn("sem permissão", agente.listar_subpastas(str(self.raiz / "a"))["erro"])
        finally:
            (self.raiz / "a").chmod(0o755)

    def test_til_expande_pelo_home_do_usuario(self):
        antigo = os.environ.get("HOME")
        os.environ["HOME"] = str(self.raiz)
        try:
            r = agente.listar_subpastas("~")
            self.assertEqual((r["ok"], r["dir"], r["home"]), (True, str(self.raiz), str(self.raiz)))
            self.assertEqual(agente.listar_subpastas("~/a")["dir"], str(self.raiz / "a"))
            self.assertIn("absoluto", agente.listar_subpastas("~outro")["erro"])  # só ~ e ~/..., nunca ~usuario
        finally:
            os.environ["HOME"] = antigo

    def test_normaliza_pontos(self):
        self.assertEqual(agente.listar_subpastas(str(self.raiz / "a" / ".." / "b"))["dir"], str(self.raiz / "b"))


class TestTarefaPastaListar(unittest.TestCase):
    P = {"api": {"dir": "/srv/api", "usuario": "dev", "porta": 7700}}

    def test_monta_argv_sem_shell_e_roteia_pelo_usuario(self):
        argv, usuario = agente.montar_comando({"tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/srv/x y"}}, self.P)
        self.assertEqual(usuario, "dev")
        self.assertEqual(argv[2:], ["--listar", "/srv/x y"])
        argv, _ = agente.montar_comando({"tipo": "pasta.listar", "args": {"usuario": "dev", "dir": "/srv", "ocultas": True}}, self.P)
        self.assertEqual(argv[-1], "--ocultas")

    def test_recusa_abuso(self):
        for args in ({"usuario": "root", "dir": "/"}, {"usuario": "dev"}, {"usuario": "dev", "dir": ""},
                     {"usuario": "dev", "dir": "/a\0b"}, {"usuario": "dev", "dir": "x" * 5000}, {"usuario": "d e", "dir": "/"}):
            with self.subTest(args=args):
                with self.assertRaises(agente.TarefaInvalida):
                    agente.montar_comando({"tipo": "pasta.listar", "args": args}, self.P)

    def test_executar_de_verdade_devolve_dados(self):
        with tempfile.TemporaryDirectory() as t:
            for n in ("um", "dois"):
                (Path(t) / n).mkdir()
            projetos = {"p": {"dir": t, "usuario": agente.usuario_atual(), "porta": 7700}}
            r = agente.executar({"id": "t1", "tipo": "pasta.listar", "args": {"usuario": agente.usuario_atual(), "dir": t}}, projetos)
            self.assertEqual((r["id"], r["ok"], r["saida"]), ("t1", True, ""))
            self.assertEqual(r["dados"]["pastas"], ["dois", "um"])
            r = agente.executar({"id": "t2", "tipo": "pasta.listar", "args": {"usuario": agente.usuario_atual(), "dir": t + "/nao"}}, projetos)
            self.assertEqual((r["ok"], r["saida"]), (False, "a pasta não existe"))
            self.assertFalse(r["dados"]["ok"])


@unittest.skipIf(os.geteuid() == 0, "o agente root não age pelo próprio usuário")
class TestAgenteSemProjetos(unittest.TestCase):
    """Um agente sem root continua útil depois que o último projeto dele foi removido."""

    def test_o_proprio_usuario_e_permitido_mesmo_sem_projetos(self):
        eu = agente.usuario_atual()
        self.assertEqual(agente.usuarios_permitidos({}), {eu})
        self.assertEqual(agente.usuarios_permitidos({"p": {"usuario": "outro"}}), {eu, "outro"})

    def test_sistema_atualizar_roda_tty_atualizar_sem_argumentos(self):
        self.assertEqual(agente.montar_comando({"tipo": "sistema.atualizar", "args": {}}, {}), (["tty-atualizar"], None))

    def test_tarefas_de_ferramenta_e_pasta_aceitam_o_proprio_usuario_sem_projetos(self):
        eu = agente.usuario_atual()
        self.assertEqual(agente.montar_comando({"tipo": "ferramenta.add", "args": {"usuario": eu, "ferramenta": "opencode"}}, {})[0], ["tty-add", "opencode"])
        self.assertEqual(agente.montar_comando({"tipo": "ferramenta.push", "args": {"usuario": eu, "ferramenta": "claude"}}, {})[0], ["tty-push", "claude"])
        self.assertEqual(agente.montar_comando({"tipo": "pasta.listar", "args": {"usuario": eu, "dir": "/tmp"}}, {})[1], eu)

    def test_continua_recusando_outros_usuarios(self):
        with self.assertRaises(agente.TarefaInvalida):
            agente.montar_comando({"tipo": "ferramenta.add", "args": {"usuario": "root", "ferramenta": "claude"}}, {})

    def test_ferramentas_instaladas_usa_o_proprio_usuario_sem_projetos(self):
        antes = (agente.projetos_locais, agente.ferramentas_disponiveis)
        agente.projetos_locais = lambda etc=None: {}
        agente.ferramentas_disponiveis = lambda d=None: {"sh": "sh", "nada": "binario-que-nao-existe-xyz"}
        try:
            self.assertEqual(agente.Agente("http://x", "m").ferramentas(), {"sh": True, "nada": False})
        finally:
            agente.projetos_locais, agente.ferramentas_disponiveis = antes


class TestConsultaDeTarefas(unittest.TestCase):
    def test_consulta_leve_entrega_resultados_e_despacha(self):
        a = agente.Agente("http://x", "maq")
        chamadas = []
        a._enviar = lambda caminho, corpo, res: chamadas.append((caminho, corpo, res)) or {"tarefas": [{"id": "1"}]}
        despachadas = []
        a._despachar = lambda resp: despachadas.append(resp)
        a.resultados = [{"id": "0", "ok": True}]
        a.consultar_tarefas()
        self.assertEqual(chamadas[0][0], "/api/agente/maq/tarefas")
        self.assertEqual(chamadas[0][1], {"agente": a.id, "resultados": [{"id": "0", "ok": True}]})
        self.assertEqual(a.resultados, [])  # a fila foi esvaziada (a falha de envio a devolveria)
        self.assertEqual(despachadas, [{"tarefas": [{"id": "1"}]}])

    def test_falha_de_envio_nao_perde_resultados(self):
        a = agente.Agente("http://127.0.0.1:1", "maq")  # nada escutando
        a.resultados = [{"id": "0", "ok": True}]
        a.consultar_tarefas()
        self.assertEqual(a.resultados, [{"id": "0", "ok": True}])


class TestConf(unittest.TestCase):
    def test_projetos_locais_le_conf_com_aspas(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "projetos").mkdir()
            (Path(t) / "projetos" / "api.conf").write_text("DIR=/srv/meu\\ api\nUSUARIO=dev\nPORTA=7700\n")
            (Path(t) / "projetos" / "ruim nome.conf").write_text("DIR=/x\nPORTA=1\n")
            (Path(t) / "projetos" / "semporta.conf").write_text("DIR=/x\n")
            self.assertEqual(agente.projetos_locais(t), {"api": {"dir": "/srv/meu api", "usuario": "dev", "porta": 7700}})


if __name__ == "__main__":
    unittest.main()
