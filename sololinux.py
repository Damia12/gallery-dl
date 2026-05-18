#!/usr/bin/env python3
"""
descarga_linux.py — wrapper de gallery-dl para Linux
Usa PTY para capturar el progreso real (velocidad, nombre de archivo)
que gallery-dl escribe en stderr con \r.
"""

import os
import pty
import re
import select
import signal
import sys
import threading
import time
from datetime import datetime

# ── Rutas ────────────────────────────────────────────────────────────────────
CONFIG = os.path.expanduser("~/gallery-dl/gallery-dl_linux.conf")
LISTA = os.path.expanduser("~/gallery-dl/lista.txt")
LOG_DIR = os.path.expanduser("~/Rips/logs")

GALLERY_DL = "gallery-dl"
SLEEP_ENTRE_URLS = 30  # segundos de pausa entre URLs
TIMEOUT_ACTIVIDAD = 300  # segundos sin output → matar proceso

os.makedirs(LOG_DIR, exist_ok=True)

# ── Colores ANSI ──────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

DEBUG_PTY = False


# ── Helpers de terminal ───────────────────────────────────────────────────────
def clear_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def es_linea_progreso(linea):
    """Detecta líneas de velocidad: '1.23 MB/s', '500 kB/s', etc."""
    return bool(re.search(r"[\d.]+\s*[KkMmGgTt]?[Bb]/s", linea))


def es_linea_error(linea):
    palabras = ["error", "warning", "failed", "unsupported", "exception"]
    ruido = ["theme-light", "color-", "--rem", "None_"]
    l = linea.lower()
    return any(p in l for p in palabras) and not any(r in linea for r in ruido)


def formatear_tiempo(segundos):
    m = int(segundos) // 60
    s = int(segundos) % 60
    return f"{m}m {s}s" if m else f"{s}s"


# ── Nombre visible del archivo ────────────────────────────────────────────────
def nombre_visible(ruta):
    """Devuelve solo la parte del nombre después del primer espacio.
    'AlphaUnicor…gtwwYr Putipobre de enormes ubres 01.mp4'
    → 'Putipobre de enormes ubres 01.mp4'
    Si no hay espacio, devuelve el nombre completo.
    """
    base = os.path.basename(ruta)
    if " " in base:
        return base.split(" ", 1)[1]
    return base


# ── Parser de línea de progreso ───────────────────────────────────────────────
_spinner_idx = 0
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def formatear_progreso(linea_raw):
    """
    Parsea '88%  14.09MB 586.09KB/s' y devuelve línea formateada:
    '⠸ 88%  10.2MB / 11.6MB  586KB/s  —  ETA 3s'
    """
    global _spinner_idx
    spinner = _SPINNER[_spinner_idx % len(_SPINNER)]
    _spinner_idx += 1

    # Extraer porcentaje, descargado y velocidad
    m = re.match(
        r"(\d+)%\s+([\d.]+)\s*([KkMmGgTt]?[Bb])\s+([\d.]+)\s*([KkMmGgTt]?[Bb]/s)",
        linea_raw.strip(),
    )
    if not m:
        return f"  {DIM}{spinner} {linea_raw.strip()}{RESET}"

    pct = int(m.group(1))
    desc_val = float(m.group(2))
    desc_unit = m.group(3)
    vel_val = float(m.group(4))
    vel_unit = m.group(5)

    # Calcular total estimado a partir del porcentaje
    if pct > 0:
        total_val = desc_val * 100 / pct
        total_str = f"{total_val:.1f}{desc_unit}"
    else:
        total_str = "?"

    desc_str = f"{desc_val}{desc_unit}"
    vel_str = f"{vel_val:.0f}{vel_unit}"

    # ETA en segundos
    if pct > 0 and vel_val > 0:
        # convertir descargado y total a bytes para calcular restante
        unidades = {
            "B": 1,
            "KB": 1024,
            "KiB": 1024,
            "MB": 1024**2,
            "MiB": 1024**2,
            "GB": 1024**3,
            "GiB": 1024**3,
        }
        factor_d = next(
            (v for k, v in unidades.items() if k.upper() == desc_unit.upper()), 1
        )
        factor_v = next(
            (
                v
                for k, v in unidades.items()
                if k.upper() == vel_unit.upper().replace("/S", "")
            ),
            1,
        )
        restante_bytes = (total_val - desc_val) * factor_d
        vel_bytes = vel_val * factor_v
        eta_s = int(restante_bytes / vel_bytes) if vel_bytes > 0 else 0
        eta_str = f"ETA {formatear_tiempo(eta_s)}"
    else:
        eta_str = ""

    partes = f"{pct}%  {desc_str} / {total_str}  {vel_str}"
    if eta_str:
        partes += f"  —  {eta_str}"

    return f"  {CYAN}{spinner}{RESET} {DIM}{partes}{RESET}"


