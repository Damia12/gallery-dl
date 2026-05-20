#!/usr/bin/env python3

import errno
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select

if IS_WINDOWS:
    GALLERY_DL = "gallery-dl.exe"
    CONFIG = r"C:\gallery-dl\gallery-dl_win.conf"
    LISTA = r"C:\gallery-dl\lista.txt"
    LOG_DIR = r"G:\Rips\logs"
    RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
    BACKUP_FILE = r"C:\gallery-dl\lista_retry_backup.txt"
else:
    GALLERY_DL = "gallery-dl"
    CONFIG = os.path.expanduser("~/gallery-dl/gallery-dl_linux.conf")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")
    LOG_DIR = os.path.expanduser("~/Rips/logs")
    RETRY_FILE = os.path.expanduser("~/gallery-dl/lista_retry.txt")
    BACKUP_FILE = os.path.expanduser("~/gallery-dl/lista_retry_backup.txt")

SLEEP_ENTRE_HILOS = 30
TIMEOUT_ACTIVIDAD = 300
TIMEOUT_SIN_ARCHIVOS = 600
MAX_REINTENTOS = 2

os.makedirs(LOG_DIR, exist_ok=True)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"
WHITE = "\033[37m"

ACCENT = WHITE

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
spinner_idx = 0

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

RE_PROGRESS = re.compile(
    r"(\d+)%\s+([\d.]+)\s*([KkMmGgTt]?[Bb])\s+([\d.]+)\s*([KkMmGgTt]?[Bb]/s)",
    re.IGNORECASE,
)


def clear_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def formatear_tiempo(segundos):
    m = int(segundos) // 60
    s = int(segundos) % 60
    return f"{m}m {s}s" if m else f"{s}s"


def nombre_visible(ruta):
    base = os.path.basename(ruta)
    if " " in base:
        return base.split(" ", 1)[1]
    return base


def render_progress_linux(linea_pura):
    global spinner_idx
    m = RE_PROGRESS.match(linea_pura.strip())
    if not m:
        return

    anim = SPINNER[spinner_idx % len(SPINNER)]
    spinner_idx += 1

    pct = int(m.group(1))
    downloaded = float(m.group(2))
    download_unit = m.group(3).upper()
    speed = float(m.group(4))
    speed_unit = m.group(5).upper()

    ancho_barra = 30
    llenos = int(ancho_barra * pct / 100)
    vacios = ancho_barra - llenos

    barra_interna = ("━" * llenos) + (" " * vacios)
    barra_visual = f"{DIM}│{RESET}{ACCENT}{barra_interna}{RESET}{DIM}│{RESET}"

    sys.stdout.write(
        f"\r  {ACCENT}{anim}{RESET} {barra_visual} {pct:>3}% │ {downloaded:.2f}{download_unit} │ {speed:.0f}{speed_unit} │\033[K{RESET}"
    )
    sys.stdout.flush()


def spinner_thread_windows(estado, lock_print):
    idx = 0
    ancho_barra = 20
    while not estado["stop"]:
        elapsed = int(time.time() - estado["inicio"])
        tiempo = formatear_tiempo(elapsed)

        resumen = f"{estado['nuevo']} nuevos"
        if estado["done"] > 0:
            resumen += f" | {estado['done']} ya descargados"

        pos = idx % (ancho_barra + 4)
        barra_lista = ["·"] * ancho_barra
        for i in range(4):
            p = pos - i
            if 0 <= p < ancho_barra:
                barra_lista[p] = "━"
        barra_interna = "".join(barra_lista)

        barra_visual = f"{DIM}│{RESET}{ACCENT}{barra_interna}{RESET}{DIM}│{RESET}"

        with lock_print:
            sys.stdout.write(
                f"\r  {ACCENT}{SPINNER[idx % len(SPINNER)]}{RESET} {barra_visual} {DIM}{resumen} — {tiempo}{RESET}\033[K"
            )
            sys.stdout.flush()

        idx += 1
        time.sleep(0.1)
    clear_line()


def watchdog_thread(proceso, estado, timeout):
    while not estado["stop"]:
        ahora = time.time()
        if ahora - estado["ultimo_output"] > timeout:
            proceso.kill()
            estado["timeout"] = True
            break
        if ahora - estado["ultimo_archivo"] > TIMEOUT_SIN_ARCHIVOS:
            proceso.kill()
            estado["timeout"] = True
            break
        time.sleep(5)


