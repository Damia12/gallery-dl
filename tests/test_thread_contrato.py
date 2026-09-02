"""Tests del CONTRATO de descarga.py (motor Windows): stderr en thread
dedicado + clasificación por tag + fusión warning->error.

A diferencia de la versión anterior de este archivo, estos tests importan
y ejecutan las funciones REALES de descarga.py (descargar_windows,
clasificar_por_tag, etc.) en vez de reimplementar el patrón de threading
por separado. Así, si alguien rompe la lógica real (por ejemplo
clasificar_por_tag o la fusión de warnings_pendientes), estos tests fallan.

descarga.py NO se modifica para poder testearlo: como el módulo carga
config.json a nivel de import (y hace sys.exit(1) si no lo encuentra),
el fixture `descarga_mod` copia el archivo real a un directorio temporal
junto a un config.json sintético, e importa esa copia. También
intercepta subprocess.Popen dentro del módulo para que, en vez de
lanzar gallery-dl real, siempre ejecute uno de los mocks locales
(mock_gallery_dl.py / mock_gallery_dl_warning.py).

Para ejecutar:
    pytest tests/test_thread_contrato.py -v
"""

import importlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
DESCARGA_SRC = TESTS_DIR.parent / "descarga.py"  # el real, nunca se toca
MOCK_GDL_SIMPLE = TESTS_DIR / "mock_gallery_dl.py"
MOCK_GDL_WARNING = TESTS_DIR / "mock_gallery_dl_warning.py"


def _config_sintetico(tmp_path: Path) -> dict:
    """Config.json con las mismas claves que el real, pero rutas de mentira
    dentro de tmp_path -- nunca toca la configuración de producción."""
    paths = {
        "rips_dir": str(tmp_path / "Rips"),
        "log_dir": str(tmp_path / "Rips" / "logs"),
        "lista_file": str(tmp_path / "lista.txt"),
        "state_file": str(tmp_path / "state.json"),
        "audit_csv": str(tmp_path / "Rips" / "logs" / "auditoria.csv"),
        "skip_posts_file": str(tmp_path / "skip_posts.json"),
        "posts_fallidos_file": str(tmp_path / "posts_fallidos.json"),
    }
    return {
        "windows": {
            "paths": paths,
            "gallery_dl": {
                "executable": "gallery-dl-fake.exe",
                "config_file": str(tmp_path / "gallery-dl_fake.conf"),
            },
        },
        "linux": {
            "paths": paths,
            "gallery_dl": {
                "executable": "gallery-dl-fake",
                "config_file": str(tmp_path / "gallery-dl_fake.conf"),
            },
        },
        "pipeline": {"batch_size": 5},
    }


