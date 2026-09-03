"""Tests de auditar.py — clasificación y atribución sobre eventos .jsonl.

A diferencia de test_funciones_puras.py, acá no hace falta copiar el módulo a
tmp_path: auditar.py se escribe testeable desde el principio (sin cargar
config.json a nivel de módulo), así que se importa directo.

El criterio de cobertura es el del proyecto: se testea lo que falla en
silencio. Clasificar mal un log no rompe nada visible — solo escribe una
etiqueta equivocada en un CSV que nadie mira hasta que importa.
"""

import csv
import json
import zipfile
from datetime import datetime

import pytest

import auditar


# =============================================================================
# HELPERS
# =============================================================================


def escribir_jsonl(tmp_path, nombre, eventos):
    """Escribe eventos como .jsonl y devuelve la ruta."""
    ruta = tmp_path / nombre
    with open(ruta, "w", encoding="utf-8") as f:
        for e in eventos:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return ruta


def ev_inicio(url="https://simpcity.cr/threads/test.12345/", nombre=None, **kw):
    # En producción el `nombre` lo calcula descarga.py una sola vez y lo emite
    # acá dentro. Derivarlo de la URL es una comodidad DEL FIXTURE, para no
    # repetirlo en cada llamada; auditar nunca lo recalcula.
    if nombre is None:
        nombre = url.rstrip("/").split("/")[-1]
    return {
        "t": "inicio",
        "url": url,
        "nombre": nombre,
        "ts": "2026-08-31T15:07:49",
        **kw,
    }


def ev_archivo(path, nuevo=True):
    return {"t": "archivo", "path": path, "nuevo": nuevo}


def ev_error(post_id, msg, origen="download"):
    return {"t": "error", "post_id": post_id, "origen": origen, "msg": msg}


def ev_warning(post_id, msg, origen="downloader.http"):
    return {"t": "warning", "post_id": post_id, "origen": origen, "msg": msg}


def ev_fin(duracion=100, returncode=0, timeout=False):
    return {
        "t": "fin",
        "duracion": duracion,
        "returncode": returncode,
        "timeout": timeout,
    }


# =============================================================================
# DATOS DEL CASO DE REGRESIÓN
# =============================================================================

# DATOS ANONIMIZADOS. El repo es público: los nombres y URLs de un caso real no
# se comitean. Lo que el test necesita no son los archivos concretos sino su
# FORMA — que es lo que rompía al clasificador.
#
# Los nombres de redes sociales son cadenas de IDs numéricos largos unidos por
# guiones bajos. Cuando 404, 403 o 410 cae en el medio de uno de esos IDs, el
# `RE_ERR` de auditar.py lo confunde con un código HTTP y marca el archivo como
# error. Verificado en producción: una descarga de 219 archivos con errores="0"
# y returncode=0 quedó clasificada FATAL por 18 nombres con esta forma.
VENENOS = ("404", "403", "410")


def nombre_envenenado(base: int, veneno: str) -> str:
    """Genera un nombre con la forma que confunde a auditar.py.

    `veneno` queda embebido dentro de un ID numérico, sin límite de palabra a
    los costados — que es exactamente la condición que hace fallar a un patrón
    `404` suelto y que un `\\b404\\b` sí distingue.
    """
    return f"{base}_{base + 1}_{base}{veneno}{base + 2}_n{base:016x}.jpg"


NOMBRES_ENVENENADOS = [
    nombre_envenenado(100000000 + i * 7919, VENENOS[i % len(VENENOS)])
    for i in range(18)
]

CARPETA_CASO = r"G:\Rips\Simpcity\hilo-ejemplo"
URL_CASO = "https://simpcity.cr/threads/hilo-ejemplo.100001/"

CARPETA_ERRORES = r"G:\Rips\Simpcity\hilo-con-errores"
URL_ERRORES = "https://simpcity.cr/threads/hilo-con-errores.100002/"


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def jsonl_ok(tmp_path):
    """Descarga limpia: 3 archivos nuevos, sin errores."""
    return escribir_jsonl(
        tmp_path,
        "ok.jsonl",
        [
            ev_inicio(),
            ev_archivo(r"G:\Rips\Simpcity\test\a.jpg"),
            ev_archivo(r"G:\Rips\Simpcity\test\b.jpg"),
            ev_archivo(r"G:\Rips\Simpcity\test\c.jpg"),
            ev_fin(duracion=42, returncode=0),
        ],
    )