KEYWORDS_ERROR = ["error", "warning", "failed", "unsupported", "unable", "exception"]
KEYWORDS_RUIDO = ["theme-light", "color-", "--rem", "None_"]


def es_linea_error(linea):
    linea_lower = linea.lower()
    return any(k in linea_lower for k in KEYWORDS_ERROR) and not any(
        x in linea for x in KEYWORDS_RUIDO
    )


def limpiar_error(linea):
    return re.sub(r"^\[gallery-dl\]\s*", "", linea).strip()


def descargar_windows(url):
    cmd = [GALLERY_DL, "-c", CONFIG, url]

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []

    lock_print = threading.Lock()
    contador = {"seq": 0}

    estado_spinner = {"stop": False, "inicio": inicio, "nuevo": 0, "done": 0}
    estado_watchdog = {
        "stop": False,
        "ultimo_output": time.time(),
        "ultimo_archivo": time.time(),
        "timeout": False,
    }

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    proceso = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def leer_stderr():
        for linea in proceso.stderr:
            linea_strip = ANSI_ESCAPE.sub("", linea).strip()
            if not linea_strip or not es_linea_error(linea_strip):
                continue
            errores_hilo.append(linea_strip)
            with lock_print:
                estado_watchdog["ultimo_output"] = time.time()
                contador["seq"] += 1
                clear_line()
                sys.stdout.write(
                    f"  {RED}[{contador['seq']:>3}] [X] {limpiar_error(linea_strip)}{RESET}\n"
                )
                sys.stdout.flush()

    spin = threading.Thread(
        target=spinner_thread_windows, args=(estado_spinner, lock_print), daemon=True
    )
    watch = threading.Thread(
        target=watchdog_thread,
        args=(proceso, estado_watchdog, TIMEOUT_ACTIVIDAD),
        daemon=True,
    )
    stderr_thread = threading.Thread(target=leer_stderr, daemon=True)

    spin.start()
    watch.start()
    stderr_thread.start()

    try:
        for linea in proceso.stdout:
            linea_strip = linea.strip()
            if not linea_strip:
                continue

            with lock_print:
                estado_watchdog["ultimo_output"] = time.time()
                clear_line()

                if linea_strip.startswith("#"):
                    estado_spinner["done"] += 1
                    estado_watchdog["ultimo_archivo"] = time.time()
                    contador["seq"] += 1
                    ruta = linea_strip[1:].strip()
                    print(
                        f"  {GRAY}[{contador['seq']:>3}] [DONE] {nombre_visible(ruta)}{RESET}"
                    )
                else:
                    estado_spinner["nuevo"] += 1
                    estado_watchdog["ultimo_archivo"] = time.time()
                    contador["seq"] += 1
                    archivos_nuevos.append(linea_strip)
                    print(
                        f"  {GREEN}[{contador['seq']:>3}]{RESET} {nombre_visible(linea_strip)}"
                    )
    finally:
        proceso.wait()
        stderr_thread.join(timeout=5)

        estado_spinner["stop"] = True
        estado_watchdog["stop"] = True
        spin.join()
        watch.join(timeout=2)

        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    return (
        archivos_nuevos,
        errores_hilo,
        estado_spinner["nuevo"],
        estado_spinner["done"],
        estado_watchdog["timeout"],
        int(time.time() - inicio),
    )


