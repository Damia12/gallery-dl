"""Tests de las funciones puras de descarga.py.

Criterio de selección: se cubren las funciones donde un fallo sería
SILENCIOSO — mala clasificación de logs, posts omitidos por error,
timeouts mal asignados, estado corrupto. Se dejan fuera a propósito
las funciones cuyo error se ve al instante en pantalla (spinner,
formateo de tiempo, nombres visibles): un test ahí no aporta.

Todas son entrada -> salida, sin threads ni subprocess, así que este
archivo corre en milisegundos.

Para ejecutar:
    pytest tests/test_funciones_puras.py -v
"""

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
DESCARGA_SRC = TESTS_DIR.parent / "descarga.py"  # el real, nunca se toca


def _config_sintetico(tmp_path: Path) -> dict:
    """Mismas claves que el config.json real, con rutas dentro de tmp_path."""
    paths = {
        "rips_dir": str(tmp_path / "Rips"),
        "log_dir": str(tmp_path / "Rips" / "logs"),
        "lista_file": str(tmp_path / "lista.txt"),
        "state_file": str(tmp_path / "state.json"),
        "audit_csv": str(tmp_path / "Rips" / "logs" / "auditoria.csv"),
        "skip_posts_file": str(tmp_path / "skip_posts.json"),
        "posts_fallidos_file": str(tmp_path / "posts_fallidos.json"),
    }
    gdl = {
        "executable": "gallery-dl-fake",
        "config_file": str(tmp_path / "gallery-dl_fake.conf"),
    }
    return {
        "windows": {"paths": paths, "gallery_dl": gdl},
        "linux": {"paths": paths, "gallery_dl": gdl},
        "pipeline": {"batch_size": 5},
    }


