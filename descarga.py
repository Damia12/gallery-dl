import os
import subprocess
import sys
import threading
import time
from datetime import datetime

GALLERY_DL = "gallery-dl.exe" if sys.platform == "win32" else "gallery-dl"

if sys.platform == "win32":
    CONFIG = r"C:\gallery-dl\gallery-dl_win.conf"
    LISTA = r"C:\gallery-dl\lista.txt"
    LOG_DIR = r"G:\Rips\logs"
else:
    CONFIG = os.path.expanduser("~/gallery-dl/gallery-dl_linux.conf")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")
    LOG_DIR = os.path.expanduser("~/Rips/logs")

SLEEP_ENTRE_HILOS = 30
TIMEOUT_ACTIVIDAD = 300  # segundos sin output → matar proceso

os.makedirs(LOG_DIR, exist_ok=True)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def clear_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def spinner_thread(estado):
    idx = 0
    while not estado["stop"]:
        elapsed = int(time.time() - estado["inicio"])
        mins = elapsed // 60
        segs = elapsed % 60
        tiempo = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"
        nuevo = estado["nuevo"]
        done = estado["done"]
        resumen = f"{nuevo}"
        if done > 0:
            resumen += f" | {done} done"
        sys.stdout.write(
            f"\r  {CYAN}{SPINNER[idx % len(SPINNER)]}{RESET} {DIM}{resumen} —  {tiempo}{RESET}"
        )
        sys.stdout.flush()
        idx += 1
        time.sleep(0.1)
    clear_line()


def watchdog_thread(proceso, estado, timeout):
    """Mata el proceso si lleva más de `timeout` segundos sin output."""
    while not estado["stop"]:
        tiempo_sin_output = time.time() - estado["ultimo_output"]
        if tiempo_sin_output > timeout:
            proceso.kill()
            estado["timeout"] = True
            break
        time.sleep(5)


reset_archive = "--reset" in sys.argv
if reset_archive:
    sys.argv.remove("--reset")

if len(sys.argv) > 1:
    urls = sys.argv[1:]
else:
    with open(LISTA, "r", encoding="utf-8") as f:
        urls = [u.strip() for u in f if u.strip() and not u.startswith("#")]

print(f"{BOLD}{'═' * 50}{RESET}")
print(f"{BOLD}  Iniciando descarga — {len(urls)} hilos{RESET}")
if reset_archive:
    print(f"  {YELLOW}Modo: ignorando archive{RESET}")
