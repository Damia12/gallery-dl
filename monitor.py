#!/usr/bin/env python3
"""
monitor.py — Muestra en tiempo real todos los .part activos en G:/Rips
Uso: python monitor.py [--rips-dir G:/Rips] [--intervalo 1]
"""

import argparse
import os
import sys
import time
from collections import deque

IS_WINDOWS = sys.platform == "win32"
RIPS_DIR = r"G:\Rips" if IS_WINDOWS else os.path.expanduser("~/Rips")

VENTANA_ACTIVO = 5
VENTANA_VEL = 3

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
GRAY = "\033[90m"
RED = "\033[31m"
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


def get_active_parts(rips_dir):
    ahora = time.time()
    candidatos = []
    for root, _, files in os.walk(rips_dir):
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


def ruta_corta(ruta_completa, rips_dir, maxlen=60):
    rel = ruta_completa.replace(rips_dir, "").lstrip("\\/")
    return ("..." + rel[-(maxlen - 3) :]) if len(rel) > maxlen else rel


HEADER_LINES = 5


def dibujar_panel(activos, historiales, spin_idx, rips_dir, ultimas_filas):
    ahora = time.monotonic()
    lineas = []

    if not activos:
        lineas.append(f"  {GRAY}Sin descarga activa — esperando .part...{RESET}")
        lineas.append("")
    else:
        for ruta, nombre, tamanio in activos:
            rel = ruta_corta(ruta, rips_dir)
            hist = historiales[nombre]

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rips-dir", default=RIPS_DIR)
    parser.add_argument("--intervalo", default=1, type=float)
    args = parser.parse_args()

    CENTINELA = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "descarga.running"
    )
    if os.path.exists(CENTINELA):
        if time.time() - os.path.getmtime(CENTINELA) > 60:
            os.remove(CENTINELA)

    rips_dir = args.rips_dir
    intervalo = args.intervalo

    if IS_WINDOWS:
        os.system("")  # activar ANSI
        time.sleep(1)  # esperar que el panel de WT termine de abrirse
        os.system("cls")  # limpiar residuo visual

    hide_cursor()

    sys.stdout.write("\033[2J\033[H")
    print(f"{CYAN}{BOLD}  {'═' * 58}{RESET}")
    print(f"{CYAN}{BOLD}  MONITOR DE DESCARGA ACTIVA{RESET}")
    print(f"{GRAY}  Dir: {rips_dir}{RESET}")
    print(f"{GRAY}  Ctrl+C para salir{RESET}")
    print(f"{CYAN}{BOLD}  {'═' * 58}{RESET}")
    sys.stdout.flush()

    historiales = {}
    spin_idx = 0
    ultimas_filas = 0

    try:
        while True:
            if not os.path.exists(CENTINELA):
                break

            activos = get_active_parts(rips_dir)

            nombres_activos = {n for _, n, _ in activos}
            for _, nombre, _ in activos:
                if nombre not in historiales:
                    historiales[nombre] = deque()

            for n in list(historiales):
                if n not in nombres_activos:
                    del historiales[n]

            ultimas_filas = dibujar_panel(
                activos, historiales, spin_idx, rips_dir, ultimas_filas
            )
            spin_idx += 1
            time.sleep(intervalo)

    except KeyboardInterrupt:
        pass
    finally:
        show_cursor()
        goto(HEADER_LINES + ultimas_filas + 2)
        print(f"{GRAY}  Descarga terminada. Cerrando monitor...{RESET}\n")
        time.sleep(1)


if __name__ == "__main__":
    main()