def leer_pty(fd, estado, errores, archivos_nuevos):
    buf = ""
    archivo_actual = ""
    ultima_ruta = ""  # para deduplicar rutas duplicadas por stdout+stderr
    dbg = open("/tmp/pty_debug.txt", "wb") if DEBUG_PTY else None

    try:
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except (ValueError, OSError):
                break

            if not r:
                continue

            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break

            if not chunk:
                break

            if dbg:
                dbg.write(chunk)
                dbg.flush()

            estado["ultimo_output"] = time.time()
            buf += chunk.decode("utf-8", errors="replace")
            buf = buf.replace("\r\n", "\n")  # normalizar antes de parsear

            while True:
                cr = buf.find("\r")
                nl = buf.find("\n")

                if cr == -1 and nl == -1:
                    break

                if cr != -1 and (nl == -1 or cr < nl):
                    linea = buf[:cr].strip()
                    buf = buf[cr + 1 :]
                else:
                    linea = buf[:nl].strip()
                    buf = buf[nl + 1 :]

                if not linea:
                    continue

                linea_limpia = re.sub(r"\033\[[0-9;]*[A-Za-z]", "", linea).strip()
                if not linea_limpia:
                    continue

                if es_linea_progreso(linea_limpia):
                    if archivo_actual:
                        sys.stdout.write(f"\r\033[2K{formatear_progreso(linea_limpia)}")
                        sys.stdout.flush()

                elif es_linea_error(linea_limpia):
                    sys.stdout.write("\r\033[2K")
                    print(f"  {YELLOW}⚠  {linea_limpia}{RESET}")
                    errores.append(linea_limpia)

                elif "/" in linea_limpia or "\\" in linea_limpia:
                    es_dim = linea.startswith("\x1b[2m") or linea.startswith("\033[2m")
                    if linea_limpia == ultima_ruta:
                        continue
                    ultima_ruta = linea_limpia
                    if es_dim:
                        # Ya descargado — gris
                        estado["done"] += 1
                        if archivo_actual:
                            sys.stdout.write("\r\033[2K")
                        archivo_actual = ""
                        print(
                            f"  {GRAY}[{estado['done']:>3}] {nombre_visible(linea_limpia)}{RESET}"
                        )
                    else:
                        # Nuevo — verde
                        estado["nuevos"] += 1
                        archivos_nuevos.append(linea_limpia)
                        if archivo_actual:
                            sys.stdout.write("\r\033[2K")
                        archivo_actual = linea_limpia
                        print(
                            f"  {GREEN}[{estado['nuevos']:>3}]{RESET} {nombre_visible(linea_limpia)}"
                        )

                else:
                    pass  # ignorar otras líneas informativas
    finally:
        if dbg:
            dbg.close()


# ── Watchdog ──────────────────────────────────────────────────────────────────
def watchdog(pid, estado, timeout):
    while not estado["parar"]:
        if time.time() - estado["ultimo_output"] > timeout:
            estado["timeout"] = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(5)


