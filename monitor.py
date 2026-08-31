#!/usr/bin/env python3
"""
monitor.py — Muestra en tiempo real archivos .part activos en G:/Rips
Uso: python monitor.py [--rips-dir G:/Rips] [--intervalo 1]

v3.0 — Híbrido: watchdog (eventos push) para rutas locales, polling (os.walk) fallback.
       Auto-detecta si la ruta es local o remota (NAS/SMB).
       Si watchdog no está instalado, cae graceful a polling.
       Si la ruta es de red, usa polling directamente.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
def cargar_rips_dir() -> str:
    script_dir = Path(__file__).parent
    config_path = script_dir / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entorno = "windows" if IS_WINDOWS else "linux"
            raw = data.get(entorno, {}).get("paths", {}).get("rips_dir", "")
            if raw:
                return os.path.expandvars(os.path.expanduser(raw))
        except (json.JSONDecodeError, KeyError):
            pass
    return r"G:\\Rips" if IS_WINDOWS else os.path.expanduser("~/Rips")


RIPS_DIR = cargar_rips_dir()

VENTANA_ACTIVO = 5  # solo archivos .part que crecieron en los últimos 5s
VENTANA_VEL = 3

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
GRAY = "\033[90m"
RED = "\033[31m"
MAGENTA = "\033[35m"
CLEAR = "\033[2K"
SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def hide_cursor():
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def show_cursor():
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def goto(row):
    sys.stdout.write(f"\033[{row};0H")


def fmt_bytes(b):
    if b >= 1024**3:
        return f"{b / 1024**3:.2f} GB"
    if b >= 1024**2:
        return f"{b / 1024**2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b} B"


def ruta_corta(ruta_completa, rips_dir, maxlen=60):
    rel = ruta_completa.replace(rips_dir, "").lstrip("\\/")
    return ("..." + rel[-(maxlen - 3) :]) if len(rel) > maxlen else rel


# =============================================================================
# AUTO-DETECCIÓN: ¿Ruta local o remota?
# =============================================================================
def es_ruta_local(ruta: str) -> bool:
    r"""
    Detecta si una ruta apunta a un filesystem local o remoto (NAS/SMB/NFS).

    Windows:
      - UNC paths (\\servidor\carpeta) → remoto
      - Unidades mapeadas (Z:) que apuntan a red → remoto
      - Unidades fijas locales (C:, D:, G:) → local

    Linux:
      - Consulta /proc/mounts para detectar nfs, cifs, fuse, sshfs
      - Rutas en /tmp, /home, etc. locales → local
    """
    ruta = os.path.normpath(os.path.expandvars(os.path.expanduser(ruta)))

    if IS_WINDOWS:
        # UNC path: siempre remoto
        if ruta.startswith("\\\\"):
            return False

        # Letra de unidad: verificar si es local con GetDriveTypeW
        if re.match(r"^[A-Za-z]:", ruta):
            try:
                import ctypes

                drive = ruta[:2] + "\\"
                # DRIVE_FIXED = 2, DRIVE_REMOTE = 4, DRIVE_RAMDISK = 6
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive)
                # 0 = UNKNOWN, 1 = NO_ROOT_DIR, 4 = REMOTE, 5 = CDROM → no local
                # 2 = REMOVABLE, 3 = FIXED, 6 = RAMDISK → local
                return drive_type in (2, 3, 6)
            except Exception:
                # Si falla la API, asumir local como fallback conservador
                return True

        # Rutas relativas o extrañas: asumir local
        return True

    else:
        # UNC path (\\servidor\carpeta): remoto también en Linux/WSL.
        # Se comprueba ANTES de abspath() porque en Linux el backslash no es
        # separador de ruta y abspath() lo convertiría en algo como
        # "/cwd/\\servidor\carpeta", perdiendo la forma UNC original.
        if ruta.startswith("\\\\"):
            return False

        # Linux: consultar /proc/mounts
        ruta_abs = os.path.abspath(ruta)
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                for linea in f:
                    partes = linea.split()
                    if len(partes) < 3:
                        continue
                    mount_point = partes[1]
                    fs_type = partes[2]
                    # Si la ruta está bajo este mount point.
                    # Se compara respetando el límite de directorio: un
                    # startswith() a secas haría que "/mnt/rips-backup"
                    # (local) matcheara el mount point "/mnt/rips" (NAS).
                    if ruta_abs == mount_point or ruta_abs.startswith(
                        mount_point.rstrip("/") + "/"
                    ):
                        if fs_type in (
                            "nfs",
                            "nfs4",
                            "cifs",
                            "fuse",
                            "fuse.sshfs",
                            "fuse.rclone",
                        ):
                            return False
        except OSError:
            pass
        return True


# =============================================================================
# MODO POLLING (clásico, sin cambios funcionales)
# =============================================================================
class ModoPolling:
    """Monitor clásico con os.walk(). Funciona siempre, en cualquier entorno."""

    def __init__(self, rips_dir):
        self.rips_dir = rips_dir
        self.historiales = {}
        self.modo_str = "POLLING"

    def get_active_parts(self):
        """Devuelve lista de archivos .part que crecieron en los últimos 5s."""
        ahora = time.time()
        candidatos = []
        for root, _, files in os.walk(self.rips_dir):
            for fname in files:
                if fname.endswith(".part"):
                    ruta = os.path.join(root, fname)
                    try:
                        st = os.stat(ruta)
                        if (ahora - st.st_mtime) <= VENTANA_ACTIVO:
                            candidatos.append((st.st_mtime, ruta, fname, st.st_size))
                    except OSError:
                        pass
        candidatos.sort(key=lambda x: x[0], reverse=True)
        return [(r, n, t) for _, r, n, t in candidatos]

    def tick(self):
        """Un ciclo de actualización. Devuelve la lista de activos."""
        return self.get_active_parts()

    def shutdown(self):
        """Limpieza al cerrar. En polling no hay nada que limpiar."""


# =============================================================================
# MODO WATCHDOG (eventos push del SO)
# =============================================================================
class ModoWatchdog:
    """
    Monitor basado en eventos del filesystem (watchdog).

    El SO notifica cada cambio en archivos .part. Un thread del Observer
    recibe los eventos y actualiza un dict compartido (protegido por Lock).
    El thread principal lee ese dict cada 100ms y renderiza la UI.

    Debounce implícito: la UI refresca a 10 FPS, así que miles de eventos
    por segundo se colapsan naturalmente en ~10 frames.
    """

    def __init__(self, rips_dir):
        self.rips_dir = rips_dir
        self._lock = threading.Lock()
        # ruta -> (mtime, size)
        self._activos = {}
        self.historiales = {}
        self.modo_str = "WATCHDOG"
        self._observer = None
        self._inicializar()

    def _inicializar(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class PartHandler(FileSystemEventHandler):
            def __init__(self, parent):
                self.parent = parent

            def on_modified(self, event):
                if not event.is_directory and event.src_path.endswith(".part"):
                    self.parent._registrar(event.src_path)

            def on_created(self, event):
                if not event.is_directory and event.src_path.endswith(".part"):
                    self.parent._registrar(event.src_path)

            def on_deleted(self, event):
                if not event.is_directory and event.src_path.endswith(".part"):
                    self.parent._eliminar(event.src_path)

            def on_moved(self, event):
                # Si un .part se renombra (termina descarga), eliminar el viejo
                if event.src_path.endswith(".part"):
                    self.parent._eliminar(event.src_path)
                # Si el destino es .part (raro, pero posible), registrar
                if hasattr(event, "dest_path") and event.dest_path.endswith(".part"):
                    self.parent._registrar(event.dest_path)

        self._observer = Observer()
        self._observer.schedule(PartHandler(self), self.rips_dir, recursive=True)
        self._observer.start()

    def _registrar(self, ruta):
        try:
            st = os.stat(ruta)
            with self._lock:
                self._activos[ruta] = (time.time(), st.st_size)
        except OSError:
            # El archivo puede haber desaparecido entre el evento y el stat
            pass

    def _eliminar(self, ruta):
        """Elimina un archivo .part de la tabla."""
        with self._lock:
            self._activos.pop(ruta, None)

    def tick(self):
        """
        Devuelve la lista de archivos .part activos (<=5s sin crecer).
        Limpia entradas vencidas para no mostrar archivos que dejaron de crecer.
        """
        ahora = time.time()
        with self._lock:
            # Limpiar archivos que no crecieron en los últimos 5s
            vencidos = [
                r for r, (m, _) in self._activos.items() if (ahora - m) > VENTANA_ACTIVO
            ]
            for r in vencidos:
                del self._activos[r]

            # Devolver como lista de tuplas (ruta, nombre, tamanio)
            resultado = []
            for ruta, (mtime, size) in self._activos.items():
                resultado.append((ruta, os.path.basename(ruta), size))

            # Ordenar por mtime descendente (más reciente primero)
            resultado.sort(key=lambda x: self._activos[x[0]][0], reverse=True)
            return resultado

    def shutdown(self):
        """Detiene el observer de watchdog de forma segura."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)


