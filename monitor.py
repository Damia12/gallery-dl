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
import shutil
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

RETENCION_TERMINADO = 1.0  # segundos que un archivo recién completado sigue en pantalla


def _terminado_vigente(terminado, ahora=None, retencion=RETENCION_TERMINADO):
    """Filtra por antigüedad el `(nombre, tamaño, momento)` de lo último bajado.

    Sin esto el panel decía "esperando .part..." casi todo el tiempo: es cierto
    —entre archivo y archivo gallery-dl está pidiendo la siguiente URL y
    durmiendo el `sleep-request`— pero no informa de nada. Retenerlo un segundo
    convierte ese hueco en el dato que el panel de `descarga.py` no da: cuánto
    pesó lo que acaba de bajar.
    """
    ahora = time.time() if ahora is None else ahora
    if not terminado or (ahora - terminado[2]) > retencion:
        return None
    return (terminado[0], terminado[1])

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
        self._previos = set()      # rutas .part del ciclo anterior
        self._terminado = None     # (nombre, tam, momento)

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
        """Un ciclo de actualización. Devuelve la lista de activos.

        Sin eventos del SO, "este archivo terminó" se deduce comparando con el
        ciclo anterior: un `.part` que desapareció y existe ya sin sufijo se
        completó. Cuesta un `os.stat` por desaparición, sobre un `os.walk` que
        de todos modos se hizo.
        """
        activos = self.get_active_parts()
        actuales = {r for r, _, _ in activos}
        for ruta in self._previos - actuales:
            final = ruta[: -len(".part")]
            try:
                self._terminado = (
                    os.path.basename(final),
                    os.stat(final).st_size,
                    time.time(),
                )
            except OSError:
                pass  # se borró en vez de renombrarse: descarga abortada
        self._previos = actuales
        return activos

    def terminado(self, ahora=None):
        """`(nombre, tamaño)` del último archivo completado, si sigue vigente."""
        return _terminado_vigente(self._terminado, ahora)

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
        self._terminado = None  # (nombre, tam, momento) del ultimo completado
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
                destino = getattr(event, "dest_path", "") or ""
                if event.src_path.endswith(".part"):
                    self.parent._eliminar(event.src_path)
                    # .part -> nombre final: esa descarga terminó
                    if destino and not destino.endswith(".part"):
                        self.parent._completar(destino)
                # Si el destino es .part (raro, pero posible), registrar
                if destino.endswith(".part"):
                    self.parent._registrar(destino)

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

    def _completar(self, ruta_final):
        """Un `.part` se renombró: ese archivo terminó de bajar.

        Es la **única** señal fiable de "terminó". `tick()` no sirve para esto:
        un archivo chico puede vivir menos que un ciclo de refresco —medido en
        producción, un `.mp4` de 0.97 MB estuvo presente en 1 de 59 muestras de
        0.25s— y entonces jamás llega a aparecer en la lista de activos. Los
        eventos de creación y de renombrado sí son puntuales; los que Windows
        escatima son los de modificación (ver `tick`).
        """
        try:
            tam = os.stat(ruta_final).st_size
        except OSError:
            return
        with self._lock:
            self._terminado = (os.path.basename(ruta_final), tam, time.time())

    def terminado(self, ahora=None):
        """`(nombre, tamaño)` del último archivo completado, si sigue vigente."""
        with self._lock:
            t = self._terminado
        return _terminado_vigente(t, ahora)

    def tick(self):
        """Devuelve los .part activos, midiendo cada uno con `os.stat()`.

        El evento del watchdog sirve para **descubrir** el archivo; el tamaño y
        la vigencia salen de `os.stat()`. En Windows `ReadDirectoryChangesW`
        avisa cuando NTFS actualiza la entrada de directorio, no en cada
        escritura: medido, 4 MB escritos en 5s dan 1 `created` y **2**
        `modified`. Confiar en el evento dejaba un `.mp4` grande congelado en el
        tamaño de su `on_created` —0 B, y en pausa, porque sin cambio de tamaño
        nunca hay velocidad— y encima lo purgaba por vencido en plena descarga.

        Un `.part` sigue activo si **cualquiera** de tres señales es reciente:
        el último evento, el `mtime` del archivo, o que su tamaño haya crecido
        desde el tick anterior. Se suman en vez de sustituirse porque cada una
        falla en un escenario distinto: el `mtime` en rutas de red con
        metadatos cacheados (ver `test_watchdog_no_purga_por_mtime_obsoleto`),
        y el evento acá. El coste es un `os.stat()` por `.part` registrado, y
        con `"concurrent": 1` en el conf eso es uno solo por ciclo.
        """
        ahora = time.time()
        resultado = []
        with self._lock:
            for ruta in list(self._activos):
                visto, tam_previo = self._activos[ruta]
                try:
                    st = os.stat(ruta)
                except OSError:
                    del self._activos[ruta]  # desapareció entre eventos
                    continue

                creciendo = st.st_size != tam_previo
                escrito_recien = (ahora - st.st_mtime) <= VENTANA_ACTIVO
                if creciendo or escrito_recien:
                    visto = ahora

                if (ahora - visto) > VENTANA_ACTIVO:
                    del self._activos[ruta]
                    continue

                self._activos[ruta] = (visto, st.st_size)
                resultado.append((ruta, os.path.basename(ruta), st.st_size))

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
def intervalo_por_modo(modo_str, pedido=None):
    """Cada cuánto refrescar el panel, según el modo.

    En WATCHDOG el tick solo lee un dict en memoria: refrescar cuatro veces por
    segundo no cuesta nada, y hace visibles los archivos chicos —la mediana real
    ronda los 98 KB, así que un `.part` vive menos que un ciclo de 1s y el panel
    se lo perdía entre frame y frame. En POLLING cada tick es un `os.walk()`
    sobre todo el árbol de Rips, que puede estar en un NAS: ahí se queda en 1s.

    `pedido` (el `--intervalo` de la línea de comandos) manda si viene dado.
    """
    if pedido is not None:
        return pedido
    return 0.25 if modo_str == "WATCHDOG" else 1.0


