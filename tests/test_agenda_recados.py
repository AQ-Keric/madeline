"""Recados y agenda: sin red y sin Google."""
import unittest
from datetime import date, datetime
from unittest import mock

import entorno  # noqa: F401
import agenda
import madeline_server as j
import recados


def nombres(texto):
    return {t["function"]["name"] for t in j.tools_necesarias(texto)}


class TestHoraDicha(unittest.TestCase):
    LUNES = date(2026, 10, 19)

    def hora(self, dicha, parte=None, ahora="10:00"):
        momento = datetime.combine(self.LUNES, datetime.strptime(ahora, "%H:%M").time())
        return agenda.interpretar_hora_dicha(self.LUNES, dicha, parte, momento)

    def test_de_1_a_6_sin_aclarar_es_de_tarde(self):
        self.assertEqual(self.hora("4:00", ahora="02:44"), datetime(2026, 10, 19, 16, 0))

    def test_parte_del_dia_manda(self):
        self.assertEqual(self.hora("4:00", "manana", ahora="02:44"), datetime(2026, 10, 19, 4, 0))
        self.assertEqual(self.hora("9:00", "noche"), datetime(2026, 10, 19, 21, 0))
        self.assertEqual(self.hora("12:00", "noche"), datetime(2026, 10, 20, 0, 0))

    def test_proxima_vez_que_llega_esa_hora(self):
        self.assertEqual(self.hora("8:00", ahora="10:00"), datetime(2026, 10, 19, 20, 0))
        self.assertEqual(self.hora("8:30", ahora="07:00"), datetime(2026, 10, 19, 8, 30))

    def test_24_horas(self):
        self.assertEqual(self.hora("16:30"), datetime(2026, 10, 19, 16, 30))

    def test_hora_invalida(self):
        self.assertIsNone(self.hora("25:00"))
        self.assertIsNone(self.hora("a las cuatro"))

    def test_lo_dicho(self):
        self.assertEqual(agenda.parte_del_dia("a las 9 de la noche"), "noche")
        self.assertIsNone(agenda.parte_del_dia("mañana a las 9"))
        self.assertTrue(agenda.menciona_dia("mañana a las 9"))
        self.assertTrue(agenda.menciona_dia("el jueves dentista"))
        self.assertFalse(agenda.menciona_dia("agenda dentista a las 4 de la tarde"))

    def test_en_voz_alta(self):
        self.assertEqual(agenda.a_la_hora(datetime(2026, 1, 1, 13, 0)), "a la 1 de la tarde")
        self.assertEqual(agenda.a_la_hora(datetime(2026, 1, 1, 8, 30)), "a las 8:30 de la mañana")
        self.assertEqual(agenda.dia_relativo(date(2026, 10, 24), self.LUNES), "el sábado 24 de octubre")


class TestRecados(unittest.TestCase):
    def setUp(self):
        recados._guardar([])

    def test_agregar_ver_y_tachar(self):
        j.agregar_recado("pagar la luz")
        j.agregar_recado("comprar pan para la once")
        self.assertEqual(j.ver_recados(), "Tienes 2 recados: Pagar la luz y Comprar pan para la once.")
        self.assertIn("Comprar pan", j.borrar_recado("ya compré el pan"))
        self.assertEqual([r["texto"] for r in recados.listar()], ["Pagar la luz"])

    def test_no_encontrado_y_todos(self):
        j.agregar_recado("pagar la luz")
        self.assertTrue(j.borrar_recado("lavar el auto").startswith("No encontré"))
        self.assertEqual(j.borrar_recado("todos"), "Listo, borré tus 1 recados.")
        self.assertIn("No tienes recados", j.ver_recados())