@pytest.fixture
def dsc(tmp_path, monkeypatch):
    """Copia aislada de descarga.py, importable sin tocar producción.

    descarga.py carga config.json al importarse y hace sys.exit(1) si no
    lo encuentra, así que se le prepara uno sintético al lado de la copia.
    """
    shutil.copy(DESCARGA_SRC, tmp_path / "descarga.py")
    (tmp_path / "config.json").write_text(
        json.dumps(_config_sintetico(tmp_path)), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("descarga", None)
    mod = importlib.import_module("descarga")
    mod.TMP_PATH = tmp_path  # para los tests que tocan archivos
    yield mod
    sys.modules.pop("descarga", None)


# =============================================================================
# 1. convertir_skip_a_range
#    Fallo silencioso: omitiría posts equivocados sin que nada lo indique.
# =============================================================================

class TestConvertirSkipARange:

    def test_un_solo_post_omitido(self, dsc):
        """Saltar el post 6 => descargar 1-5 y de 7 en adelante."""
        assert dsc.convertir_skip_a_range({"skip": [6]}) == "1-5,7-"

    def test_varios_posts_omitidos(self, dsc):
        """Los huecos deben quedar entre los rangos, en orden."""
        assert dsc.convertir_skip_a_range({"skip": [3, 6, 9]}) == "1-2,4-5,7-8,10-"

    def test_omitir_el_primero(self, dsc):
        """Si se salta el post 1, el rango arranca en 2."""
        assert dsc.convertir_skip_a_range({"skip": [1]}) == "2-"

    def test_acepta_rangos_como_string(self, dsc):
        """'1-5' expande a los posts 1..5, todos omitidos."""
        assert dsc.convertir_skip_a_range({"skip": ["1-5"]}) == "6-"

    def test_mezcla_enteros_y_rangos(self, dsc):
        """Enteros y strings de rango pueden convivir en la misma lista.

        Nota: los huecos de un solo post se emiten como 'N-N' (no como 'N'
        suelto). Es válido para --post-range y así lo genera el algoritmo.
        """
        assert dsc.convertir_skip_a_range({"skip": [2, "4-6"]}) == "1-1,3-3,7-"

    def test_alias_omitir(self, dsc):
        """La clave 'omitir' se acepta igual que 'skip'."""
        assert dsc.convertir_skip_a_range({"omitir": [6]}) == "1-5,7-"

    def test_lista_vacia_devuelve_none(self, dsc):
        """Sin posts que omitir no hay --post-range que pasar."""
        assert dsc.convertir_skip_a_range({"skip": []}) is None
        assert dsc.convertir_skip_a_range({}) is None

    def test_valores_no_numericos_se_ignoran(self, dsc):
        """Entradas corruptas no deben romper ni alterar el resultado."""
        assert dsc.convertir_skip_a_range({"skip": ["abc", 6]}) == "1-5,7-"
        assert dsc.convertir_skip_a_range({"skip": ["abc"]}) is None


# =============================================================================
# 2. extraer_posts_desde_range  (el inverso del anterior)
#    Alimenta el reporte de posts fallidos.
# =============================================================================

class TestExtraerPostsDesdeRange:

    def test_rango_cerrado(self, dsc):
        assert dsc.extraer_posts_desde_range("1-5") == {1, 2, 3, 4, 5}

    def test_valores_sueltos(self, dsc):
        assert dsc.extraer_posts_desde_range("3,7,11") == {3, 7, 11}

    def test_mezcla_de_rangos_y_sueltos(self, dsc):
        assert dsc.extraer_posts_desde_range("1-3,7") == {1, 2, 3, 7}

    def test_rango_abierto_solo_toma_el_inicio(self, dsc):
        """'7-' es infinito: no se puede expandir, se registra solo el inicio."""
        assert dsc.extraer_posts_desde_range("7-") == {7}

    def test_entradas_vacias_o_none(self, dsc):
        assert dsc.extraer_posts_desde_range(None) == set()
        assert dsc.extraer_posts_desde_range("") == set()
        assert dsc.extraer_posts_desde_range("none") == set()

    def test_basura_no_rompe(self, dsc):
        """Fragmentos inválidos se descartan sin excepción."""
        assert dsc.extraer_posts_desde_range("abc,5") == {5}

    def test_roundtrip_con_convertir_skip_a_range(self, dsc):
        """Los posts omitidos NO deben aparecer en el rango generado.

        Esta es la propiedad que de verdad importa: si se rompe, se
        descargarían justo los posts que se querían saltar.
        """
        omitidos = [3, 6, 9]
        rango = dsc.convertir_skip_a_range({"skip": omitidos})
        incluidos = dsc.extraer_posts_desde_range(rango)
        for p in omitidos:
            assert p not in incluidos, f"el post {p} debía quedar omitido"


# =============================================================================
# 3. clasificar_por_tag
#    Fallo silencioso: estadísticas de auditoría corruptas.
# =============================================================================

class TestClasificarPorTag:

    def test_tag_warning(self, dsc):
        linea = "[downloader.http][warning] Rate limit exceeded"
        assert dsc.clasificar_por_tag(linea) == (True, False)

    def test_tag_error(self, dsc):
        linea = "[downloader.http][error] 404 Not Found"
        assert dsc.clasificar_por_tag(linea) == (False, True)

    def test_tag_debug_es_ruido(self, dsc):
        """Regresión del bug original: 'Sleeping' en una línea [debug] NO es warning.

        La línea existe solo para capturar el post_id. Antes, el heurístico
        de keywords la contaba como warning porque contiene 'sleeping', lo
        que inflaba el conteo de warnings de cada hilo.
        """
        linea = "[simpcity][debug] post 4001: Sleeping 1.00 seconds"
        assert dsc.clasificar_por_tag(linea) == (False, False)

    def test_tag_info_es_ruido(self, dsc):
        linea = "[simpcity][info] Starting download"
        assert dsc.clasificar_por_tag(linea) == (False, False)

    def test_sin_tag_usa_heuristico_de_keywords(self, dsc):
        """Sin tag explícito se cae al heurístico anterior."""
        assert dsc.clasificar_por_tag("Something failed badly") == (False, True)
        assert dsc.clasificar_por_tag("rate limit reached") == (True, False)

    def test_sin_tag_linea_neutra(self, dsc):
        assert dsc.clasificar_por_tag("G:\\Rips\\foto.jpg") == (False, False)

    def test_warning_tiene_prioridad_sobre_error_sin_tag(self, dsc):
        """En el heurístico, es_err solo se evalúa si NO es warning."""
        es_warn, es_err = dsc.clasificar_por_tag("warning: unable to reach host")
        assert es_warn is True
        assert es_err is False


# =============================================================================
# 4. limpiar_error
# =============================================================================

class TestLimpiarError:

    def test_quita_prefijo_gallery_dl(self, dsc):
        assert dsc.limpiar_error("[gallery-dl] 404 Not Found") == "404 Not Found"

    def test_deja_intactos_otros_tags(self, dsc):
        """Solo se limpia el prefijo [gallery-dl]; el resto se conserva."""
        linea = "[downloader.http][error] 404 Not Found"
        assert dsc.limpiar_error(linea) == linea

    def test_recorta_espacios(self, dsc):
        assert dsc.limpiar_error("   mensaje con espacios   ") == "mensaje con espacios"


# =============================================================================
# 5. extraer_thread_id / extraer_dominio
#    Fallo silencioso: post_urls mal formadas en el .log.
# =============================================================================

class TestExtraerThreadId:

    def test_url_simpcity_estandar(self, dsc):
        res = dsc.extraer_thread_id("https://simpcity.cr/threads/hilo-ejemplo.99999/")
        assert res == ("99999", "simpcity.cr")

    def test_conserva_el_dominio_real(self, dsc):
        """El dominio se propaga tal cual: no se normaliza a simpcity.cr."""
        res = dsc.extraer_thread_id("https://simpcity.su/threads/otra.999")
        assert res == ("999", "simpcity.su")

    def test_url_con_post_al_final(self, dsc):
        res = dsc.extraer_thread_id(
            "https://simpcity.cr/threads/modelo.12345/post-987"
        )
        assert res == ("12345", "simpcity.cr")

    def test_url_sin_thread_id_devuelve_none(self, dsc):
        assert dsc.extraer_thread_id("https://simpcity.cr/forums/general") is None
        assert dsc.extraer_thread_id("no es una url") is None

    def test_extraer_dominio_con_puerto_y_subdominio(self, dsc):
        assert dsc.extraer_dominio("https://www.bunkr.si/a/xyz") == "www.bunkr.si"

    def test_extraer_dominio_fallback(self, dsc):
        """Si la URL no matchea, se usa el fallback conservador."""
        assert dsc.extraer_dominio("cadena sin protocolo") == "simpcity.cr"


# =============================================================================
# 6. obtener_timeout_por_url
#    Fallo silencioso: un timeout corto mata descargas grandes por
#    "inactividad" que en realidad era rate-limit esperado.
# =============================================================================

class TestTimeouts:

    def test_host_lento_recibe_timeout_largo(self, dsc):
        for url in [
            "https://simpcity.cr/threads/x.1",
            "https://bunkr.si/a/xyz",
            "https://bunkrr.su/a/xyz",
        ]:
            assert dsc.obtener_timeout_por_url(url) == dsc.TIMEOUT_ACTIVIDAD_LENTO
            assert (
                dsc.obtener_timeout_sin_archivos_por_url(url)
                == dsc.TIMEOUT_SIN_ARCHIVOS_LENTO
            )

    def test_host_normal_recibe_timeout_base(self, dsc):
        url = "https://ejemplo.com/galeria/1"
        assert dsc.obtener_timeout_por_url(url) == dsc.TIMEOUT_ACTIVIDAD
        assert (
            dsc.obtener_timeout_sin_archivos_por_url(url) == dsc.TIMEOUT_SIN_ARCHIVOS
        )

    def test_deteccion_es_case_insensitive(self, dsc):
        assert dsc.es_host_lento("https://SimpCity.CR/threads/x.1") is True

    def test_el_timeout_lento_es_mayor_que_el_base(self, dsc):
        """Invariante: si esto se invierte, los hosts lentos morirían antes."""
        assert dsc.TIMEOUT_ACTIVIDAD_LENTO > dsc.TIMEOUT_ACTIVIDAD
        assert dsc.TIMEOUT_SIN_ARCHIVOS_LENTO > dsc.TIMEOUT_SIN_ARCHIVOS


# =============================================================================
# 7. cargar_estado / guardar_estado (incluye la escritura atómica WAL)
#    Fallo silencioso: estado corrupto -> re-descargas o URLs saltadas.
# =============================================================================

class TestEstado:

    def test_sin_archivo_devuelve_default(self, dsc):
        assert dsc.cargar_estado(["url1", "url2"]) == {"batch_index": 0}

    def test_roundtrip_guardar_cargar(self, dsc):
        dsc.guardar_estado({"batch_index": 3})
        assert dsc.cargar_estado(["a"] * 10) == {"batch_index": 3}

    def test_indice_mayor_que_la_lista_se_reinicia(self, dsc):
        """Si la lista se acortó, seguir en el índice viejo saltaría URLs."""
        dsc.guardar_estado({"batch_index": 50})
        assert dsc.cargar_estado(["a", "b"]) == {"batch_index": 0}

    def test_indice_igual_al_largo_se_conserva(self, dsc):
        """El reinicio es solo si el índice SUPERA el largo, no si lo iguala."""
        dsc.guardar_estado({"batch_index": 2})
        assert dsc.cargar_estado(["a", "b"]) == {"batch_index": 2}

    def test_no_deja_archivo_tmp_de_basura(self, dsc):
        """En operación normal el .tmp se consume con os.replace()."""
        dsc.guardar_estado({"batch_index": 1})
        sobrantes = list(dsc.TMP_PATH.glob("*.tmp"))
        assert sobrantes == [], f"quedaron temporales: {sobrantes}"

    def test_escritura_atomica_sobrevive_a_un_crash(self, dsc, monkeypatch):
        """Propiedad central del patrón WAL.

        Si el proceso muere antes del os.replace(), state.json debe
        conservar el contenido anterior — nunca quedar truncado ni vacío.
        """
        dsc.guardar_estado({"batch_index": 7})
        previo = (dsc.TMP_PATH / "state.json").read_text(encoding="utf-8")

        def replace_que_falla(origen, destino):
            raise OSError("crash simulado antes del replace")

        monkeypatch.setattr(dsc.os, "replace", replace_que_falla)
        with pytest.raises(OSError):
            dsc.guardar_estado({"batch_index": 999})

        actual = (dsc.TMP_PATH / "state.json").read_text(encoding="utf-8")
        assert actual == previo, "state.json quedó corrupto tras el crash"
        assert dsc.cargar_estado(["a"] * 10) == {"batch_index": 7}


# =============================================================================
# 8. detectar_y_reportar_fallidos: la palabra del reporte
#    Fallo silencioso: posts_fallidos.json y auditoria.csv describen la misma
#    corrida con palabras opuestas. Solo se nota leyendo los dos juntos, y para
#    entonces ya skipeaste posts que el CSV daba por recuperables.
# =============================================================================

URL_HILO = "https://simpcity.cr/threads/hilo-ejemplo.99999/"


def _res_desde_eventos(dsc, eventos):
    """Arma el `res` igual que lo arma ejecutar_url(), desde los mismos eventos.

    Partir del .jsonl y no de un dict a mano es lo que hace fuerte al test: si
    alguien vuelve a hardcodear la razón, deja de coincidir con el clasificador
    que llena el CSV y el assert falla.
    """
    resumen = dsc.auditar.resumir(eventos)
    return {
        "timeout": resumen["timeout"],
        "nuevos": resumen["nuevos"],
        "errores": resumen["errores"],
        "returncode": resumen["returncode"],
        "estado": dsc.auditar.clasificar(resumen),
    }


def _eventos(msg_error, returncode=4):
    return [
        {"t": "inicio", "url": URL_HILO, "nombre": "hilo-ejemplo.99999",
         "ts": "2026-09-02T17:39:30"},
        {"t": "archivo", "path": "a.jpg", "nuevo": True},
        {"t": "warning", "post_id": 4001, "origen": "downloader.http",
         "msg": "HTML response"},
        {"t": "error", "post_id": 4001, "origen": "download", "msg": msg_error},
        {"t": "fin", "duracion": 60, "returncode": returncode, "timeout": False},
    ]


def _leer_reporte(dsc):
    ruta = dsc.TMP_PATH / "posts_fallidos.json"
    return json.loads(ruta.read_text(encoding="utf-8"))[URL_HILO.rstrip("/")]


class TestRazonDelReporte:

    def test_dice_la_misma_palabra_que_el_csv(self, dsc):
        """Forma del caso real del 2026-09-02: decenas de "HTML response".

        El CSV decía TRANSITORIO y el reporte decía "fatal", porque acá se
        recalculaba con `errores > 0 and returncode != 0` — que es exactamente
        el predicado de TRANSITORIO en auditar.clasificar().
        """
        eventos = _eventos("Failed to download foto-1.jpg — HTML response")
        res = _res_desde_eventos(dsc, eventos)
        assert res["estado"] == "TRANSITORIO"  # lo que va al CSV

        dsc.detectar_y_reportar_fallidos(URL_HILO, res, None, None, [4001])

        assert _leer_reporte(dsc)["razon"] == res["estado"]

    def test_un_fatal_de_verdad_sigue_diciendo_fatal(self, dsc):
        """No alcanza con que coincidan: la razón tiene que seguir variando con
        el error. Un 404 es irrecuperable y el reporte debe decirlo."""
        eventos = _eventos("Failed to download foto.jpg — 404 Not Found")
        res = _res_desde_eventos(dsc, eventos)
        assert res["estado"] == "FATAL"

        dsc.detectar_y_reportar_fallidos(URL_HILO, res, None, None, [4001])

        assert _leer_reporte(dsc)["razon"] == "FATAL"

    def test_el_timeout_conserva_su_matiz(self, dsc):
        """clasificar() colapsa todo timeout en TIMEOUT. La distinción entre
        morir sin bajar nada y morir a medio camino no es un umbral inventado
        —o hay archivos o no los hay— y se mantiene."""
        res = {"timeout": True, "nuevos": 0, "errores": 0,
               "returncode": -9, "estado": "TIMEOUT"}
        dsc.detectar_y_reportar_fallidos(URL_HILO, res, None, None, [4001])
        assert _leer_reporte(dsc)["razon"] == "TIMEOUT_ATASCADO"

        res["nuevos"] = 120
        dsc.detectar_y_reportar_fallidos(URL_HILO, res, None, None, [4001])
        assert _leer_reporte(dsc)["razon"] == "TIMEOUT_PARCIAL"

    def test_una_corrida_ok_no_genera_reporte(self, dsc):
        eventos = [
            {"t": "inicio", "url": URL_HILO, "nombre": "x", "ts": "2026-09-02T00:00:00"},
            {"t": "archivo", "path": "a.jpg", "nuevo": True},
            {"t": "fin", "duracion": 10, "returncode": 0, "timeout": False},
        ]
        res = _res_desde_eventos(dsc, eventos)
        assert res["estado"] == "OK"

        dsc.detectar_y_reportar_fallidos(URL_HILO, res, None, None, [])

        assert not (dsc.TMP_PATH / "posts_fallidos.json").exists()


# =============================================================================
# 9. escribir_log_texto: los warnings que no tienen error
#    Fallo silencioso: el warning existe en el .jsonl y no aparece en el .log,
#    que es lo que uno realmente lee. Solo se ve como un número en el Resumen.
# =============================================================================

class TestWarningsSinErrorEnElLog:

    def _render(self, dsc, eventos):
        ruta = dsc.TMP_PATH / "salida.log"
        resumen = dsc.auditar.resumir(eventos)
        dsc.escribir_log_texto(ruta, resumen["url"], resumen, eventos)
        return ruta.read_text(encoding="utf-8")

    def test_muestra_el_warning_que_no_tiene_error(self, dsc):
        """deviantart avisa que hay contenido de pago que no bajó.
        Sin error asociado, quedaba solo como "1 warnings" en el Resumen."""
        eventos = [
            {"t": "inicio", "url": "https://www.deviantart.com/alguien",
             "nombre": "alguien", "ts": "2026-09-02T17:57:09"},
            {"t": "archivo", "path": "a.jpg", "nuevo": True},
            {"t": "warning", "post_id": None, "origen": "deviantart",
             "msg": "Unable to access premium content (type: paid)"},
            {"t": "fin", "duracion": 37, "returncode": 0, "timeout": False},
        ]

        texto = self._render(dsc, eventos)

        assert "WARNINGS SIN ERROR (1)" in texto
        assert "Unable to access premium content" in texto
        assert "Sin errores." in texto  # sigue siendo una corrida sin errores

    def test_no_repite_el_warning_ya_fusionado_en_su_error(self, dsc):
        """descarga.py mete la causa del warning dentro del mensaje del error
        del mismo post. Listarlo otra vez sería ruido duplicado: el post 4001
        se lee arriba, y solo el 4002 —que nunca falló— es novedad."""
        eventos = [
            {"t": "inicio", "url": URL_HILO, "nombre": "hilo-ejemplo.99999",
             "ts": "2026-09-02T17:39:30"},
            {"t": "warning", "post_id": 4001, "origen": "downloader.http",
             "msg": "HTML response"},
            {"t": "error", "post_id": 4001, "origen": "download",
             "msg": "Failed to download foto-1.jpg — HTML response"},
            {"t": "warning", "post_id": 4002, "origen": "downloader.http",
             "msg": "Read timed out. (1/7)"},
            {"t": "fin", "duracion": 777, "returncode": 4, "timeout": False},
        ]

        texto = self._render(dsc, eventos)

        assert "WARNINGS SIN ERROR (1)" in texto
        assert "Read timed out" in texto
        assert "post 4002" in texto
        # El 4001 aparece una sola vez, en ERRORES POR POST.
        assert texto.count("post 4001") == 1

    def test_sin_warnings_no_aparece_la_seccion(self, dsc):
        eventos = [
            {"t": "inicio", "url": URL_HILO, "nombre": "x", "ts": "2026-09-02T00:00:00"},
            {"t": "archivo", "path": "a.jpg", "nuevo": True},
            {"t": "fin", "duracion": 10, "returncode": 0, "timeout": False},
        ]
        assert "WARNINGS SIN ERROR" not in self._render(dsc, eventos)