def lineas_cabecera(rips_dir, modo_str, ancho=62):
    """Una sola fila (más un separador en blanco), recortada al ancho del panel.

    La cabecera anterior ocupaba 6 filas de las ~11 que mide el panel de
    Windows Terminal con `--size 0.35`: más de la mitad del alto gastada en
    texto que nunca cambia. Y si no cabe a lo ancho tampoco sirve: al envolver
    de línea metería una fila extra y desalinearía el panel entero, que es
    justo lo que `dibujar_panel()` existe para impedir. Por eso va soltando
    partes —primero el `Ctrl+C`, después la ruta— antes de recortar a lo bruto.
    """
    color_modo = GREEN if modo_str == "WATCHDOG" else YELLOW
    util = max(10, min(60, ancho - 2))

    for plana in (
        f" MONITOR · {modo_str} · {rips_dir} · Ctrl+C ",
        f" MONITOR · {modo_str} · {rips_dir} ",
        f" MONITOR · {modo_str} ",
    ):
        if len(plana) <= util:
            break
    else:
        plana = plana[:util]

    barras = util - len(plana)
    izq, der = barras // 2, barras - barras // 2
    etiqueta = plana.replace(modo_str, f"{color_modo}{modo_str}{RESET}{CYAN}{BOLD}", 1)
    return [f"{CYAN}{BOLD}  {'═' * izq}{etiqueta}{'═' * der}{RESET}", ""]


def acortar_nombre(nombre, maxlen):
    """Recorta por el principio: la cola lleva la extensión y el identificador."""
    return nombre if len(nombre) <= maxlen else "…" + nombre[-(maxlen - 1) :]


def carpeta_de(activos, rips_dir, previa=None):
    """Carpeta del `.part` que se está bajando, relativa a Rips (`~/` es Rips).

    Se queda **pegada** a la anterior cuando no hay nada activo. Entre archivo y
    archivo hay huecos de menos de un segundo, y con refresco de 0.25s la línea
    parpadearía una vez por archivo; la carpeta solo cambia al saltar de hilo,
    así que pegada es una línea quieta. `activos` viene ordenado por mtime
    descendente, así que el primero es el que de verdad está bajando: si quedó
    un `.part` huérfano de otro hilo esperando a vencer, no se roba la fila.
    """
    if not activos:
        return previa
    carpeta = os.path.dirname(activos[0][0]).replace("\\", "/")
    raiz = rips_dir.replace("\\", "/").rstrip("/")
    if carpeta.lower().startswith(raiz.lower()):
        carpeta = carpeta[len(raiz) :]
    carpeta = carpeta.strip("/")
    return f"~/{carpeta}/" if carpeta else "~/"