class TestAgendaSinGoogle(unittest.TestCase):
    def test_crear_sin_autorizar_lo_dice(self):
        with mock.patch.object(agenda, "autorizado", return_value=False):
            respuesta = j.crear_evento("Dentista", "2026-10-23", "4:00", mensaje="el jueves a las 4")
        self.assertIn("conectar tu cuenta de Google", respuesta)

    def test_permiso_vencido_abre_el_navegador(self):
        with mock.patch.object(agenda, "crear", side_effect=agenda.PermisoVencido("invalid_grant")), \
                mock.patch.object(agenda, "reautorizar") as reautorizar:
            respuesta = j.crear_evento("Dentista", "2026-10-23", "4:00", mensaje="el jueves a las 4")
        reautorizar.assert_called_once()
        self.assertIn("se venció el permiso", respuesta)

    def test_crear_usa_lo_dicho(self):
        with mock.patch.object(agenda, "crear") as crear, \
                mock.patch.object(j, "datetime", wraps=datetime) as reloj:
            reloj.now.return_value = datetime(2026, 10, 19, 2, 44)
            respuesta = j.crear_evento("Dentista", "2026-10-25", "4:00", mensaje="agéndame dentista a las 4")
        # Sin día dicho es hoy (aunque Groq diga otro), y "a las 4" es de tarde.
        self.assertEqual(crear.call_args[0][1], datetime(2026, 10, 19, 16, 0))
        self.assertEqual(respuesta, "Listo, agendé Dentista hoy a las 4 de la tarde.")

    def test_ver_agenda(self):
        eventos = [
            {"fecha": date(2026, 10, 19), "hora": "", "titulo": "Cumple de la Coni"},
            {"fecha": date(2026, 10, 19), "hora": "16:00", "titulo": "Dentista"},
        ]
        with mock.patch.object(agenda, "eventos_entre", return_value=eventos), \
                mock.patch.object(j, "datetime", wraps=datetime) as reloj:
            reloj.now.return_value = datetime(2026, 10, 19, 9, 0)
            reloj.strptime = datetime.strptime
            reloj.combine = datetime.combine
            respuesta = j.ver_agenda("2026-10-19")
        self.assertEqual(respuesta, "Hoy tienes Cumple de la Coni y Dentista a las 4 de la tarde.")

    def test_sin_calendario(self):
        with mock.patch.object(agenda, "eventos_entre", side_effect=agenda.SinAgenda("sin red")):
            self.assertTrue(j.ver_agenda("2026-10-19").startswith("No pude"))


class TestTemas(unittest.TestCase):
    def test_frases_reciben_sus_tools(self):
        casos = {
            "recuérdame que debo pagar la luz": {"agregar_recado"},
            "tengo que llamar a mi mamá, no me dejes olvidar": {"agregar_recado"},
            "qué recados tengo": {"ver_recados"},
            "ya pagué la luz": {"borrar_recado"},
            "¿qué tengo mañana?": {"ver_agenda"},
            "agéndame dentista el jueves a las 4": {"crear_evento"},
            "tengo hora al doctor el viernes": {"crear_evento"},
        }
        for frase, esperadas in casos.items():
            with self.subTest(frase=frase):
                self.assertTrue(esperadas <= nombres(frase), nombres(frase))

    def test_recado_no_va_a_la_memoria(self):
        self.assertIsNone(j.pedido_de_memoria("anota un recado: comprar pan"))
        self.assertIsNone(j.pedido_de_memoria("recuérdame que debo pagar la luz"))
        self.assertIsNotNone(j.pedido_de_memoria("recuerda que me gusta el café"))



class TestEndpointsCelular(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        recados._guardar([])
        self.cliente = TestClient(j.app, client=("127.0.0.1", 50000))

    def test_recados(self):
        nuevo = self.cliente.post("/recados", json={"texto": "pagar la luz"}).json()
        self.assertEqual(nuevo["texto"], "Pagar la luz")
        self.assertEqual(len(self.cliente.get("/recados").json()["recados"]), 1)
        self.assertEqual(self.cliente.delete(f"/recados/{nuevo['id']}").status_code, 200)
        self.assertEqual(self.cliente.delete(f"/recados/{nuevo['id']}").status_code, 404)
        self.assertEqual(self.cliente.post("/recados", json={"texto": "  "}).status_code, 400)

    def test_agenda(self):
        eventos = [{"fecha": date(2026, 10, 19), "hora": "16:00", "titulo": "Dentista"}]
        with mock.patch.object(agenda, "eventos_entre", return_value=eventos):
            datos = self.cliente.get("/agenda").json()
        self.assertEqual(datos["eventos"], [{"fecha": "2026-10-19", "hora": "16:00", "titulo": "Dentista"}])
        with mock.patch.object(agenda, "eventos_entre", side_effect=agenda.SinAgenda("sin red")):
            self.assertEqual(self.cliente.get("/agenda").json()["error"], "sin red")


if __name__ == "__main__":
    unittest.main()