@pytest.fixture
def jsonl_errores(tmp_path):
    """Caso real anonimizado: errores repartidos en 4 posts distintos."""
    eventos = [ev_inicio(URL_ERRORES)]
    eventos += [ev_archivo(rf"{CARPETA_ERRORES}\f{i}.jpg") for i in range(286)]
    for post_id, cuantos in ((13510, 15), (13511, 15), (13501, 13), (13515, 6)):
        for i in range(cuantos):
            eventos.append(
                ev_error(post_id, f"Failed to download IMG_{i}.jpg: Connection reset")
            )
    eventos.append(ev_fin(duracion=1020, returncode=4))
    return escribir_jsonl(tmp_path, "errores.jsonl", eventos)


@pytest.fixture
def jsonl_timeout(tmp_path):
    """El watchdog mató el proceso: hay archivos, pero timeout=true."""
    return escribir_jsonl(
        tmp_path,
        "timeout.jsonl",
        [
            ev_inicio(),
            ev_archivo(r"G:\Rips\Simpcity\test\a.jpg"),
            ev_fin(duracion=7200, returncode=-1, timeout=True),
        ],
    )


@pytest.fixture
def jsonl_incompleto(tmp_path):
    """Proceso interrumpido: falta el evento `fin`."""
    return escribir_jsonl(
        tmp_path,
        "incompleto.jsonl",
        [ev_inicio(), ev_archivo(r"G:\Rips\Simpcity\test\a.jpg")],
    )


@pytest.fixture
def jsonl_falso_fatal(tmp_path):
    """Reconstrucción anonimizada de un log real.

    219 archivos, cero errores, returncode 0 — igual que el caso de producción.
    Los primeros 18 nombres tienen la forma que hoy dispara el falso FATAL.
    """
    eventos = [ev_inicio(URL_CASO)]
    eventos += [ev_archivo(rf"{CARPETA_CASO}\{n}") for n in NOMBRES_ENVENENADOS]
    faltan = 219 - len(NOMBRES_ENVENENADOS)
    eventos += [ev_archivo(rf"{CARPETA_CASO}\normal_{i}.jpg") for i in range(faltan)]
    eventos.append(ev_fin(duracion=159, returncode=0))
    return escribir_jsonl(tmp_path, "falso_fatal.jsonl", eventos)


# =============================================================================
# LECTURA
# =============================================================================


class TestLeerEventos:
    def test_lee_todos_los_eventos(self, jsonl_ok):
        eventos = auditar.leer_eventos(jsonl_ok)
        assert len(eventos) == 5
        assert eventos[0]["t"] == "inicio"
        assert eventos[-1]["t"] == "fin"

    def test_linea_corrupta_no_aborta(self, tmp_path):
        """Un .jsonl truncado por un kill debe auditarse hasta donde llegó."""
        ruta = tmp_path / "truncado.jsonl"
        ruta.write_text(
            json.dumps(ev_inicio())
            + "\n"
            + json.dumps(ev_archivo(r"G:\a.jpg"))
            + "\n"
            + '{"t":"archivo","path":"G:\\\\b.jp',  # cortado a la mitad
            encoding="utf-8",
        )
        eventos = auditar.leer_eventos(ruta)
        assert len(eventos) == 2

    def test_archivo_vacio(self, tmp_path):
        ruta = tmp_path / "vacio.jsonl"
        ruta.write_text("", encoding="utf-8")
        assert auditar.leer_eventos(ruta) == []


# =============================================================================
# RESUMEN
# =============================================================================