# =============================================================================
# FACTORY: Auto-detecta el mejor modo
# =============================================================================
def crear_monitor(rips_dir: str):
    """
    Crea el monitor adecuado para el entorno:
      1. Si la ruta es remota (NAS/SMB/NFS) → ModoPolling (garantizado)
      2. Si la ruta es local y watchdog está instalado → ModoWatchdog (eficiente)
      3. Si la ruta es local pero watchdog no está → ModoPolling (fallback)
    """
    if not es_ruta_local(rips_dir):
        print(f"  {YELLOW}[POLLING] Ruta remota/NAS detectada. Usando os.walk(){RESET}")
        return ModoPolling(rips_dir)

    try:
        import watchdog  # noqa: F401

        print(
            f"  {GREEN}[WATCHDOG] Ruta local detectada. Usando eventos push del SO{RESET}"
        )
        return ModoWatchdog(rips_dir)
    except ImportError:
        print(
            f"  {YELLOW}[POLLING] watchdog no instalado (pip install watchdog). Usando os.walk(){RESET}"
        )
        return ModoPolling(rips_dir)


# =============================================================================
# RENDERIZADO DE UI (compartido entre modos)
# =============================================================================
HEADER_LINES = 6  # +1 línea para el banner de modo


def dibujar_panel(activos, historiales, spin_idx, rips_dir, ultimas_filas, modo_str):
    """Dibuja el panel de archivos .part activos."""
    ahora = time.monotonic()
    lineas = []

    if not activos:
        lineas.append(f"  {GRAY}Sin descarga activa — esperando .part...{RESET}")
        lineas.append("")
    else:
        for ruta, nombre, tamanio in activos:
            rel = ruta_corta(ruta, rips_dir)
            hist = historiales.get(ruta, deque())

            hist.append((ahora, tamanio))
            while hist and (ahora - hist[0][0]) > VENTANA_VEL:
                hist.popleft()

            vel_str = "—"
            color = YELLOW
            spin = "⏸"
            if len(hist) >= 2:
                t0, s0 = hist[0]
                delta_t = ahora - t0
                delta_b = tamanio - s0
                if delta_t > 0 and delta_b > 0:
                    vel_str = fmt_bytes(delta_b / delta_t) + "/s"
                    color = GREEN
                    spin = SPINNERS[spin_idx % len(SPINNERS)]

            lineas.append(f"  {GRAY}📄{RESET} {YELLOW}{rel}{RESET}")
            lineas.append(
                f"  {color}{spin}{RESET}  {BOLD}{fmt_bytes(tamanio)}{RESET} descargados   {color}{vel_str}{RESET}"
            )
            lineas.append("")

    filas_necesarias = len(lineas)
    filas_a_borrar = max(0, ultimas_filas - filas_necesarias)

    goto(HEADER_LINES + 1)
    for linea in lineas:
        sys.stdout.write(f"{CLEAR}{linea}\n")
    for _ in range(filas_a_borrar):
        sys.stdout.write(f"{CLEAR}\n")

    sys.stdout.flush()
    return filas_necesarias


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rips-dir", default=RIPS_DIR)
    parser.add_argument("--intervalo", default=1, type=float)
    args = parser.parse_args()

    CENTINELA = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "descarga.running"
    )

    rips_dir = args.rips_dir
    intervalo = args.intervalo

    if IS_WINDOWS:
        os.system("")  # activar ANSI
        time.sleep(1)  # esperar que el panel de WT termine de abrirse
        os.system("cls")  # limpiar residuo visual

    hide_cursor()

    # Crear monitor (auto-detecta watchdog vs polling)
    monitor = crear_monitor(rips_dir)

    sys.stdout.write("\033[2J\033[H")
    print(f"{CYAN}{BOLD}  {'═' * 58}{RESET}")
    print(f"{CYAN}{BOLD}  MONITOR DE DESCARGA ACTIVA{RESET}")
    print(f"{GRAY}  Dir: {rips_dir}{RESET}")
    print(f"{GRAY}  Ventana activa: {VENTANA_ACTIVO}s | Ctrl+C para salir{RESET}")
    if monitor.modo_str == "WATCHDOG":
        color_modo = GREEN  # ✅ Eficiente: eventos push del kernel
    else:
        color_modo = YELLOW  # ⚠️ Fallback: os.walk() cada 1s

    print(f"{color_modo}{BOLD}  [{monitor.modo_str}]{RESET}")
    print(f"{CYAN}{BOLD}  {'═' * 58}{RESET}")
    sys.stdout.flush()

    spin_idx = 0
    ultimas_filas = 0

    TIEMPO_MAXIMO_SIN_CENTINELA_NUEVO = 7200  # 2 horas
    motivo_cierre = "normal"

    try:
        while True:
            if not os.path.exists(CENTINELA):
                break

            try:
                mtime = os.path.getmtime(CENTINELA)
                if time.time() - mtime > TIEMPO_MAXIMO_SIN_CENTINELA_NUEVO:
                    motivo_cierre = "stale"
                    print(
                        f"\n  {GRAY}[MONITOR] Centinela stale detectado (>2h). Cerrando.{RESET}"
                    )
                    break
            except OSError:
                break

            # Obtener activos del modo actual
            activos = monitor.tick()

            # Asegurar historiales para archivos activos
            rutas_activas = {r for r, _, _ in activos}
            for ruta, _, _ in activos:
                if ruta not in monitor.historiales:
                    monitor.historiales[ruta] = deque()

            # Limpiar historiales de archivos que ya no están activos
            for r in list(monitor.historiales):
                if r not in rutas_activas:
                    del monitor.historiales[r]

            ultimas_filas = dibujar_panel(
                activos,
                monitor.historiales,
                spin_idx,
                rips_dir,
                ultimas_filas,
                monitor.modo_str,
            )
            spin_idx += 1
            time.sleep(intervalo)

    except KeyboardInterrupt:
        pass
    finally:
        show_cursor()
        goto(HEADER_LINES + ultimas_filas + 2)
        if motivo_cierre == "stale":
            print(
                f"{YELLOW}  [!] El monitor se cerro porque descarga.running no se "
                f"actualizo en 2h (posible proceso colgado o cerrado a la fuerza).{RESET}"
            )
            print(
                f"{GRAY}  Revisa si descarga.py sigue vivo y el ultimo log en Rips/logs.{RESET}\n"
            )
            time.sleep(6)
        else:
            print(f"{GRAY}  Descarga terminada. Cerrando monitor...{RESET}\n")
            time.sleep(1)

        # Limpieza graceful del modo watchdog
        monitor.shutdown()


if __name__ == "__main__":
    main()