def descargar_linux(url):
    cmd = [GALLERY_DL, "-c", CONFIG, url]

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    contador_nuevo = 0
    contador_done = 0
    contador_seq = 0
    ultima_ruta = ""
    timeout_ocurrido = False
    ultimo_archivo = time.time()

    env_vars = os.environ.copy()
    env_vars["PYTHONUNBUFFERED"] = "1"

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    master_fd, slave_fd = pty.openpty()
    proceso = subprocess.Popen(
        cmd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env_vars
    )
    os.close(slave_fd)

    buffer = ""
    ultimo_output = time.time()

    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 1.0)

            if not r:
                ahora = time.time()
                if ahora - ultimo_output > TIMEOUT_ACTIVIDAD:
                    proceso.kill()
                    timeout_ocurrido = True
                    break
                if ahora - ultimo_archivo > TIMEOUT_SIN_ARCHIVOS:
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
                            render_progress_linux(prog_pura)
                        continue

                if es_linea_completa:
                    linea_limpia = linea.strip()
                    if not linea_limpia:
                        continue

                    if "%" in linea_limpia and any(
                        x in linea_limpia for x in ["MB", "KB", "B/s"]
                    ):
                        prog_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()
                        render_progress_linux(prog_pura)
                        continue

                    es_done = "\x1b[2m" in linea_limpia
                    es_error = es_linea_error(linea_limpia)

                    linea_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()

                    if not linea_pura:
                        continue
                    if linea_pura == ultima_ruta:
                        continue
                    ultima_ruta = linea_pura

                    partes = linea_pura.split(" ", 1)
                    nombre_visible_str = partes[1] if len(partes) > 1 else linea_pura

                    contador_seq += 1

                    if es_done:
                        contador_done += 1
                        ultimo_archivo = time.time()
                        sys.stdout.write(
                            f"\r\033[2K  {DIM}[{contador_seq:>3}] [DONE] {nombre_visible_str}{RESET}\n"
                        )
                    elif es_error:
                        errores_hilo.append(linea_pura)
                        sys.stdout.write(
                            f"\r\033[2K  {RED}[{contador_seq:>3}] [X] {limpiar_error(linea_pura)}{RESET}\n"
                        )
                    else:
                        contador_nuevo += 1
                        ultimo_archivo = time.time()
                        archivos_nuevos.append(linea_pura)
                        sys.stdout.write(
                            f"\r\033[2K  {GREEN}[{contador_seq:>3}] {nombre_visible_str}{RESET}\n"
                        )

                    sys.stdout.flush()
    finally:
        proceso.wait()
        clear_line()

        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

        try:
            os.close(master_fd)
        except OSError:
            pass

    return (
        archivos_nuevos,
        errores_hilo,
        contador_nuevo,
        contador_done,
        timeout_ocurrido,
        int(time.time() - inicio),
    )


def ejecutar_con_reintentos(url, intento=1):
    """Ejecuta la descarga con hasta MAX_REINTENTOS reintentos si hay errores."""
    if intento > 1:
        print(f"  {YELLOW}↺ Reintento {intento - 1}/{MAX_REINTENTOS}{RESET}\n")

    if IS_WINDOWS:
        return descargar_windows(url)
    else:
        return descargar_linux(url)


