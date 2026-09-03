"""Tests para monitor.py híbrido (watchdog + polling).

Para ejecutar:
    pytest tests/test_monitor_hibrido.py -v
    pytest tests/test_monitor_hibrido.py -v --cov=monitor --cov-report=term-missing
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, mock_open, patch

import pytest

MONITOR_MODULE = "monitor"


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def temp_rips_dir():
    """Crea un directorio temporal vacío y lo limpia al finalizar."""
    tmp = tempfile.mkdtemp(prefix="test_rips_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def temp_part_file(temp_rips_dir):
    """Crea un archivo .part de prueba con contenido dummy."""
    path = os.path.join(temp_rips_dir, "test.part")
    with open(path, "wb") as f:
        f.write(b"x" * 1024)
    return path


@pytest.fixture
def mock_watchdog_module():
    """Crea un mock completo del módulo watchdog para aislar los tests."""
    mock_mod = MagicMock()
    mock_mod.observers = MagicMock()
    mock_mod.events = MagicMock()
    mock_obs = MagicMock()
    mock_obs_class = MagicMock(return_value=mock_obs)
    mock_mod.observers.Observer = mock_obs_class
    mock_handler = MagicMock()
    mock_mod.events.FileSystemEventHandler = mock_handler
    return mock_mod, mock_obs_class, mock_obs


# =============================================================================
# TESTS: es_ruta_local
# =============================================================================


class TestEsRutaLocal:
    """Valida la detección de rutas locales vs remotas."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    def test_windows_unc_path_es_remota(self):
        r"""Las rutas UNC (\\servidor\carpeta) siempre son remotas."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        assert mod.es_ruta_local(r"\\SYNOLOGY\Rips") is False
        assert mod.es_ruta_local(r"\\192.168.1.10\share") is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    def test_windows_unidad_remota_detectada_por_api(self):
        """Si GetDriveTypeW devuelve DRIVE_REMOTE (4), es remota."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4) as mock_api:
            result = mod.es_ruta_local(r"Z:\Rips")
            assert result is False
            mock_api.assert_called_once_with("Z:\\")

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    def test_windows_unidad_fija_detectada_por_api(self):
        """Si GetDriveTypeW devuelve DRIVE_FIXED (3), es local.

        Caso real que motivó este test: G: es un disco interno fijo y la API
        devuelve 3, pero es_ruta_local() comparaba contra (2, 6) creyendo que
        DRIVE_FIXED era 2. Resultado: el disco local se clasificaba como NAS
        y crear_monitor() caía a ModoPolling en vez de usar watchdog.
        """
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=3) as mock_api:
            result = mod.es_ruta_local(r"G:\Rips")
            assert result is True
            mock_api.assert_called_once_with("G:\\")

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    def test_windows_unidad_removible_es_local(self):
        """DRIVE_REMOVABLE (2) — USB/SD — es almacenamiento local.

        ReadDirectoryChangesW funciona sobre medios removibles, así que
        watchdog es válido; lo que hay que excluir es DRIVE_REMOTE (SMB/NAS).
        """
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=2):
            assert mod.es_ruta_local(r"E:\Rips") is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    @pytest.mark.parametrize(
        "drive_type, nombre, esperado",
        [
            (0, "DRIVE_UNKNOWN", False),
            (1, "DRIVE_NO_ROOT_DIR", False),
            (2, "DRIVE_REMOVABLE", True),
            (3, "DRIVE_FIXED", True),
            (4, "DRIVE_REMOTE", False),
            (5, "DRIVE_CDROM", False),
            (6, "DRIVE_RAMDISK", True),
        ],
    )
    def test_windows_tabla_completa_de_tipos_de_unidad(
        self, drive_type, nombre, esperado
    ):
        """Mapa exhaustivo de los 7 códigos de GetDriveTypeW.

        Blinda la tupla de tipos locales contra cambios accidentales: si
        alguien vuelve a alterar los valores aceptados, este test señala
        exactamente qué código quedó mal clasificado.
        """
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=drive_type):
            result = mod.es_ruta_local(r"G:\Rips")
            assert result is esperado, (
                f"{nombre} ({drive_type}) debería dar es_ruta_local={esperado}, "
                f"dio {result}"
            )

    @pytest.mark.skipif(sys.platform != "win32", reason="Solo Windows")
    def test_windows_unidad_fallback_si_api_falla(self):
        """Si GetDriveTypeW lanza excepción, asumir local como fallback."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch(
            "ctypes.windll.kernel32.GetDriveTypeW",
            side_effect=OSError("API no disponible"),
        ):
            result = mod.es_ruta_local(r"H:\Rips")
            assert result is True

    @pytest.mark.skipif(sys.platform != "linux", reason="Solo Linux")
    def test_linux_ruta_local_por_defecto(self):
        """En Linux, sin /proc/mounts o sin coincidencia, asumir local."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert mod.es_ruta_local("/home/user/rips") is True

    @pytest.mark.skipif(sys.platform != "linux", reason="Solo Linux")
    def test_linux_ruta_nfs_es_remota(self):
        """Si /proc/mounts indica nfs, la ruta es remota."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        fake_mounts = "server:/export /mnt/rips nfs4 defaults 0 0\n"
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            assert mod.es_ruta_local("/mnt/rips") is False

    @pytest.mark.skipif(sys.platform != "linux", reason="Solo Linux")
    def test_linux_ruta_cifs_es_remota(self):
        """Si /proc/mounts indica cifs, la ruta es remota."""
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        fake_mounts = "//server/share /mnt/rips cifs defaults 0 0\n"
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            assert mod.es_ruta_local("/mnt/rips") is False

    @pytest.mark.skipif(sys.platform != "linux", reason="Solo Linux")
    def test_linux_unc_path_es_remota(self):
        r"""Las rutas UNC (\\servidor\carpeta) también son remotas en Linux/WSL.

        Antes, es_ruta_local() solo detectaba UNC en la rama Windows: en Linux
        devolvía True (local) y crear_monitor() intentaba usar inotify sobre
        una ruta inexistente.
        """
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        assert mod.es_ruta_local(r"\\SYNOLOGY\Rips") is False
        assert mod.es_ruta_local(r"\\192.168.1.10\share") is False

    @pytest.mark.skipif(sys.platform != "linux", reason="Solo Linux")
    def test_linux_mount_point_respeta_limite_de_directorio(self):
        """Un mount point NAS no debe "contagiar" rutas hermanas por prefijo.

        Con "/mnt/rips" montado por NFS, la ruta "/mnt/rips-backup" es un
        directorio local distinto: un startswith() a secas la clasificaría
        como remota por coincidencia de texto.
        """
        mod = __import__(MONITOR_MODULE, fromlist=["es_ruta_local"])
        fake_mounts = (
            "/dev/sda1 / ext4 rw 0 0\nserver:/export /mnt/rips nfs4 defaults 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            # La ruta realmente montada por NFS sigue siendo remota.
            assert mod.es_ruta_local("/mnt/rips") is False
            assert mod.es_ruta_local("/mnt/rips/modelo") is False
            # La hermana con prefijo parecido es local.
            assert mod.es_ruta_local("/mnt/rips-backup") is True
            assert mod.es_ruta_local("/mnt/ripsaurio") is True


# =============================================================================
# TESTS: ModoPolling
# =============================================================================


class TestModoPolling:
    """Valida el modo de polling (fallback, compatible con NAS)."""

    def test_modo_polling_sin_archivos(self, temp_rips_dir):
        """Sin archivos .part, tick() retorna lista vacía."""
        mod = __import__(MONITOR_MODULE, fromlist=["ModoPolling"])
        monitor = mod.ModoPolling(temp_rips_dir)
        activos = monitor.tick()
        assert activos == []

    def test_modo_polling_detecta_part(self, temp_rips_dir, temp_part_file):
        """Detecta un archivo .part reciente."""
        mod = __import__(MONITOR_MODULE, fromlist=["ModoPolling"])
        monitor = mod.ModoPolling(temp_rips_dir)
        activos = monitor.tick()
        assert len(activos) == 1
        assert activos[0][0] == temp_part_file

    def test_modo_polling_ignora_part_viejo(self, temp_rips_dir):
        """Ignora archivos .part con mtime > 5 segundos."""
        path = os.path.join(temp_rips_dir, "old.part")
        with open(path, "wb") as f:
            f.write(b"x")
        old_mtime = time.time() - 10
        os.utime(path, (old_mtime, old_mtime))
        mod = __import__(MONITOR_MODULE, fromlist=["ModoPolling"])
        monitor = mod.ModoPolling(temp_rips_dir)
        activos = monitor.tick()
        assert activos == []

    def test_modo_polling_no_crash_con_ruta_inexistente(self):
        """No lanza excepción si el directorio no existe."""
        mod = __import__(MONITOR_MODULE, fromlist=["ModoPolling"])
        monitor = mod.ModoPolling("/ruta/que/no/existe/12345")
        activos = monitor.tick()
        assert activos == []


# =============================================================================
# TESTS: ModoWatchdog
# =============================================================================


class TestModoWatchdog:
    """Valida el modo watchdog (eventos push del kernel)."""

    def test_watchdog_registra_archivo(self, temp_rips_dir, mock_watchdog_module):
        """Al recibir un evento on_modified, registra el archivo."""
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            path = os.path.join(temp_rips_dir, "watched.part")
            with open(path, "wb") as f:
                f.write(b"data")
            monitor._registrar(path)
            activos = monitor.tick()
            assert len(activos) == 1
            assert activos[0][0] == path

    def test_watchdog_elimina_archivo(self, temp_rips_dir, mock_watchdog_module):
        """Al recibir on_deleted, elimina el archivo del dict."""
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            path = os.path.join(temp_rips_dir, "gone.part")
            with open(path, "wb") as f:
                f.write(b"data")
            monitor._registrar(path)
            monitor._eliminar(path)
            activos = monitor.tick()
            assert activos == []

    def test_watchdog_limpia_vencidos(self, temp_rips_dir, mock_watchdog_module):
        """Elimina archivos inactivos tras 5 segundos."""
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            path = os.path.join(temp_rips_dir, "stale.part")
            with open(path, "wb") as f:
                f.write(b"data")
            monitor._registrar(path)

            # Recién registrado: sigue activo.
            assert len(monitor.tick()) == 1

            # Simulamos que pasaron 10s sin que llegara ningún evento nuevo,
            # envejeciendo la marca del registro (no el mtime del archivo).
            with monitor._lock:
                marca, tam = monitor._activos[path]
                monitor._activos[path] = (marca - 10, tam)

            assert monitor.tick() == []

    def test_watchdog_no_purga_por_mtime_obsoleto(
        self, temp_rips_dir, mock_watchdog_module
    ):
        """Un mtime viejo NO debe purgar un archivo con evento reciente.

        Regresión del bug de NTFS: la hora de última escritura no se
        actualiza mientras gallery-dl mantiene abierto el handle del .part,
        así que un archivo que crece activamente tenía mtime "antiguo" y
        tick() lo purgaba, mostrando "Sin descarga activa" en plena descarga.
        La frescura debe venir del evento, no del mtime.
        """
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            path = os.path.join(temp_rips_dir, "creciendo.part")
            with open(path, "wb") as f:
                f.write(b"data")

            # mtime muy viejo, como lo reporta NTFS durante una descarga.
            viejo = time.time() - 3600
            os.utime(path, (viejo, viejo))

            # Pero el evento acaba de llegar.
            monitor._registrar(path)

            activos = monitor.tick()
            assert len(activos) == 1, (
                "el archivo se purgó por mtime obsoleto pese a tener un evento reciente"
            )
            assert activos[0][0] == path

    def test_watchdog_observer_iniciado(self, temp_rips_dir, mock_watchdog_module):
        """El observer se crea, programa y arranca correctamente."""
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            mock_obs_class.assert_called_once()
            mock_obs.schedule.assert_called_once()
            call_args = mock_obs.schedule.call_args
            assert call_args[0][1] == temp_rips_dir
            mock_obs.start.assert_called_once()


# =============================================================================
# TESTS: crear_monitor (Factory)
# =============================================================================


class TestCrearMonitor:
    """Valida la lógica de auto-detección del monitor híbrido."""

    def test_ruta_remota_usa_polling(self, temp_rips_dir):
        """Una ruta remota siempre usa ModoPolling.

        Se fuerza es_ruta_local() -> False para probar SOLO el despacho de
        crear_monitor(), sin depender de cómo cada plataforma detecte lo
        remoto (eso lo cubren los tests de TestEsRutaLocal).
        """
        mod = __import__(MONITOR_MODULE, fromlist=["crear_monitor"])
        with patch.object(mod, "es_ruta_local", return_value=False):
            monitor = mod.crear_monitor(r"\\server\share")
        assert isinstance(monitor, mod.ModoPolling)

    def test_ruta_local_sin_watchdog_usa_polling(self, temp_rips_dir):
        """Sin watchdog instalado, cae a polling."""
        mod = __import__(MONITOR_MODULE, fromlist=["crear_monitor"])
        with patch.dict("sys.modules", {"watchdog": None}):
            real_import = __builtins__["__import__"]

            def fake_import(name, *args, **kwargs):
                if name == "watchdog":
                    raise ImportError("No module named watchdog")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                monitor = mod.crear_monitor(temp_rips_dir)
                assert isinstance(monitor, mod.ModoPolling)

    def test_ruta_local_con_watchdog_usa_watchdog(
        self, temp_rips_dir, mock_watchdog_module
    ):
        """Con watchdog disponible y ruta local, usa ModoWatchdog."""
        mock_mod, _, _ = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["crear_monitor"])
            # Forzamos es_ruta_local() -> True sin depender del tipo de unidad
            # real donde viva %TEMP% en la máquina que corre el test.
            with patch.object(mod, "es_ruta_local", return_value=True):
                monitor = mod.crear_monitor(temp_rips_dir)
            assert isinstance(monitor, mod.ModoWatchdog)


# =============================================================================
# TESTS: Concurrencia
# =============================================================================


class TestConcurrencia:
    """Valida thread-safety del dict compartido en ModoWatchdog."""

    def test_watchdog_thread_safe(self, temp_rips_dir, mock_watchdog_module):
        """10 threads escribiendo simultáneamente no corrompen el dict."""
        mock_mod, mock_obs_class, mock_obs = mock_watchdog_module
        with patch.dict(
            "sys.modules",
            {
                "watchdog": mock_mod,
                "watchdog.observers": mock_mod.observers,
                "watchdog.events": mock_mod.events,
            },
        ):
            mod = __import__(MONITOR_MODULE, fromlist=["ModoWatchdog"])
            monitor = mod.ModoWatchdog(temp_rips_dir)
            paths = []
            for i in range(10):
                p = os.path.join(temp_rips_dir, f"thread_{i}.part")
                with open(p, "wb") as f:
                    f.write(b"x")
                paths.append(p)

            def worker(path_list):
                for p in path_list:
                    monitor._registrar(p)

            threads = []
            for i in range(10):
                t = threading.Thread(target=worker, args=([paths[i]],))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            activos = monitor.tick()
            assert len(activos) == 10


# =============================================================================
# TESTS: dibujar_panel — el panel no puede desbordar su propio alto
# =============================================================================


class TestPanelNoDesborda:
    """El panel se dibuja con filas ABSOLUTAS (`goto`). Si lo que escribe supera
    el alto del panel de Windows Terminal (`--size 0.35`, unas 11 filas), el
    salto de línea de la última fila hace scrollear el buffer: la cabecera sube,
    lo ya dibujado sube con ella, y el frame siguiente vuelve a escribir en la
    misma fila absoluta. Los frames viejos quedan en pantalla en vez de ser
    sobrescritos — se veían .part ya terminados junto a 'Sin descarga activa'.
    """

    def _dibujar(self, activos, alto, carpeta=None, ancho=100):
        mod = __import__(MONITOR_MODULE, fromlist=["dibujar_panel"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.dibujar_panel(
                activos, {}, 0, "G:/Rips", "WATCHDOG", carpeta, alto=alto, ancho=ancho
            )
        return buf.getvalue()

    @staticmethod
    def _activos(n):
        return [(f"G:/Rips/Simpcity/a{i}.jpg.part", f"a{i}.jpg.part", 1024) for i in range(n)]

    def test_nunca_escribe_mas_filas_que_el_alto_del_panel(self):
        salida = self._dibujar(self._activos(10), alto=12)
        assert salida.count("\n") <= 11

    @pytest.mark.parametrize("alto", [3, 8, 11, 12, 24, 60])
    def test_el_cursor_nunca_baja_de_la_ultima_fila(self, alto):
        """Cada salto de línea baja una fila desde la 1. Si el cursor pasa de la
        última, el terminal scrollea y las filas absolutas dejan de valer."""
        salida = self._dibujar(self._activos(10), alto=alto)
        assert 1 + salida.count(chr(10)) <= alto

    def test_avisa_de_los_archivos_que_no_caben(self):
        salida = self._dibujar(self._activos(10), alto=12)
        assert "más" in salida

    def test_muestra_todos_los_archivos_si_caben(self):
        salida = self._dibujar(self._activos(2), alto=40)
        assert "a0.jpg.part" in salida and "a1.jpg.part" in salida
        assert "más" not in salida

    def test_repinta_la_cabecera_en_cada_frame(self):
        """Sin repintar desde la fila 1, un solo scroll desalinea todo lo demás."""
        salida = self._dibujar([], alto=24)
        assert salida.startswith("\033[1;0H")
        assert "MONITOR" in salida and "WATCHDOG" in salida

    def test_borra_hasta_el_final_del_panel(self):
        """Un frame con menos archivos que el anterior no puede dejar restos."""
        assert self._dibujar([], alto=24).endswith("\033[J")


# =============================================================================
# TESTS: intervalo de refresco segun el modo
# =============================================================================


class TestIntervaloPorModo:
    """En polling cada tick es un `os.walk()` que puede caer sobre un NAS, así
    que el refresco rápido solo vale para watchdog, donde el tick lee un dict."""

    def _mod(self):
        return __import__(MONITOR_MODULE, fromlist=["intervalo_por_modo"])

    def test_watchdog_refresca_rapido(self):
        assert self._mod().intervalo_por_modo("WATCHDOG") == 0.25

    def test_polling_se_queda_en_un_segundo(self):
        assert self._mod().intervalo_por_modo("POLLING") == 1.0

    def test_lo_pedido_por_linea_de_comandos_manda(self):
        assert self._mod().intervalo_por_modo("WATCHDOG", 3.0) == 3.0
        assert self._mod().intervalo_por_modo("POLLING", 0.1) == 0.1


# =============================================================================
# TESTS: la fila de descarga y la carpeta pegada
# =============================================================================


def _sin_ansi(texto):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", texto)


class TestCarpetaPegada:
    """`gallery-dl_win.conf` trae `concurrent: 1`, así que entre archivo y
    archivo no hay ningún .part activo. Con refresco de 0.25s, recalcular la
    carpeta desde cero haría parpadear la línea una vez por archivo."""

    def _mod(self):
        return __import__(MONITOR_MODULE, fromlist=["carpeta_de"])

    @staticmethod
    def _act(ruta):
        return [(ruta, os.path.basename(ruta), 1024)]

    def test_sin_activos_conserva_la_ultima(self):
        assert self._mod().carpeta_de([], "G:/Rips", "~/Simpcity/shoe0nhead/") == (
            "~/Simpcity/shoe0nhead/"
        )

    def test_sin_activos_y_sin_previa_no_inventa(self):
        assert self._mod().carpeta_de([], "G:/Rips") is None

    def test_la_saca_del_part_activo(self):
        act = self._act("G:/Rips/Simpcity/shoe0nhead/a.jpg.part")
        assert self._mod().carpeta_de(act, "G:/Rips") == "~/Simpcity/shoe0nhead/"

    def test_normaliza_las_barras_de_windows(self):
        ruta = "G:" + chr(92) + "Rips" + chr(92) + "Simpcity" + chr(92) + "shoe0nhead" + chr(92) + "a.jpg.part"
        act = self._act(ruta)

    def test_cambia_al_saltar_de_hilo(self):
        act = self._act("G:/Rips/Simpcity/olivia-sun/b.jpg.part")
        previa = "~/Simpcity/shoe0nhead/"
        assert self._mod().carpeta_de(act, "G:/Rips", previa) == "~/Simpcity/olivia-sun/"

    def test_usa_el_mas_reciente_no_el_part_huerfano(self):
        """activos viene ordenado por mtime descendente: el primero es el vivo."""
        act = [
            ("G:/Rips/Simpcity/olivia-sun/b.jpg.part", "b.jpg.part", 10),
            ("G:/Rips/Simpcity/shoe0nhead/viejo.jpg.part", "viejo.jpg.part", 10),
        ]
        assert self._mod().carpeta_de(act, "G:/Rips") == "~/Simpcity/olivia-sun/"


class TestFilaDeDescarga:
    """Una fila por archivo, en columnas fijas. Un nombre que envolviera de
    línea metería una fila extra y volvería a desalinear el panel entero."""

    def _dibujar(self, activos, alto=24, ancho=100, carpeta=None):
        mod = __import__(MONITOR_MODULE, fromlist=["dibujar_panel"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.dibujar_panel(
                activos, {}, 0, "G:/Rips", "WATCHDOG", carpeta, alto=alto, ancho=ancho
            )
        return buf.getvalue()

    def test_un_archivo_ocupa_una_sola_fila(self):
        act = [("G:/Rips/a/uno.jpg.part", "uno.jpg.part", 1024)]
        # 2 de cabecera + 1 de carpeta + 1 del archivo = 4 filas, 3 saltos
        salida = self._dibujar(act, carpeta="~/a/")
        assert _sin_ansi(salida).count("\n") == 3

    def test_ninguna_fila_pasa_del_ancho(self):
        largo = "x" * 120 + "_muy_largo.jpg.part"
        act = [(f"G:/Rips/a/{largo}", largo, 1024)]
        salida = _sin_ansi(self._dibujar(act, ancho=60, carpeta="~/a/"))
        assert all(len(f) <= 60 for f in salida.split("\n"))

    def test_el_nombre_se_recorta_por_el_principio(self):
        """La cola lleva la extensión y el identificador; el principio no."""
        largo = "prefijo_irrelevante_" * 6 + "12285-8cff76.mp4.part"
        act = [(f"G:/Rips/a/{largo}", largo, 1024)]
        salida = _sin_ansi(self._dibujar(act, ancho=70, carpeta="~/a/"))
        assert "12285-8cff76.mp4.part" in salida
        assert "prefijo_irrelevante_prefijo" not in salida

    def test_la_carpeta_aparece_sobre_la_fila(self):
        act = [("G:/Rips/Simpcity/shoe0nhead/a.jpg.part", "a.jpg.part", 1024)]
        filas = _sin_ansi(self._dibujar(act, carpeta="~/Simpcity/shoe0nhead/")).split("\n")
        i_carp = next(i for i, f in enumerate(filas) if "~/Simpcity/shoe0nhead/" in f)
        i_arch = next(i for i, f in enumerate(filas) if "a.jpg.part" in f)
        assert i_carp == i_arch - 1

    def test_sin_carpeta_conocida_no_dibuja_la_fila(self):
        """Al arrancar, antes del primer .part, no hay carpeta que mostrar."""
        assert "~/" not in _sin_ansi(self._dibujar([], carpeta=None))


class TestCabeceraRespetaElAncho:
    """Si la cabecera no cabe a lo ancho, envuelve de línea y mete una fila
    extra: el mismo desborde que `dibujar_panel()` existe para impedir."""

    def _cab(self, ancho, rips="G:/Rips"):
        mod = __import__(MONITOR_MODULE, fromlist=["lineas_cabecera"])
        return _sin_ansi(mod.lineas_cabecera(rips, "WATCHDOG", ancho)[0])

    @pytest.mark.parametrize("ancho", [12, 30, 40, 62, 120])
    def test_nunca_pasa_del_ancho(self, ancho):
        assert len(self._cab(ancho)) <= ancho

    def test_suelta_el_ctrl_c_antes_de_recortar(self):
        assert "Ctrl+C" not in self._cab(40)
        assert "G:/Rips" in self._cab(40)

    def test_suelta_la_ruta_si_tampoco_cabe(self):
        cab = self._cab(30)
        assert "G:/Rips" not in cab and "WATCHDOG" in cab

    def test_con_una_ruta_larga_no_desborda(self):
        largo = "G:/Rips/" + "carpeta_muy_larga/" * 8
        assert len(self._cab(62, largo)) <= 62
