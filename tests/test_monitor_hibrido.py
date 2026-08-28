"""Tests para monitor.py híbrido (watchdog + polling).

Para ejecutar:
    pytest tests/test_monitor_hibrido.py -v
    pytest tests/test_monitor_hibrido.py -v --cov=monitor --cov-report=term-missing
"""

import os
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
            old = time.time() - 10
            os.utime(path, (old, old))
            monitor._registrar(path)
            activos = monitor.tick()
            assert activos == []

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