def elegir_lista():
    """Pregunta interactivamente qué lista usar si no se pasaron argumentos."""
    tiene_retry = False
    retry_count = 0
    if os.path.exists(RETRY_FILE):
        with open(RETRY_FILE, encoding="utf-8") as f:
            lineas_retry = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        tiene_retry = bool(lineas_retry)
        retry_count = len(lineas_retry)

    print(f"  {CYAN}[1]{RESET} lista.txt          {GRAY}(descarga normal){RESET}")

    if tiene_retry:
        print(
            f"  {YELLOW}[2]{RESET} lista_retry.txt    {GRAY}({retry_count} URLs pendientes){RESET}"
        )
    else:
        print(f"  {DIM}[2] lista_retry.txt  (vacía o inexistente){RESET}")

    print()

    while True:
        try:
            opcion = input(f"  Opción {CYAN}[1/2]{RESET}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if opcion == "1":
            return LISTA
        elif opcion == "2":
            if not tiene_retry:
                print(f"  {RED}lista_retry.txt está vacía o no existe.{RESET}")
                continue
            return RETRY_FILE
        else:
            print(f"  {YELLOW}Ingresá 1 o 2.{RESET}")


if __name__ == "__main__":
    es_retry_run = False  # Inicializamos el flag en falso

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if os.path.isfile(arg):
            if os.path.abspath(arg) == os.path.abspath(RETRY_FILE):
                es_retry_run = True
            with open(arg, "r", encoding="utf-8") as f:
                urls = [u.strip() for u in f if u.strip() and not u.startswith("#")]
        else:
            urls = sys.argv[1:]
    else:
        print(f"{BOLD}{'═' * 50}{RESET}")
        print(
            f"{BOLD}  Iniciando descarga ({'Windows' if IS_WINDOWS else 'Linux/WSL'}) — seleccioná lista{RESET}"
        )
        print(f"{BOLD}{'═' * 50}{RESET}\n")
        archivo = elegir_lista()
        if archivo == RETRY_FILE:
            es_retry_run = True
        with open(archivo, "r", encoding="utf-8") as f:
            urls = [u.strip() for u in f if u.strip() and not u.startswith("#")]

    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"{BOLD}  Iniciando descarga — {len(urls)} hilos ({'Windows' if IS_WINDOWS else 'Linux/WSL'}){RESET}"
    )
    print(f"  {GRAY}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}\n")

    errores_totales = []
    timeouts_totales = []
    for i, url in enumerate(urls, 1):
        nombre = url.rstrip("/").split("/")[-1][:60]
        log_file = os.path.join(LOG_DIR, f"{nombre}.log")

        print(f"{BOLD}[{i}/{len(urls)}]{RESET} {CYAN}{nombre}{RESET}")
        print(f"  {GRAY}{url}{RESET}\n")

        archivos, errs, nuevos, done, timeout, duracion = ejecutar_con_reintentos(
            url, intento=1
        )

        # Reintentos automáticos si hubo errores normales
        if errs and not timeout:
            for reintento in range(2, MAX_REINTENTOS + 2):
                print(f"\n  {YELLOW}⚠ {len(errs)} errores detectados{RESET}")
                time.sleep(SLEEP_ENTRE_HILOS)
                archivos2, errs2, nuevos2, done2, timeout2, duracion2 = (
                    ejecutar_con_reintentos(url, intento=reintento)
                )

                # Acumular resultados
                archivos += archivos2
                nuevos += nuevos2
                done += done2
                duracion += duracion2

                if not errs2 or timeout2:
                    errs = errs2
                    timeout = timeout2
                    break
                errs = errs2
                timeout = timeout2

        # ── CORRECCIÓN 1: El timeout vuelve al nivel correcto del bucle FOR ──
        if timeout:
            timeouts_totales.append(nombre)
            print(
                f"\n  {RED}⏱ Timeout — sin actividad por {TIMEOUT_ACTIVIDAD}s — proceso terminado{RESET}"
            )
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\nURL: {url}\n")
                f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"TIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n")
                f.write(
                    f"{'=' * 60}\n\n"
                )  # El separador cierra el bloque perfectamente
            if i < len(urls):
                print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
                time.sleep(SLEEP_ENTRE_HILOS)
            print()
            continue

        # ── CORRECCIÓN 2: Escritura normal con su separador al final ──
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\nURL: {url}\n")
            f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            if archivos:
                f.write("\n".join(archivos))
                f.write("\n\n")
            if errs:
                f.write("ERRORES:\n" + "\n".join(errs))
            else:
                f.write("Sin errores.")
            f.write("\n")
            f.write(f"{'=' * 60}\n\n")  # <-- SEPARADOR AGREGADO AQUÍ

        mins, segs = duracion // 60, duracion % 60
        tiempo_str = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"

        resumen = f"{nuevos} nuevos"
        if done > 0:
            resumen += f" | {done} ya descargados"

        print()
        if errs:
            errores_totales.append((nombre, len(errs)))
            print(
                f"  {YELLOW}[X] {nombre} — {resumen} — {len(errs)} errores (ver log) — {tiempo_str}{RESET}"
            )
        elif nuevos > 0:
            print(f"  {GREEN}[✓] {nombre} — {resumen} — {tiempo_str}{RESET}")
        elif done > 0:
            print(f"  {GRAY}[✓] {nombre} — todo ya descargado ({done} archivos){RESET}")
        else:
            print(f"  {GRAY}[✓] {nombre} — sin archivos nuevos{RESET}")

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
    else:
        retry_pendientes = 0
        if os.path.exists(RETRY_FILE):
            with open(RETRY_FILE, "r", encoding="utf-8") as f:
                retry_pendientes = sum(
                    1 for l in f if l.strip() and not l.startswith("#")
                )
        if retry_pendientes:
            print(
                f"\n  {YELLOW}→ {retry_pendientes} URLs en lista_retry.txt — corré auditar.py para analizarlas{RESET}"
            )
    print(f"{BOLD}{'═' * 50}{RESET}")

    # Si se procesó lista_retry.txt → backup y borrar
    if os.path.exists(RETRY_FILE):
        with open(RETRY_FILE, encoding="utf-8") as f:
            urls_retry_procesadas = [
                u.strip() for u in f if u.strip() and not u.startswith("#")
            ]
        if urls_retry_procesadas and es_retry_run:
            fecha_backup = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(BACKUP_FILE, "a", encoding="utf-8") as fb:
                fb.write(f"# {fecha_backup}\n")
                for u in urls_retry_procesadas:
                    fb.write(u + "\n")
                fb.write("\n")
            os.remove(RETRY_FILE)
            print(
                f"  {DIM}lista_retry.txt procesada y vaciada — backup en lista_retry_backup.txt{RESET}"
            )

    if IS_WINDOWS:
        input("\nPresiona Enter para cerrar...")