class TestResumir:
    def test_cuenta_archivos_nuevos(self, jsonl_ok):
        r = auditar.resumir(auditar.leer_eventos(jsonl_ok))
        assert r["nuevos"] == 3
        assert r["ya"] == 0

    def test_separa_nuevos_de_ya_descargados(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path,
            "mixto.jsonl",
            [
                ev_inicio(),
                ev_archivo(r"G:\a.jpg", nuevo=True),
                ev_archivo(r"G:\b.jpg", nuevo=False),
                ev_archivo(r"G:\c.jpg", nuevo=False),
                ev_fin(),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert (r["nuevos"], r["ya"]) == (1, 2)

    def test_deduplica_por_path(self, tmp_path):
        """gallery-dl puede repetir una ruta en stdout; no debe contarse dos veces."""
        ruta = escribir_jsonl(
            tmp_path,
            "dup.jsonl",
            [
                ev_inicio(),
                ev_archivo(r"G:\a.jpg"),
                ev_archivo(r"G:\a.jpg"),
                ev_archivo(r"G:\b.jpg"),
                ev_fin(),
            ],
        )
        assert auditar.resumir(auditar.leer_eventos(ruta))["nuevos"] == 2

    def test_toma_url_del_evento_inicio(self, jsonl_falso_fatal):
        r = auditar.resumir(auditar.leer_eventos(jsonl_falso_fatal))
        assert r["url"] == URL_CASO

    def test_toma_duracion_y_returncode_del_evento_fin(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        assert r["duracion"] == 1020
        assert r["returncode"] == 4
        assert r["timeout"] is False

    def test_falta_evento_fin_marca_incompleto(self, jsonl_incompleto):
        r = auditar.resumir(auditar.leer_eventos(jsonl_incompleto))
        assert r["completo"] is False

    def test_evento_fin_presente_marca_completo(self, jsonl_ok):
        assert auditar.resumir(auditar.leer_eventos(jsonl_ok))["completo"] is True


# =============================================================================
# ATRIBUCIÓN  — el bug 2
# =============================================================================


class TestAtribucionDePosts:
    """El post reportado debe ser el que falló, no el último que se vio.

    En producción: un hilo tuvo 49 errores en los posts 13501/13510/13511/
    13515, y posts_fallidos.json registró el 51013156 — que no generó ninguno.
    La causa era leer una variable corriente al final del run en vez de tomar
    el post_id de la línea del error.
    """

    def test_un_solo_post_con_error(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path,
            "uno.jsonl",
            [ev_inicio(), ev_error(13510, "Failed to download"), ev_fin(returncode=4)],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["posts_con_error"] == [13510]

    def test_varios_posts_ordenados_y_sin_repetir(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        assert r["posts_con_error"] == [13501, 13510, 13511, 13515]

    def test_el_ultimo_post_visto_no_contamina(self, tmp_path):
        """Un evento posterior sin error no debe aparecer como culpable."""
        ruta = escribir_jsonl(
            tmp_path,
            "orden.jsonl",
            [
                ev_inicio(),
                ev_error(13510, "Failed to download"),
                ev_archivo(r"G:\despues.jpg"),
                ev_warning(51013156, "connection reset, retrying"),
                ev_fin(returncode=4),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["posts_con_error"] == [13510]
        assert 51013156 not in r["posts_con_error"]

    def test_warnings_no_cuentan_como_errores(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path,
            "warn.jsonl",
            [ev_inicio(), ev_warning(13510, "retrying"), ev_fin()],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["errores"] == 0
        assert r["warnings"] == 1
        assert r["posts_con_error"] == []


# =============================================================================
# CLASIFICACIÓN
# =============================================================================


class TestEsFatal:
    @pytest.mark.parametrize(
        "msg",
        [
            "404 Not Found",
            "410 Gone",
            "thread has been deleted",
            "Unsupported URL 'https://mega.nz/...'",
            "Unable to extract media",
            "Failed to parse response",
        ],
    )
    def test_mensajes_fatales(self, msg):
        assert auditar.es_fatal(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "Connection reset by peer",
            "ReadTimeout: server did not respond",
            "503 Service Unavailable",
            "Failed to download IMG_9271.jpg",
        ],
    )
    def test_mensajes_transitorios(self, msg):
        assert auditar.es_fatal(msg) is False

    def test_no_matchea_numeros_embebidos(self):
        """404 dentro de un ID numérico no es un HTTP 404. Este es el bug 1."""
        assert auditar.es_fatal("404998544825100695") is False


class TestClasificar:
    def test_descarga_limpia_es_ok(self, jsonl_ok):
        r = auditar.resumir(auditar.leer_eventos(jsonl_ok))
        assert auditar.clasificar(r) == "OK"

    def test_errores_recuperables_son_transitorios(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        assert auditar.clasificar(r) == "TRANSITORIO"

    def test_error_fatal_es_fatal(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path,
            "fatal.jsonl",
            [ev_inicio(), ev_error(13510, "404 Not Found"), ev_fin(returncode=4)],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert auditar.clasificar(r) == "FATAL"

    def test_timeout_gana_sobre_todo(self, tmp_path):
        """Prioridad TIMEOUT > FATAL: si lo mataron, eso es lo que pasó."""
        ruta = escribir_jsonl(
            tmp_path,
            "to_fatal.jsonl",
            [
                ev_inicio(),
                ev_error(13510, "404 Not Found"),
                ev_fin(returncode=-1, timeout=True),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert auditar.clasificar(r) == "TIMEOUT"

    def test_timeout_simple(self, jsonl_timeout):
        r = auditar.resumir(auditar.leer_eventos(jsonl_timeout))
        assert auditar.clasificar(r) == "TIMEOUT"

    def test_returncode_no_cero_sin_errores_es_transitorio(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path, "rc.jsonl", [ev_inicio(), ev_fin(returncode=1)]
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert auditar.clasificar(r) == "TRANSITORIO"

    def test_solo_devuelve_estados_conocidos(self, jsonl_ok, jsonl_errores, jsonl_timeout):
        for j in (jsonl_ok, jsonl_errores, jsonl_timeout):
            r = auditar.resumir(auditar.leer_eventos(j))
            assert auditar.clasificar(r) in auditar.ESTADOS


# =============================================================================
# REGRESIÓN CON DATOS REALES  — el bug 1
# =============================================================================


class TestRegresionFalsoFatal:
    """Caso real: 219 archivos, 0 errores, rc=0 — hoy auditar.py dice FATAL.

    Causa: RE_ERR y FATAL_PATTERNS escanean todas las líneas del .log,
    incluidos los 219 paths. 18 nombres de Instagram traen 404/403/410 dentro
    de un ID numérico. Reproducido de una fila real de auditoria.csv.
    """

    def test_descarga_perfecta_no_es_fatal(self, jsonl_falso_fatal):
        r = auditar.resumir(auditar.leer_eventos(jsonl_falso_fatal))
        assert auditar.clasificar(r) == "OK"

    def test_cuenta_los_219_archivos(self, jsonl_falso_fatal):
        r = auditar.resumir(auditar.leer_eventos(jsonl_falso_fatal))
        assert r["nuevos"] == 219

    def test_no_inventa_errores_desde_los_nombres(self, jsonl_falso_fatal):
        r = auditar.resumir(auditar.leer_eventos(jsonl_falso_fatal))
        assert r["errores"] == 0
        assert r["posts_con_error"] == []

    def test_los_nombres_envenenados_estan_presentes(self, jsonl_falso_fatal):
        """Guarda de la guarda: si el fixture pierde los nombres, no prueba nada."""
        eventos = auditar.leer_eventos(jsonl_falso_fatal)
        paths = [e["path"] for e in eventos if e["t"] == "archivo"]
        assert sum("404" in p or "403" in p or "410" in p for p in paths) >= 18


# =============================================================================
# FILA DEL CSV
# =============================================================================


class TestFilaCsv:
    def test_orden_y_largo_coinciden_con_el_header(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        fila = auditar.fila_csv(r, "2026-08-31 15:13:31")
        assert len(fila) == len(auditar.CSV_HEADER)

    def test_contenido_de_la_fila(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        fila = dict(zip(auditar.CSV_HEADER, auditar.fila_csv(r, "F")))
        assert fila["Fecha"] == "F"
        assert fila["URL"] == URL_ERRORES
        assert fila["Nuevos"] == 286
        assert fila["Errores"] == 49
        assert fila["Estado"] == "TRANSITORIO"
        assert fila["Returncode"] == 4

    def test_posts_con_error_es_legible_en_una_celda(self, jsonl_errores):
        r = auditar.resumir(auditar.leer_eventos(jsonl_errores))
        fila = dict(zip(auditar.CSV_HEADER, auditar.fila_csv(r, "F")))
        celda = str(fila["Posts_con_error"])
        for pid in (13501, 13510, 13511, 13515):
            assert str(pid) in celda
        assert ";" not in celda  # el delimitador del CSV no puede aparecer adentro

    def test_sin_errores_deja_la_celda_vacia(self, jsonl_ok):
        r = auditar.resumir(auditar.leer_eventos(jsonl_ok))
        fila = dict(zip(auditar.CSV_HEADER, auditar.fila_csv(r, "F")))
        assert fila["Posts_con_error"] in ("", [])


# =============================================================================
# LAS DOS DECISIONES DE SCHEMA
# =============================================================================
# Estos tests no cubren un bug: fijan dos decisiones de diseño que ya se
# tomaron, para que un cambio futuro las rompa ruidosamente en vez de en
# silencio. Ver PLAN_AUDITAR.md, decisiones 21 y 22.


class TestNombreVieneDelEvento:
    """El nombre del modelo lo emite descarga.py; auditar no lo deriva."""

    def test_toma_el_nombre_del_evento_inicio(self, tmp_path):
        ruta = escribir_jsonl(
            tmp_path,
            "x.jsonl",
            [
                ev_inicio(url="https://simpcity.cr/threads/algo.999/", nombre="algo.999"),
                ev_fin(),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["nombre_modelo"] == "algo.999"

    def test_no_lo_recalcula_desde_la_url(self, tmp_path):
        """Si el nombre y la URL discrepan, gana el nombre emitido.

        Es el caso real que la duplicación provocaba: descarga.py trunca a 60
        caracteres y reemplaza caracteres inválidos de Windows. Si auditar
        volviera a derivarlo de la URL con su propia copia de esa regla, el CSV
        nombraría una carpeta que no existe en disco.
        """
        ruta = escribir_jsonl(
            tmp_path,
            "x.jsonl",
            [
                ev_inicio(
                    url="https://simpcity.cr/threads/nombre-larguisimo.123/",
                    nombre="nombre-truncado",
                ),
                ev_fin(),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["nombre_modelo"] == "nombre-truncado"

    def test_sin_el_campo_queda_vacio_en_vez_de_adivinar(self, tmp_path):
        """Un .jsonl sin `nombre` deja la celda vacía. Es deliberado: un
        fallback que recalcula desde la URL reintroduce la duplicación y falla
        en silencio; una celda vacía se ve."""
        ruta = escribir_jsonl(
            tmp_path,
            "x.jsonl",
            [{"t": "inicio", "url": "https://simpcity.cr/threads/algo.999/"}, ev_fin()],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["nombre_modelo"] == ""


class TestFinSinContadores:
    """Los totales se derivan de los eventos, no se leen del evento `fin`."""

    def test_ignora_contadores_si_alguien_los_agrega_al_fin(self, tmp_path):
        """Un `fin` con totales inventados no debe poder torcer el resultado."""
        ruta = escribir_jsonl(
            tmp_path,
            "x.jsonl",
            [
                ev_inicio(),
                ev_archivo(r"G:\Rips\Simpcity\test\a.jpg"),
                ev_archivo(r"G:\Rips\Simpcity\test\b.jpg"),
                {
                    "t": "fin",
                    "nuevos": 999,
                    "errores": 999,
                    "duracion": 10,
                    "returncode": 0,
                    "timeout": False,
                },
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["nuevos"] == 2
        assert r["errores"] == 0

    def test_jsonl_truncado_conserva_lo_que_alcanzo_a_bajar(self, tmp_path):
        """Sin evento `fin` (watchdog kill), los contadores siguen siendo
        correctos hasta donde llegó el log. Con contadores en `fin` darían 0."""
        ruta = escribir_jsonl(
            tmp_path,
            "x.jsonl",
            [
                ev_inicio(),
                ev_archivo(r"G:\Rips\Simpcity\test\a.jpg"),
                ev_archivo(r"G:\Rips\Simpcity\test\b.jpg"),
                ev_archivo(r"G:\Rips\Simpcity\test\c.jpg"),
            ],
        )
        r = auditar.resumir(auditar.leer_eventos(ruta))
        assert r["completo"] is False
        assert r["nuevos"] == 3


# =============================================================================
# ORQUESTACIÓN
# =============================================================================
# Todo lo de acá falla en silencio: una fila que no se escribe, un log que se
# borra sin archivarse, un ZIP que se purga antes de tiempo. Nada de eso se ve
# en pantalla el día que pasa.


@pytest.fixture
def entorno(tmp_path):
    """log_dir y rips_dir de mentira, con el mismo schema que config.json."""
    log_dir = tmp_path / "Rips" / "logs"
    rips_dir = tmp_path / "Rips"
    log_dir.mkdir(parents=True)
    return {
        "log_dir": log_dir,
        "rips_dir": rips_dir,
        "audit_csv": log_dir / "auditoria.csv",
        "posts_fallidos_file": tmp_path / "posts_fallidos.json",
    }


def poner_log(entorno, nombre, eventos, con_texto=True):
    """Deja un {nombre}.jsonl (y su .log) en log_dir, como los deja descarga.py."""
    ruta = escribir_jsonl(entorno["log_dir"], f"{nombre}.jsonl", eventos)
    if con_texto:
        (entorno["log_dir"] / f"{nombre}.log").write_text("vista humana", encoding="utf-8")
    return ruta


def leer_csv(entorno):
    """Devuelve las filas del CSV como dicts, salteando el 'sep=;' de Excel."""
    texto = entorno["audit_csv"].read_text(encoding="utf-8")
    lineas = [ln for ln in texto.splitlines() if ln and not ln.startswith("sep=")]
    filas = list(csv.reader(lineas, delimiter=";"))
    return [dict(zip(filas[0], f)) for f in filas[1:]]


class TestAnalizarLogs:
    def test_una_fila_por_jsonl(self, entorno):
        poner_log(entorno, "a", [ev_inicio(nombre="a"), ev_fin()])
        poner_log(entorno, "b", [ev_inicio(nombre="b"), ev_fin()])

        auditar.analizar_logs(entorno)

        filas = leer_csv(entorno)
        assert {f["Nombre_Modelo"] for f in filas} == {"a", "b"}

    def test_la_fecha_es_la_de_la_corrida_no_la_de_la_auditoria(self, entorno):
        """El .jsonl puede auditarse días después: la fila tiene que decir
        cuándo se descargó, no cuándo se miró."""
        poner_log(
            entorno,
            "a",
            [ev_inicio(nombre="a", ts="2026-08-31T15:07:49"), ev_fin()],
        )

        auditar.analizar_logs(entorno)

        assert leer_csv(entorno)[0]["Fecha"] == "2026-08-31 15:07:49"

    def test_clasifica_cada_fila(self, entorno):
        poner_log(entorno, "ok", [ev_inicio(nombre="ok"), ev_fin()])
        poner_log(
            entorno,
            "malo",
            [ev_inicio(nombre="malo"), ev_error(1, "404 Not Found"), ev_fin(returncode=4)],
        )

        auditar.analizar_logs(entorno)

        estados = {f["Nombre_Modelo"]: f["Estado"] for f in leer_csv(entorno)}
        assert estados == {"ok": "OK", "malo": "FATAL"}

    def test_comprime_el_jsonl_y_su_log_y_los_saca_del_directorio(self, entorno):
        poner_log(entorno, "a", [ev_inicio(nombre="a"), ev_fin()])

        auditar.analizar_logs(entorno)

        assert list(entorno["log_dir"].glob("*.jsonl")) == []
        assert list(entorno["log_dir"].glob("*.log")) == []
        zips = list(entorno["log_dir"].glob("logs_*.zip"))
        assert len(zips) == 1
        with zipfile.ZipFile(zips[0]) as z:
            nombres = [n.split("_", 1)[1] for n in z.namelist()]
        assert sorted(nombres) == ["a.jsonl", "a.log"]

    def test_un_log_sin_jsonl_se_archiva_pero_no_genera_fila(self, entorno):
        """Un .log huérfano (de una corrida vieja) no tiene eventos que
        resumir. Se archiva igual para que log_dir no lo acumule."""
        poner_log(entorno, "a", [ev_inicio(nombre="a"), ev_fin()])
        (entorno["log_dir"] / "viejo.log").write_text("sin jsonl", encoding="utf-8")

        auditar.analizar_logs(entorno)

        assert len(leer_csv(entorno)) == 1
        assert list(entorno["log_dir"].glob("*.log")) == []
        with zipfile.ZipFile(next(entorno["log_dir"].glob("logs_*.zip"))) as z:
            assert any(n.endswith("viejo.log") for n in z.namelist())

    def test_jsonl_truncado_igual_genera_fila(self, entorno):
        """Sin evento `fin` el proceso murió a mitad, pero lo que bajó cuenta."""
        poner_log(
            entorno,
            "cortado",
            [ev_inicio(nombre="cortado"), ev_archivo(r"G:\a.jpg")],
        )

        auditar.analizar_logs(entorno)

        fila = leer_csv(entorno)[0]
        assert fila["Nuevos"] == "1"
        assert fila["Returncode"] == "-1"

    def test_dos_corridas_acumulan_en_el_mismo_csv(self, entorno):
        """El CSV es append-only: la segunda auditoría no pisa la primera."""
        poner_log(entorno, "a", [ev_inicio(nombre="a"), ev_fin()])
        auditar.analizar_logs(entorno)
        poner_log(entorno, "b", [ev_inicio(nombre="b"), ev_fin()])
        auditar.analizar_logs(entorno)

        assert len(leer_csv(entorno)) == 2

    def test_sin_logs_ni_part_no_escribe_csv(self, entorno):
        auditar.analizar_logs(entorno)
        assert not entorno["audit_csv"].exists()


class TestRegistrarFilas:
    def test_escribe_el_header_una_sola_vez(self, tmp_path):
        csv_path = tmp_path / "a.csv"
        fila = ["x"] * len(auditar.CSV_HEADER)
        auditar.registrar_filas_en_csv(csv_path, [fila])
        auditar.registrar_filas_en_csv(csv_path, [fila])

        texto = csv_path.read_text(encoding="utf-8")
        assert texto.count("Nombre_Modelo") == 1
        assert texto.startswith("sep=;")  # para que Excel separe columnas solo

    def test_sin_filas_no_crea_el_archivo(self, tmp_path):
        csv_path = tmp_path / "a.csv"
        auditar.registrar_filas_en_csv(csv_path, [])
        assert not csv_path.exists()


class TestMantenimiento:
    def test_purga_zips_viejos_y_conserva_los_nuevos(self, tmp_path):
        viejo = tmp_path / "logs_2020-01-01.zip"
        nuevo = tmp_path / f"logs_{datetime.now().strftime('%Y-%m-%d')}.zip"
        otro = tmp_path / "no_es_un_zip_de_logs.zip"
        for f in (viejo, nuevo, otro):
            f.write_bytes(b"")

        assert auditar.purgar_zip_antiguos(tmp_path, dias=60) == 1
        assert not viejo.exists()
        assert nuevo.exists() and otro.exists()

    def test_part_huerfanos_ignora_la_carpeta_de_logs(self, tmp_path):
        """Los .part de descargas van en Rips; un .part dentro de logs sería
        del propio proceso de auditoría y no un archivo abandonado."""
        (tmp_path / "modelo").mkdir()
        (tmp_path / "logs").mkdir()
        (tmp_path / "modelo" / "a.jpg.part").write_bytes(b"")
        (tmp_path / "logs" / "b.part").write_bytes(b"")

        encontrados = auditar.buscar_part_huerfanos(tmp_path)

        assert len(encontrados) == 1
        assert encontrados[0].endswith("a.jpg.part")

    def test_zip_no_borra_los_originales_si_no_pudo_escribir(self, tmp_path):
        """El borrado va después de cerrar el ZIP: si falla, no se pierde nada."""
        origen = tmp_path / "a.jsonl"
        origen.write_text("{}", encoding="utf-8")
        # log_dir inexistente -> zipfile no puede crear el archivo
        assert auditar.archivar_en_zip(tmp_path / "no" / "existe", [origen]) == 0
        assert origen.exists()


class TestAvisoPostsFallidos:
    def test_cuenta_el_campo_nuevo(self, tmp_path, capsys):
        ruta = tmp_path / "pf.json"
        ruta.write_text(
            json.dumps({"https://x/1": {"posts_con_error": [11, 22]}}), encoding="utf-8"
        )
        auditar.avisar_posts_fallidos(ruta)
        salida = capsys.readouterr().out
        assert "2 post(s)" in salida
        # La instrucción vieja decía "copiar a skip_posts.json", que espera
        # ordinales y no post_id. No debe volver.
        assert "Copiar manualmente" not in salida

    def test_sin_archivo_no_dice_nada(self, tmp_path, capsys):
        auditar.avisar_posts_fallidos(tmp_path / "no_existe.json")
        assert capsys.readouterr().out == ""

    def test_avisa_aunque_no_haya_logs_que_auditar(self, entorno, capsys):
        """Los posts fallidos son un pendiente ACUMULADO, no un subproducto del
        lote recién auditado.

        `python auditar.py` sin logs pendientes es el uso donde el aviso más
        importa —revisar qué quedó colgado— y era justo el que no lo mostraba:
        analizar_logs() retornaba en el early return de "sin logs nuevos",
        varias líneas antes de la llamada a avisar_posts_fallidos().
        """
        entorno["posts_fallidos_file"].write_text(
            json.dumps({"https://x/1": {"posts_con_error": [11, 22, 33]}}),
            encoding="utf-8",
        )

        auditar.analizar_logs(entorno)  # log_dir vacío: no hay nada que auditar

        salida = capsys.readouterr().out
        assert "Sin logs nuevos" in salida
        assert "3 post(s)" in salida, "el aviso quedó del otro lado del return"