def dibujar_panel(
    activos, historiales, spin_idx, rips_dir, modo_str, carpeta=None,
    terminado=None, alto=None, ancho=None,
):
    r"""Repinta el panel entero desde la fila 1, sin pasarse de su alto.

    Las tres reglas de acá son el arreglo de un bug real. El panel de Windows
    Terminal mide ~11 filas (`--size 0.35`) y con dos .part activos el dibujo
    necesitaba 13: el salto de línea de la última fila scrolleaba el buffer, así
    que la cabecera y lo ya dibujado subían una fila mientras el `goto()` del
    frame siguiente seguía apuntando a la misma fila ABSOLUTA. Cada frame se
    escribía debajo del anterior en vez de encima, y quedaban en pantalla .part
    ya terminados conviviendo con el cartel de que no había nada bajando.

      1. Se repinta también la cabecera: el frame no depende de que nada se
         haya movido, ni de cuántas líneas quedaron impresas antes del bucle.
      2. Se recorta a lo que cabe —de alto y de ancho— y no se emite el "\n"
         final, así el terminal nunca scrollea. Un nombre largo que envolviera
         de línea metería una fila extra y volvería a desalinear todo.
      3. Cierra con ED0 ("\033[J"), que borra de ahí al final de la pantalla:
         un frame más corto que el anterior no puede dejar restos, y ya no hace
         falta llevar la cuenta de las filas del frame previo.

    Una fila por archivo, en columnas fijas. `gallery-dl_win.conf` trae
    `"concurrent": 1`, así que en la práctica hay un solo `.part` a la vez: el
    recorte por cantidad casi nunca se usa, pero tiene que estar bien igual.
    """
    if alto is None:
        alto = shutil.get_terminal_size(fallback=(80, 24)).lines
    if ancho is None:
        ancho = shutil.get_terminal_size(fallback=(80, 24)).columns

    ahora = time.monotonic()
    libre = max(20, ancho - 30)  # lo que queda para el nombre tras las columnas
    filas = []

    for ruta, nombre, tamanio in activos:
        hist = historiales.get(ruta, deque())

        hist.append((ahora, tamanio))
        while hist and (ahora - hist[0][0]) > VENTANA_VEL:
            hist.popleft()

        vel_str = "—"
        color = YELLOW
        spin = "-"  # no crece: ni girando ni terminado
        if len(hist) >= 2:
            t0, s0 = hist[0]
            delta_t = ahora - t0
            delta_b = tamanio - s0
            if delta_t > 0 and delta_b > 0:
                vel_str = fmt_bytes(delta_b / delta_t) + "/s"
                color = GREEN
                spin = SPINNERS[spin_idx % len(SPINNERS)]

        filas.append(
            f"  {color}{spin}{RESET}  {BOLD}{fmt_bytes(tamanio):>9}{RESET}  "
            f"{color}{vel_str:>9}{RESET}   {YELLOW}{acortar_nombre(nombre, libre)}{RESET}"
        )

    if not filas and terminado:
        nom, tam = terminado
        filas.append(
            f"  {GREEN}+{RESET}  {BOLD}{fmt_bytes(tam):>9}{RESET}  {'':>9}   "
            f"{GRAY}{acortar_nombre(nom, libre)}{RESET}"
        )
    if not filas:
        filas.append(f"  {GRAY}esperando .part...{RESET}")

    fijas = lineas_cabecera(rips_dir, modo_str, ancho)
    if carpeta:
        fijas.append(f"  {CYAN}{carpeta}{RESET}")

    tope = max(1, alto - 1)  # la última fila queda libre: sin salto de línea final
    lineas = fijas + filas

    if len(lineas) > tope:
        caben = max(0, tope - len(fijas) - 1)  # una fila se va en el aviso
        lineas = fijas + filas[:caben]
        lineas.append(f"  {GRAY}… +{len(filas) - caben} archivo(s) más{RESET}")
        del lineas[tope:]  # si ni la cabecera cabe, se corta igual

    goto(1)
    sys.stdout.write("\n".join(f"{CLEAR}{linea}" for linea in lineas))
    sys.stdout.write("\033[J")
    sys.stdout.flush()
    return len(lineas)


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rips-dir", default=RIPS_DIR)
    parser.add_argument("--intervalo", default=None, type=float)
    args = parser.parse_args()

    CENTINELA = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "descarga.running"
    )

    rips_dir = args.rips_dir

    if IS_WINDOWS:
        os.system("")  # activar ANSI

    # Crear monitor (auto-detecta watchdog vs polling)
    monitor = crear_monitor(rips_dir)
    intervalo = intervalo_por_modo(monitor.modo_str, args.intervalo)

    # El banner de crear_monitor() se lee durante este segundo —que igual hay
    # que esperar a que Windows Terminal termine de abrir el panel— y después
    # desaparece. `cls` borra el buffer entero, scrollback incluido: el
    # `\033[2J` que había antes no alcanzaba, porque WT sube el contenido al
    # scrollback en vez de borrarlo y el banner quedaba sobre la cabecera.
    if IS_WINDOWS:
        time.sleep(1)
        os.system("cls")
    else:
        time.sleep(0.6)
        sys.stdout.write("\033[3J\033[2J\033[H")
    hide_cursor()
    sys.stdout.flush()

    spin_idx = 0
    carpeta = None  # se queda pegada a la última vista: ver carpeta_de()
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

            carpeta = carpeta_de(activos, rips_dir, carpeta)
            terminado = monitor.terminado()
            ultimas_filas = dibujar_panel(
                activos,
                monitor.historiales,
                spin_idx,
                rips_dir,
                monitor.modo_str,
                carpeta,
                terminado,
            )
            spin_idx += 1
            time.sleep(intervalo)

    except KeyboardInterrupt:
        pass
    finally:
        show_cursor()
        goto(ultimas_filas + 1)
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
