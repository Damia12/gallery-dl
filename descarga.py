import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

# ── Detección de OS e Imports Condicionales ─────────────────────────────
IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import errno
    import pty
    import select

# ── Configuración según OS ──────────────────────────────────────────────
if IS_WINDOWS:
    GALLERY_DL = "gallery-dl.exe"
    CONFIG = r"C:\gallery-dl\gallery-dl_win.conf"
    LISTA = r"C:\gallery-dl\lista.txt"
    LOG_DIR = r"G:\Rips\logs"
else:
    GALLERY_DL = "gallery-dl"
    CONFIG = os.path.expanduser("~/gallery-dl/gallery-dl_linux.conf")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")
    LOG_DIR = os.path.expanduser("~/Rips/logs")

SLEEP_ENTRE_HILOS = 30
TIMEOUT_ACTIVIDAD = 300

os.makedirs(LOG_DIR, exist_ok=True)

# ── Constantes ANSI ─────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def clear_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


# ==============================================================================
# LÓGICA WINDOWS
# ==============================================================================
def spinner_thread(estado):
    idx = 0
    while not estado["stop"]:
        elapsed = int(time.time() - estado["inicio"])
        mins = elapsed // 60
        segs = elapsed % 60
        tiempo = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"
        resumen = f"{estado['nuevo']}"
        if estado["done"] > 0:
            resumen += f" | {estado['done']} done"
        sys.stdout.write(
            f"\r  {CYAN}{SPINNER[idx % len(SPINNER)]}{RESET} {DIM}{resumen} —  {tiempo}{RESET}"
        )
        sys.stdout.flush()
        idx += 1
        time.sleep(0.1)
    clear_line()


def watchdog_thread(proceso, estado, timeout):
    while not estado["stop"]:
        if time.time() - estado["ultimo_output"] > timeout:
            proceso.kill()
            estado["timeout"] = True
            break
        time.sleep(5)


def descargar_windows(url, reset_archive):
    cmd = [GALLERY_DL, "-c", CONFIG, url]
    if reset_archive:
        cmd.append("--no-download-archive")

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []

    estado_spinner = {"stop": False, "inicio": inicio, "nuevo": 0, "done": 0}
    estado_watchdog = {"stop": False, "ultimo_output": time.time(), "timeout": False}

    proceso = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    spin = threading.Thread(target=spinner_thread, args=(estado_spinner,), daemon=True)
    watch = threading.Thread(
        target=watchdog_thread,
        args=(proceso, estado_watchdog, TIMEOUT_ACTIVIDAD),
        daemon=True,
    )
    spin.start()
    watch.start()

    for linea in proceso.stdout:
        linea_strip = linea.strip()
        if not linea_strip:
            continue

        estado_watchdog["ultimo_output"] = time.time()
        clear_line()

        if linea_strip.startswith("#"):
            estado_spinner["done"] += 1
            ruta = linea_strip[1:].strip()
            print(f"  {GRAY}[{estado_spinner['done']:>3}] [DONE] {ruta}{RESET}")
        else:
            estado_spinner["nuevo"] += 1
            archivos_nuevos.append(linea_strip)
            print(f"  {GREEN}[{estado_spinner['nuevo']:>3}]{RESET} {linea_strip}")

    stderr = proceso.stderr.read()
    proceso.wait()

    estado_spinner["stop"] = True
    estado_watchdog["stop"] = True
    spin.join()
    watch.join(timeout=2)

    for linea in stderr.splitlines():
        if any(
            k in linea.lower() for k in ["error", "warning", "failed", "unsupported"]
        ):
            if not any(x in linea for x in ["theme-light", "color-", "--rem", "None_"]):
                errores_hilo.append(linea)

    return (
        archivos_nuevos,
        errores_hilo,
        estado_spinner["nuevo"],
        estado_spinner["done"],
        estado_watchdog["timeout"],
        int(time.time() - inicio),
    )


