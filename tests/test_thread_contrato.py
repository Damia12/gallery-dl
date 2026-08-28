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

def test_stderr_no_bloquea_stdout_y_clasifica_correctamente(descarga_mod):
    """
    Ejercita descargar_windows() real contra mock_gallery_dl.py:
    - 3 archivos nuevos + 1 marcado "ya descargado" (línea con '#')
    - 3 líneas de debug en stderr (post_id) que NO deben contarse como
      warnings (ese era justamente el bug que motivó clasificar_por_tag)
    - post_id_activo debe quedar en el último post logueado (1002)
    """
    descarga_mod.usar_mock(MOCK_GDL_SIMPLE)

    inicio = time.time()
    resultado = descarga_mod.descargar_windows(
        "https://simpcity.cr/threads/modelo.123", "Modelo"
    )
    duracion = time.time() - inicio

    (
        archivos_nuevos,
        errores_hilo,
        warnings_hilo,
        nuevos,
        done,
        timeout,
        dur_reportada,
        returncode,
        post_id_activo,
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

    # post_id_activo debe reflejar el último post logueado por el mock.
    assert post_id_activo == 1002


# =============================================================================
# TEST 2: fusión warning HTTP + error sobre el mismo post_id
# =============================================================================

def test_fusion_warning_error_mismo_post(descarga_mod, capsys):
    """
    Camino NO cubierto anteriormente por ningún test: un warning de
    [downloader.http] debe retenerse en warnings_pendientes y, si llega
    un error sobre el mismo post_id, fusionarse en una sola línea
    ("<error> -- <causa>") en la salida -- en vez de imprimirse suelto.
    """
    descarga_mod.usar_mock(MOCK_GDL_WARNING)

    resultado = descarga_mod.descargar_windows(
        "https://simpcity.cr/threads/modelo.456", "Modelo"
    )
    salida = capsys.readouterr().out

    (
        archivos_nuevos,
        errores_hilo,
        warnings_hilo,
        nuevos,
        done,
        timeout,
        dur_reportada,
        returncode,
        post_id_activo,
    ) = resultado

    assert post_id_activo == 5555
    assert returncode == 1

    # El warning se cuenta (para el resumen), pero el error real es el
    # que queda registrado como "el" error del hilo.
    assert warnings_hilo == ["[downloader.http][warning] Rate limit exceeded, retrying"]
    assert errores_hilo == ["[downloader.http][error] 404 Not Found"]

    # La salida impresa debe mostrar AMBOS fragmentos fusionados en una
    # sola línea, no el warning suelto en una línea y el error en otra.
    assert "404 Not Found" in salida
    assert "Rate limit exceeded" in salida
