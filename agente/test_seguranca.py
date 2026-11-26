"""Regressão de segurança do agente (TDD): projeto.add não aceita --usuario arbitrário."""
import unittest

import agente


class TestProjetoAddUsuario(unittest.TestCase):
    P = {"api": {"dir": "/srv/api", "usuario": "dev", "porta": 7700}}

    def test_usuario_arbitrario_recusado(self):
        with self.assertRaises(agente.TarefaInvalida):
            agente.montar_comando({"tipo": "projeto.add", "args": {"nome": "x", "dir": "/srv/x", "usuario": "outro"}}, self.P)

    def test_dono_e_sem_usuario_continuam_ok(self):
        argv, _ = agente.montar_comando({"tipo": "projeto.add", "args": {"nome": "x", "dir": "/srv/x", "usuario": "dev"}}, self.P)
        self.assertIn("--usuario", argv)
        argv, _ = agente.montar_comando({"tipo": "projeto.add", "args": {"nome": "x", "dir": "/srv/x"}}, self.P)
        self.assertNotIn("--usuario", argv)


if __name__ == "__main__":
    unittest.main()