# ── Procesar una URL ──────────────────────────────────────────────────────────
def procesar_url(url, idx, total, reset_archive):
    nombre = url.rstrip("/").split("/")[-1][:60]
    log_file = os.path.join(LOG_DIR, f"{nombre}.log")

    print(f"{BOLD}[{idx}/{total}]{RESET} {CYAN}{nombre}{RESET}")
    print(f"  {GRAY}{url}{RESET}\n")

    errores = []
    archivos_nuevos = []
    inicio = time.time()

    estado = {
        "inicio": inicio,
        "ultimo_output": time.time(),
        "nuevos": 0,
        "done": 0,
        "timeout": False,
        "parar": False,
    }

    cmd = [GALLERY_DL, "-c", CONFIG, url]
    if reset_archive:
        cmd.append("--no-download-archive")

    # Conectar stdout Y stderr al mismo PTY slave
    # gallery-dl verifica isatty() — si no es TTY real, suprime el progreso
    master_fd, slave_fd = pty.openpty()

    import subprocess

    proceso = subprocess.Popen(
        cmd,
        stdout=slave_fd,  # stdout → PTY slave
        stderr=slave_fd,  # stderr → PTY slave (mismo fd)
        text=False,
        close_fds=True,
    )
    os.close(slave_fd)  # el padre solo necesita master_fd

    # Hilo watchdog
    watch = threading.Thread(
        target=watchdog,
        args=(proceso.pid, estado, TIMEOUT_ACTIVIDAD),
        daemon=True,
    )
    watch.start()

    # Hilo lector de PTY (stderr)
    pty_hilo = threading.Thread(
        target=leer_pty,
        args=(master_fd, estado, errores, archivos_nuevos),
        daemon=True,
    )
    pty_hilo.start()

    # Toda la lectura (stdout + stderr) la hace leer_pty desde el master_fd
    proceso.wait()
    estado["parar"] = True

    try:
        os.close(master_fd)
    except OSError:
        pass

    pty_hilo.join(timeout=3)
    watch.join(timeout=2)

    sys.stdout.write("\r\033[2K")  # limpiar última línea de progreso
    sys.stdout.flush()

    # ── Timeout ───────────────────────────────────────────────────────────────
    if estado["timeout"]:
        print(
            f"  {RED}⏱  Timeout — {TIMEOUT_ACTIVIDAD}s sin actividad — proceso terminado{RESET}"
        )
        with open(log_file, "a") as f:
            f.write(f"\nURL: {url}\n")
            f.write(f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("=" * 60 + "\n")
            f.write(f"TIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n")
        return "timeout"

    # ── Log ───────────────────────────────────────────────────────────────────
    with open(log_file, "a") as f:
        f.write(f"\nURL: {url}\n")
        f.write(f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 60 + "\n\n")
        if archivos_nuevos:
            f.write("\n".join(archivos_nuevos) + "\n\n")
        f.write("ERRORES:\n" + "\n".join(errores) if errores else "Sin errores.")
        f.write("\n")

    # ── Resumen del hilo ──────────────────────────────────────────────────────
    duracion = formatear_tiempo(time.time() - inicio)
    nuevos = estado["nuevos"]
    done = estado["done"]
    resumen = f"{nuevos} nuevos" + (f" | {done} ya descargados" if done else "")

    if errores:
        print(
            f"  {YELLOW}⚠  {nombre} — {resumen} — {len(errores)} errores (ver log) — {duracion}{RESET}"
        )
        return "error"
    elif nuevos:
        print(f"  {GREEN}✓  {nombre} — {resumen} — {duracion}{RESET}")
    elif done:
        print(f"  {GRAY}✓  {nombre} — todo ya descargado ({done} archivos){RESET}")
    else:
        print(f"  {GRAY}✓  {nombre} — sin archivos nuevos{RESET}")

    return "ok"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    reset_archive = "--reset" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--reset"]

    if argv:
        urls = argv
    else:
        with open(LISTA, encoding="utf-8") as f:
            urls = [u.strip() for u in f if u.strip() and not u.startswith("#")]

    print(f"{BOLD}{'═' * 50}{RESET}")
    print(f"{BOLD}  Iniciando — {len(urls)} URL{'s' if len(urls) != 1 else ''}{RESET}")
    if reset_archive:
        print(f"  {YELLOW}Modo: ignorando archive{RESET}")
    print(f"  {GRAY}{datetime.now():%Y-%m-%d %H:%M:%S}{RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}\n")

    errores_totales = []
    timeouts_totales = []

    for i, url in enumerate(urls, 1):
        resultado = procesar_url(url, i, len(urls), reset_archive)

        if resultado == "timeout":
            timeouts_totales.append(url.rstrip("/").split("/")[-1][:60])
        elif resultado == "error":
            errores_totales.append(url.rstrip("/").split("/")[-1][:60])

        if i < len(urls):
            print(f"\n  {DIM}Esperando {SLEEP_ENTRE_URLS}s...{RESET}\n")
            time.sleep(SLEEP_ENTRE_URLS)

        print()

    # ── Resumen final ─────────────────────────────────────────────────────────
    print(f"{BOLD}{'═' * 50}{RESET}")
    print(f"{BOLD}  Terminado — {datetime.now():%Y-%m-%d %H:%M:%S}{RESET}")

    if errores_totales:
        print(f"\n  {YELLOW}Con errores:{RESET}")
        for n in errores_totales:
            print(f"    {n}")

    if timeouts_totales:
        print(f"\n  {RED}Con timeout:{RESET}")
        for n in timeouts_totales:
            print(f"    {n}")

    if not errores_totales and not timeouts_totales:
        print(f"  {GREEN}Todos los hilos sin errores.{RESET}")

    print(f"{BOLD}{'═' * 50}{RESET}")


if __name__ == "__main__":
    main()