print(f"  {GRAY}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
print(f"{BOLD}{'═' * 50}{RESET}\n")

errores_totales = []
timeouts_totales = []

for i, url in enumerate(urls, 1):
    nombre = url.rstrip("/").split("/")[-1][:60]
    log_file = os.path.join(LOG_DIR, f"{nombre}.log")

    print(f"{BOLD}[{i}/{len(urls)}]{RESET} {CYAN}{nombre}{RESET}")
    print(f"  {GRAY}{url}{RESET}\n")

    errores_hilo = []
    archivos_nuevos = []
    contador_nuevo = 0
    contador_done = 0
    inicio = time.time()

    estado_spinner = {
        "stop": False,
        "inicio": inicio,
        "nuevo": 0,
        "done": 0,
    }

    estado_watchdog = {
        "stop": False,
        "ultimo_output": time.time(),
        "timeout": False,
    }

    cmd = [GALLERY_DL, "-c", CONFIG, url]
    if reset_archive:
        cmd.append("--no-download-archive")

    proceso = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    spin = threading.Thread(target=spinner_thread, args=(estado_spinner,), daemon=True)
    spin.start()

    watch = threading.Thread(
        target=watchdog_thread,
        args=(proceso, estado_watchdog, TIMEOUT_ACTIVIDAD),
        daemon=True,
    )
    watch.start()

    for linea in proceso.stdout:
        linea_strip = linea.strip()
        if not linea_strip:
            continue

        # Actualizar watchdog
        estado_watchdog["ultimo_output"] = time.time()

        clear_line()

        if linea_strip.startswith("#"):
            contador_done += 1
            estado_spinner["done"] = contador_done
            ruta = linea_strip[1:].strip()
            print(f"  {GRAY}[{contador_done:>3}] [DONE] {ruta}{RESET}")
        else:
            contador_nuevo += 1
            estado_spinner["nuevo"] = contador_nuevo
            archivos_nuevos.append(linea_strip)
            print(f"  {GREEN}[{contador_nuevo:>3}]{RESET} {linea_strip}")

    stderr = proceso.stderr.read()
    proceso.wait()

    estado_spinner["stop"] = True
    estado_watchdog["stop"] = True
    spin.join()
    watch.join(timeout=2)

    # ── Detectar timeout ──────────────────────────────────────────────────────
    if estado_watchdog["timeout"]:
        timeouts_totales.append(nombre)
        clear_line()
        print(
            f"\n  {RED}⏱ Timeout — sin actividad por {TIMEOUT_ACTIVIDAD}s — proceso terminado{RESET}"
        )
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\nURL: {url}\n")
            f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(f"TIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n")
        if i < len(urls):
            print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
            time.sleep(SLEEP_ENTRE_HILOS)
        print()
        continue

    for linea in stderr.splitlines():
        if any(
            k in linea.lower() for k in ["error", "warning", "failed", "unsupported"]
        ):
            if not any(x in linea for x in ["theme-light", "color-", "--rem", "None_"]):
                errores_hilo.append(linea)

    # ── Log ───────────────────────────────────────────────────────────────────
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\nURL: {url}\n")
        f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'=' * 60}\n\n")
        if archivos_nuevos:
            f.write("\n".join(archivos_nuevos))
            f.write("\n\n")
        if errores_hilo:
            f.write("ERRORES:\n")
            f.write("\n".join(errores_hilo))
        else:
            f.write("Sin errores.")
        f.write("\n")

    # ── Resumen del hilo ──────────────────────────────────────────────────────
    duracion = int(time.time() - inicio)
    mins = duracion // 60
    segs = duracion % 60
    tiempo = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"

    resumen = f"{contador_nuevo} nuevos"
    if contador_done > 0:
        resumen += f" | {contador_done} ya descargados"

    print()
    if errores_hilo:
        errores_totales.append((nombre, len(errores_hilo)))
        print(
            f"  {YELLOW}⚠ {nombre} — {resumen} — {len(errores_hilo)} errores (ver log) — {tiempo}{RESET}"
        )
    elif contador_nuevo > 0:
        print(f"  {GREEN}✓ {nombre} — {resumen} — {tiempo}{RESET}")
    elif contador_done > 0:
        print(
            f"  {GRAY}✓ {nombre} — todo ya descargado ({contador_done} archivos){RESET}"
        )
    else:
        print(f"  {GRAY}✓ {nombre} — sin archivos nuevos{RESET}")

    if i < len(urls):
        print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
        time.sleep(SLEEP_ENTRE_HILOS)

    print()

# ── Resumen final ─────────────────────────────────────────────────────────────
print(f"{BOLD}{'═' * 50}{RESET}")
print(
    f"{BOLD}  Descarga terminada — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}"
)
if errores_totales:
    print(f"\n  {YELLOW}Hilos con errores:{RESET}")
    for nombre, count in errores_totales:
        print(f"    {nombre}: {count} errores")
if timeouts_totales:
    print(f"\n  {RED}Hilos con timeout:{RESET}")
    for nombre in timeouts_totales:
        print(f"    {nombre}")
if not errores_totales and not timeouts_totales:
    print(f"  {GREEN}Todos los hilos sin errores.{RESET}")
print(f"{BOLD}{'═' * 50}{RESET}")

if sys.platform == "win32":
    input("\nPresiona Enter para cerrar...")