@pytest.fixture
def descarga_mod(tmp_path, monkeypatch):
    """Importa una copia aislada de descarga.py, lista para pruebas.

    - `mod.usar_mock(ruta)` define qué script mock ejecuta el siguiente
      subprocess.Popen en lugar de gallery-dl real.
    """
    copia = tmp_path / "descarga.py"
    shutil.copy(DESCARGA_SRC, copia)
    (tmp_path / "config.json").write_text(
        json.dumps(_config_sintetico(tmp_path)), encoding="utf-8"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("descarga", None)
    mod = importlib.import_module("descarga")

    original_popen = mod.subprocess.Popen
    estado = {"mock_script": None}

    def fake_popen(cmd, *args, **kwargs):
        assert estado["mock_script"] is not None, (
            "Llama a mod.usar_mock(ruta) antes de descargar_windows()."
        )
        # Ignoramos el cmd real (gallery-dl-fake -c ...) y siempre
        # ejecutamos el mock elegido, vía el mismo intérprete de Python.
        nuevo_cmd = [sys.executable, str(estado["mock_script"])]
        return original_popen(nuevo_cmd, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod.usar_mock = lambda ruta: estado.update(mock_script=ruta)

    yield mod

    sys.modules.pop("descarga", None)


# =============================================================================
# TEST 1: contrato básico -- stderr no bloquea stdout, clasificación correcta
# =============================================================================

def test_stderr_no_bloquea_stdout_y_clasifica_correctamente(descarga_mod, tmp_path):
    """
    Ejercita descargar_windows() real contra mock_gallery_dl.py:
    - 3 archivos nuevos + 1 marcado "ya descargado" (línea con '#')
    - 3 líneas de debug en stderr (post_id) que NO deben contarse como
      warnings (ese era justamente el bug que motivó clasificar_por_tag)
    - ningún error, así que `posts_con_error` queda vacío: un post que solo
      aparece en una línea de debug NO es un post fallido
    """
    descarga_mod.usar_mock(MOCK_GDL_SIMPLE)
    eventos = descarga_mod.EventLog(tmp_path / "simple.jsonl")

    inicio = time.time()
    resultado = descarga_mod.descargar_windows(
        "https://simpcity.cr/threads/modelo.123", "Modelo", eventos=eventos
    )
    duracion = time.time() - inicio
    eventos.cerrar()

    (
        archivos_nuevos,
        errores_hilo,
        warnings_hilo,
        nuevos,
        done,
        timeout,
        dur_reportada,
        returncode,
        posts_con_error,
    ) = resultado

    # Si stderr bloqueara la lectura de stdout (o viceversa), esta función
    # quedaría colgada hasta el timeout real de actividad (900s), no hasta
    # que el mock termine (~0.3s). Un tiempo razonable confirma el contrato.
    assert duracion < 10, (
        f"descargar_windows tardó {duracion:.1f}s -- posible bloqueo entre "
        f"stdout/stderr (ver leer_stderr / for linea in proceso.stdout)"
    )

    assert len(archivos_nuevos) == 3
    assert nuevos == 3
    assert done == 1
    assert returncode == 0
    assert timeout is False

    # Las líneas "[simpcity][debug] post N: Sleeping..." NO deben contar
    # como warning solo por contener la palabra "sleeping".
    assert errores_hilo == []
    assert warnings_hilo == []

    # Los posts 1000-1002 aparecen en líneas de debug, pero ninguno falló.
    # El código viejo devolvía "el último post visto" (1002) haya fallado o no.
    assert posts_con_error == []

    # Los eventos del .jsonl deben reflejar exactamente lo mismo que la tupla:
    # son la fuente de la que salen el .log y el CSV.
    archivos = [e for e in eventos.eventos if e["t"] == "archivo"]
    assert len(archivos) == 4
    assert sum(1 for e in archivos if e["nuevo"]) == 3
    assert sum(1 for e in archivos if not e["nuevo"]) == 1
    assert [e for e in eventos.eventos if e["t"] in ("error", "warning")] == []


# =============================================================================
# TEST 2: fusión warning HTTP + error sobre el mismo post_id
# =============================================================================

def test_fusion_warning_error_mismo_post(descarga_mod, capsys, tmp_path):
    """
    Un warning de [downloader.http] debe retenerse en warnings_pendientes y,
    si llega un error sobre el mismo post_id, fusionarse en una sola línea
    ("<error> -- <causa>") en la salida -- en vez de imprimirse suelto.

    El mock intercala un post señuelo (9999) entre el warning y el error, así
    que este test cubre además la atribución: el error es del 5555.
    """
    descarga_mod.usar_mock(MOCK_GDL_WARNING)
    eventos = descarga_mod.EventLog(tmp_path / "warning.jsonl")

    resultado = descarga_mod.descargar_windows(
        "https://simpcity.cr/threads/modelo.456", "Modelo", eventos=eventos
    )
    salida = capsys.readouterr().out
    eventos.cerrar()

    (
        archivos_nuevos,
        errores_hilo,
        warnings_hilo,
        nuevos,
        done,
        timeout,
        dur_reportada,
        returncode,
        posts_con_error,
    ) = resultado

    assert returncode == 1

    # El warning se cuenta (para el resumen), pero el error real es el
    # que queda registrado como "el" error del hilo.
    assert warnings_hilo == [
        "[downloader.http][warning] post 5555: Rate limit exceeded, retrying"
    ]
    assert errores_hilo == ["[downloader.http][error] post 5555: 404 Not Found"]

    # La salida impresa debe mostrar AMBOS fragmentos fusionados en una
    # sola línea, no el warning suelto en una línea y el error en otra.
    assert "404 Not Found" in salida
    assert "Rate limit exceeded" in salida


# =============================================================================
# TEST 3: atribución -- el error es del post de SU línea, no del último visto
# =============================================================================


def test_el_error_se_atribuye_a_su_propio_post(descarga_mod, tmp_path):
    """Regresión del bug 2: entre el warning del post 5555 y su error, el mock
    intercala una línea de debug del post 9999. La versión anterior leía la
    variable corriente `post_id_activo` y culpaba al 9999.

    Es el mismo bug que en producción registró el post 51013156 en
    posts_fallidos.json mientras los 49 errores estaban en otros cuatro posts.
    """
    descarga_mod.usar_mock(MOCK_GDL_WARNING)
    eventos = descarga_mod.EventLog(tmp_path / "atribucion.jsonl")

    resultado = descarga_mod.descargar_windows(
        "https://simpcity.cr/threads/modelo.456", "Modelo", eventos=eventos
    )
    eventos.cerrar()
    posts_con_error = resultado[8]

    assert posts_con_error == [5555]
    assert 9999 not in posts_con_error

    errores = [e for e in eventos.eventos if e["t"] == "error"]
    assert len(errores) == 1
    assert errores[0]["post_id"] == 5555
    assert errores[0]["origen"] == "downloader.http"

    # El mensaje del evento lleva la causa fusionada: el error de gallery-dl
    # solo dice que falló, el warning previo dice por qué. Sin esa fusión,
    # auditar2.es_fatal() no tendría el "404" para clasificar.
    assert "404 Not Found" in errores[0]["msg"]
    assert "Rate limit exceeded" in errores[0]["msg"]

    # El warning también queda como evento propio, con su post_id.
    warnings = [e for e in eventos.eventos if e["t"] == "warning"]
    assert [w["post_id"] for w in warnings] == [5555]

    # El .jsonl en disco debe tener exactamente los mismos eventos que la
    # lista en memoria: si el flush fallara, un kill del watchdog los perdería.
    lineas = (tmp_path / "atribucion.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lineas) == len(eventos.eventos)