# ==============================================================================
# LÓGICA LINUX / WSL
# ==============================================================================
def descargar_linux(url, reset_archive):
    cmd = [GALLERY_DL, "-c", CONFIG, url]
    if reset_archive:
        cmd.append("--no-download-archive")

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    contador_nuevo = 0
    contador_done = 0
    ultima_ruta = ""
    timeout_ocurrido = False

    env_vars = os.environ.copy()
    env_vars["PYTHONUNBUFFERED"] = "1"

    master_fd, slave_fd = pty.openpty()
    proceso = subprocess.Popen(
        cmd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env_vars
    )
    os.close(slave_fd)

    buffer = ""
    ultimo_output = time.time()
    spinner_idx = 0

    while True:
        r, _, _ = select.select([master_fd], [], [], 1.0)

        if not r:
            if time.time() - ultimo_output > TIMEOUT_ACTIVIDAD:
                proceso.kill()
                timeout_ocurrido = True
                break
            continue

        try:
            chunk = os.read(master_fd, 4096).decode("utf-8", "replace")
        except OSError as e:
            if e.errno == errno.EIO:
                break
            raise

        if not chunk:
            break

        ultimo_output = time.time()

        buffer += chunk
        buffer = buffer.replace("\r\n", "\n")

        while True:
            idx_n = buffer.find("\n")
            idx_r = buffer.find("\r")

            if idx_n == -1 and idx_r == -1:
                break

            es_linea_completa = False
            linea = ""

            if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
                linea = buffer[:idx_n]
                buffer = buffer[idx_n + 1 :]
                es_linea_completa = True

            elif idx_r != -1:
                texto_antes_del_r = buffer[:idx_r]

                if texto_antes_del_r.strip() and not re.search(
                    r"\d+%", texto_antes_del_r
                ):
                    linea = texto_antes_del_r
                    buffer = buffer[idx_r + 1 :]
                    es_linea_completa = True
                else:
                    if idx_r == len(buffer) - 1:
                        break

                    buffer = buffer[idx_r + 1 :]
                    prog_limpio = texto_antes_del_r.strip()
                    if prog_limpio and "%" in prog_limpio:
                        prog_pura = ANSI_ESCAPE.sub("", prog_limpio).strip()
                        anim = SPINNER[spinner_idx % len(SPINNER)]
                        spinner_idx += 1
                        sys.stdout.write(f"\r  {CYAN}{anim}{RESET} {prog_pura}\033[K")
                        sys.stdout.flush()
                    continue

            if es_linea_completa:
                linea_limpia = linea.strip()
                if not linea_limpia:
                    continue

                if "%" in linea_limpia and any(
                    x in linea_limpia for x in ["MB", "KB", "B/s"]
                ):
                    prog_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()
                    anim = SPINNER[spinner_idx % len(SPINNER)]
                    spinner_idx += 1
                    sys.stdout.write(f"\r  {CYAN}{anim}{RESET} {prog_pura}\033[K")
                    sys.stdout.flush()
                    continue

                es_done = "\x1b[2m" in linea_limpia
                es_error = any(k in linea_limpia.lower() for k in ["error", "warning"])

                linea_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()

                if not linea_pura:
                    continue
                if linea_pura == ultima_ruta:
                    continue
                ultima_ruta = linea_pura

                partes = linea_pura.split(" ", 1)
                nombre_visible = partes[1] if len(partes) > 1 else linea_pura

                if es_done:
                    contador_done += 1
                    sys.stdout.write(
                        f"\r\033[2K  {DIM}[{contador_done:>3}] [DONE] {nombre_visible}{RESET}\n"
                    )
                elif es_error:
                    errores_hilo.append(linea_pura)
                    sys.stdout.write(f"\r\033[2K  {YELLOW}⚠ {linea_pura}{RESET}\n")
                else:
                    contador_nuevo += 1
                    archivos_nuevos.append(linea_pura)
                    # Eliminada la etiqueta [N] aquí, se mantiene el color verde
                    sys.stdout.write(
                        f"\r\033[2K  {GREEN}[{contador_nuevo:>3}] {nombre_visible}{RESET}\n"
                    )

                sys.stdout.flush()

    proceso.wait()
    clear_line()
    return (
        archivos_nuevos,
        errores_hilo,
        contador_nuevo,
        contador_done,
        timeout_ocurrido,
        int(time.time() - inicio),
    )


# ==============================================================================
# BUCLE PRINCIPAL
# ==============================================================================
if __name__ == "__main__":
    reset_archive = "--reset" in sys.argv
    if reset_archive:
        sys.argv.remove("--reset")

    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        with open(LISTA, "r", encoding="utf-8") as f:
            urls = [u.strip() for u in f if u.strip() and not u.startswith("#")]

    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"{BOLD}  Iniciando descarga — {len(urls)} hilos ({'Windows' if IS_WINDOWS else 'Linux/WSL'}){RESET}"
    )
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

        if IS_WINDOWS:
            archivos, errs, nuevos, done, timeout, duracion = descargar_windows(
                url, reset_archive
            )
        else:
            archivos, errs, nuevos, done, timeout, duracion = descargar_linux(
                url, reset_archive
            )

        if timeout:
            timeouts_totales.append(nombre)
            print(
                f"\n  {RED}⏱ Timeout — sin actividad por {TIMEOUT_ACTIVIDAD}s — proceso terminado{RESET}"
            )
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(
                    f"\nURL: {url}\nFecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 60}\n\nTIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n"
                )
            if i < len(urls):
                print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
                time.sleep(SLEEP_ENTRE_HILOS)
            print()
            continue

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\nURL: {url}\n")
            f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'=' * 60}\n\n")
            if archivos:
                f.write("\n".join(archivos))
                f.write("\n\n")
            if errs:
                f.write("ERRORES:\n" + "\n".join(errs))
            else:
                f.write("Sin errores.")
            f.write("\n")

        mins, segs = duracion // 60, duracion % 60
        tiempo_str = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"

        resumen = f"{nuevos} nuevos"
        if done > 0:
            resumen += f" | {done} ya descargados"

        print()
        if errs:
            errores_totales.append((nombre, len(errs)))
            print(
                f"  {YELLOW}⚠ {nombre} — {resumen} — {len(errs)} errores (ver log) — {tiempo_str}{RESET}"
            )
        elif nuevos > 0:
            print(f"  {GREEN}✓ {nombre} — {resumen} — {tiempo_str}{RESET}")
        elif done > 0:
            print(f"  {GRAY}✓ {nombre} — todo ya descargado ({done} archivos){RESET}")
        else:
            print(f"  {GRAY}✓ {nombre} — sin archivos nuevos{RESET}")

        if i < len(urls):
            print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
            time.sleep(SLEEP_ENTRE_HILOS)
        print()

    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"{BOLD}  Descarga terminada — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}"
    )
    if errores_totales:
        print(f"\n  {YELLOW}Hilos con errores:{RESET}")
        for n, c in errores_totales:
            print(f"    {n}: {c} errores")
    if timeouts_totales:
        print(f"\n  {RED}Hilos con timeout:{RESET}")
        for n in timeouts_totales:
            print(f"    {n}")
    if not errores_totales and not timeouts_totales:
        print(f"  {GREEN}Todos los hilos sin errores.{RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}")

    if IS_WINDOWS:
        input("\nPresiona Enter para cerrar...")
