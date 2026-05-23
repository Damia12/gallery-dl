#!/usr/bin/env python3
import errno
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

# Forzar codificación UTF-8 en la terminal para evitar fallos con caracteres raros
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select

# =============================================================================
# CONFIGURACIÓN DE RUTAS FIJAS MULTIPLATAFORMA
# =============================================================================
if IS_WINDOWS:
    GALLERY_DL = "gallery-dl.exe"
    CONFIG = r"C:\gallery-dl\gallery-dl_win.conf"
    LISTA = r"C:\gallery-dl\lista.txt"
    LOG_DIR = r"G:\Rips\logs"
    RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
    BACKUP_FILE = r"C:\gallery-dl\lista_retry_backup.txt"
    RIPS_DIR = r"G:\Rips"
else:
    GALLERY_DL = "gallery-dl"
    CONFIG = os.path.expanduser("~/gallery-dl/gallery-dl_linux.conf")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")
    LOG_DIR = os.path.expanduser("~/Rips/logs")
    RETRY_FILE = os.path.expanduser("~/gallery-dl/lista_retry.txt")
    BACKUP_FILE = os.path.expanduser("~/gallery-dl/lista_retry_backup.txt")
    RIPS_DIR = os.path.expanduser("~/Rips")

SLEEP_ENTRE_HILOS = 30
TIMEOUT_ACTIVIDAD = 300
TIMEOUT_SIN_ARCHIVOS = 600
MAX_REINTENTOS = 2

os.makedirs(LOG_DIR, exist_ok=True)

# Estilos y Colores ANSI
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

KEYWORDS_ERROR = ["error", "warning", "failed", "unsupported", "unable", "exception"]
KEYWORDS_RUIDO = ["theme-light", "color-", "--rem", "None_"]


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


def formatear_nombre_modelo(nombre_hilo):
    base = nombre_hilo.split(".")[0]
    partes = base.split("-")
    vistas = []
    for p in partes:
        if p not in vistas and len(vistas) < 2:
            vistas.append(p.capitalize())
    return " ".join(vistas)


def es_linea_error(linea):
    linea_lower = linea.lower()
    return any(k in linea_lower for k in KEYWORDS_ERROR) and not any(
        x in linea for x in KEYWORDS_RUIDO
    )


def limpiar_error(linea):
    return re.sub(r"^\[gallery-dl\]\s*", "", linea).strip()


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
            try:
                proceso.kill()
            except OSError:
                pass
            estado["timeout"] = True
            break
        if ahora - estado["ultimo_archivo"] > TIMEOUT_SIN_ARCHIVOS:
            try:
                proceso.kill()
            except OSError:
                pass
            estado["timeout"] = True
            break

        time.sleep(5)


def descargar_windows(url, nombre_modelo, extra_flags=None):
    cmd = [GALLERY_DL, "-c", CONFIG]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(url)
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
                        f"  {GRAY}[{contador['seq']:>3}] [DONE] {nombre_modelo} - {nombre_visible(ruta)}{RESET}"
                    )
                else:
                    estado_spinner["nuevo"] += 1
                    estado_watchdog["ultimo_archivo"] = time.time()
                    contador["seq"] += 1
                    archivos_nuevos.append(linea_strip)
                    print(
                        f"  {GREEN}[{contador['seq']:>3}] {RESET} {nombre_modelo} - {nombre_visible(linea_strip)}"
                    )
    finally:
        try:
            proceso.wait()
        except OSError:
            pass
        stderr_thread.join(timeout=5)
        estado_spinner["stop"] = True
        estado_watchdog["stop"] = True
        spin.join(timeout=2)
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


def descargar_linux(url, nombre_modelo, extra_flags=None):
    cmd = [GALLERY_DL, "-c", CONFIG]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(url)
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

    master_fd = None
    proceso = None
    buffer = ""
    ultimo_output = time.time()

    try:
        master_fd, slave_fd = pty.openpty()
        proceso = subprocess.Popen(
            cmd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env_vars
        )
        os.close(slave_fd)

        while True:
            r, _, _ = select.select([master_fd], [], [], 1.0)

            if not r:
                ahora = time.time()
                if ahora - ultimo_output > TIMEOUT_ACTIVIDAD:
                    try:
                        proceso.kill()
                    except OSError:
                        pass
                    timeout_ocurrido = True
                    break
                if ahora - ultimo_archivo > TIMEOUT_SIN_ARCHIVOS:
                    try:
                        proceso.kill()
                    except OSError:
                        pass
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

                es_linea_complete = False
                linea = ""

                if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
                    linea = buffer[:idx_n]
                    buffer = buffer[idx_n + 1 :]
                    es_linea_complete = True

                elif idx_r != -1:
                    texto_antes_del_r = buffer[:idx_r]

                    if texto_antes_del_r.strip() and not re.search(
                        r"\d+%", texto_antes_del_r
                    ):
                        linea = texto_antes_del_r
                        buffer = buffer[idx_r + 1 :]
                        es_linea_complete = True
                    else:
                        if idx_r == len(buffer) - 1:
                            break
                        buffer = buffer[idx_r + 1 :]
                        prog_limpio = texto_antes_del_r.strip()
                        if prog_limpio and "%" in prog_limpio:
                            prog_pura = ANSI_ESCAPE.sub("", prog_limpio).strip()
                            render_progress_linux(prog_pura)
                        continue

                if es_linea_complete:
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

                    if not linea_pura or linea_pura == ultima_ruta:
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
        if proceso is not None:
            try:
                proceso.wait()
            except OSError:
                pass
        clear_line()
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        if master_fd is not None:
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


def esperar_entre_hilos(i, total):
    if i < total:
        print(f"\n  {DIM}Esperando {SLEEP_ENTRE_HILOS}s...{RESET}\n")
        time.sleep(SLEEP_ENTRE_HILOS)
    print()


def ejecutar_descarga(url, nombre_modelo, intento=1, extra_flags=None):
    if intento > 1:
        print(f"  {YELLOW}[!] Reintento {intento - 1}/{MAX_REINTENTOS}{RESET}\n")
    if IS_WINDOWS:
        return descargar_windows(url, nombre_modelo, extra_flags)
    else:
        return descargar_linux(url, nombre_modelo, extra_flags)


def procesar_descargas(urls, es_retry_run=False):
    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"  Iniciando descarga — {len(urls)} hilos ({'Windows' if IS_WINDOWS else 'Linux/WSL'}){RESET}"
    )
    print(f"  {GRAY}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 50}{RESET}\n")

    errores_totales = []
    timeouts_totales = []

    for i, item in enumerate(urls, 1):
        if isinstance(item, dict):
            url = item["url"]
            extra_flags = item["extra_flags"]
        else:
            url = item
            extra_flags = None

        nombre_base = url.rstrip("/").split("/")[-1][:60]

        # Inicialización del formateador de modelo
        nombre_modelo = formatear_nombre_modelo(nombre_base)

        # CORRECCIÓN PUNTO 4: Aislamiento de identificador numérico de hilo
        match_id = re.search(r"\.(\d+)/?$", url)
        id_str = match_id.group(1) if match_id else "000000"
        nombre = f"{nombre_base}.{id_str}"

        log_file = os.path.join(LOG_DIR, f"{nombre}.log")

        print(f"{BOLD}[{i}/{len(urls)}]{RESET} {CYAN}{nombre}{RESET}")
        print(f"  {GRAY}{url}{RESET}\n")

        archivos, errs, nuevos, done, timeout, duracion = ejecutar_descarga(
            url=url, nombre_modelo=nombre_modelo, intento=1, extra_flags=extra_flags
        )

        if timeout:
            timeouts_totales.append(nombre)
            print(
                f"\n  {RED}[T] Timeout — sin actividad por {TIMEOUT_ACTIVIDAD}s — proceso terminado{RESET}"
            )
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(
                    f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\nURL: {url}\n"
                )
                f.write(
                    f"TIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n{'=' * 60}\n\n"
                )
            esperar_entre_hilos(i, len(urls))
            continue

        if errs and es_retry_run:
            errs_acumulados = list(errs)
            for reintento in range(1, MAX_REINTENTOS + 1):
                print(
                    f"\n  {YELLOW}[!] {len(errs_acumulados)} errores detectados{RESET}"
                )
                time.sleep(SLEEP_ENTRE_HILOS)
                archivos2, errs2, nuevos2, done2, timeout2, duracion2 = (
                    ejecutar_descarga(
                        url=url,
                        nombre_modelo=nombre_modelo,
                        intento=reintento + 1,
                        extra_flags=extra_flags,
                    )
                )
                archivos += archivos2
                nuevos += nuevos2
                done += done2
                duracion += duracion2

                if timeout2:
                    timeout = True
                    break
                errs_acumulados = errs2
                if not errs2:
                    break
            errs = errs_acumulados

        if timeout:
            timeouts_totales.append(nombre)
            print(
                f"\n  {RED}[X] Timeout — sin actividad por {TIMEOUT_ACTIVIDAD}s — proceso terminado{RESET}"
            )
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(
                    f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\nURL: {url}\n"
                )
                if errs:
                    f.write("ERRORES:\n" + "\n".join(errs) + "\n")
                f.write(
                    f"TIMEOUT: sin actividad por {TIMEOUT_ACTIVIDAD}s\n{'=' * 60}\n\n"
                )
            esperar_entre_hilos(i, len(urls))
            continue

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(
                f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\nURL: {url}\n"
            )
            if archivos:
                f.write("\n".join(archivos) + "\n\n")
            if errs:
                f.write("ERRORES:\n" + "\n".join(errs))
            else:
                f.write("Sin errores.")
            f.write(f"\n{'=' * 60}\n\n")

        mins, segs = duracion // 60, duracion % 60
        tiempo_str = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"
        resumen = f"{nuevos} nuevos"
        if done > 0:
            resumen += f" | {done} ya descargados"

        print()
        if errs:
            errores_totales.append((nombre, len(errs)))
            print(
                f"  {YELLOW}[!] {nombre} — {resumen} — {len(errs)} (ver log) — {tiempo_str}{RESET}"
            )
        elif nuevos > 0:
            print(f"  {GREEN}[+] {nombre} — {resumen} — {tiempo_str}{RESET}")
        elif done > 0:
            print(
                f"  {GRAY}[OK] {nombre} — todo ya descargado ({done} archivos){RESET}"
            )
        else:
            print(f"  {GRAY}[OK] {nombre} — sin archivos nuevos{RESET}")

        esperar_entre_hilos(i, len(urls))

    # Resumen final
    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"  Descarga terminada — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}"
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
    print(f"{BOLD}{'═' * 50}{RESET}\n")

    if os.path.exists(RETRY_FILE):
        # MITIGACIÓN DE BUG: Cambiado modo "w" a "r" para evitar truncado prematuro
        with open(RETRY_FILE, "r", encoding="utf-8") as f:
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


def elegir_lista():
    tiene_retry = False
    retry_count = 0
    if os.path.exists(RETRY_FILE):
        with open(RETRY_FILE, "r", encoding="utf-8") as f:
            lineas_retry = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        tiene_retry = bool(lineas_retry)
        retry_count = len(lineas_retry)

    os.system("cls" if os.name == "nt" else "clear")
    print(f"{BOLD}{'═' * 50}{RESET}")
    print(
        f"  {'Windows' if IS_WINDOWS else 'Linux/WSL'} — selecciona una opción{RESET}"
    )
    print(f"{BOLD}{'═' * 50}{RESET}\n")
    print(f"  {GREEN}[1]{RESET} lista.txt          {GRAY}(descarga normal){RESET}")

    if tiene_retry:
        print(
            f"  {MAGENTA}[2]{RESET} lista_retry.txt    {GRAY}({retry_count} URLs pendientes){RESET}"
        )
    else:
        print(f"  {DIM}[2] lista_retry.txt  (vacía o inexistente){RESET}")

    print(
        f"  {CYAN}[3]{RESET} auditar            {GRAY}(analizar logs y generar retry){RESET}"
    )
    print(f"  {GRAY}[4]{RESET} salir")
    print()

    while True:
        try:
            opcion = input(f"  Opción {WHITE}[1/2/3/4]{RESET}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ("SALIR", None)

        if opcion == "1":
            return ("DESCARGA", LISTA)
        elif opcion == "2":
            if not tiene_retry:
                print(f"  {RED}lista_retry.txt está vacía o no existe.{RESET}")
                continue
            return ("DESCARGA", RETRY_FILE)
        elif opcion == "3":
            return ("AUDITAR", None)
        elif opcion == "4":
            return ("SALIR", None)
        else:
            print(f"  {YELLOW}Ingresa 1, 2, 3 o 4.{RESET}")


# ==========================================
# INTERFAZ INTERACTIVE CENTRAL (REPL)
# ==========================================
if __name__ == "__main__":
    # CORRECCIÓN PUNTO 15: Validar que gallery-dl esté instalado y accesible al inicio
    if not shutil.which(GALLERY_DL):
        print(
            f"\n  {RED}[X] Error Crítico: No se encontró '{GALLERY_DL}' en el PATH del sistema.{RESET}"
        )
        print(
            "      Asegúrese de que gallery-dl esté instalado y correctamente configurado.\n"
        )
        if IS_WINDOWS:
            input(f"  {GRAY}Presiona Enter para salir...{RESET}")
        sys.exit(1)

    while True:
        accion, payload = elegir_lista()

        if accion == "SALIR":
            break

        elif accion == "AUDITAR":
            print(f"\n  {CYAN}Ejecutando auditar.py en subproceso aislado...{RESET}\n")
            ruta_directorio_actual = os.path.dirname(os.path.abspath(__file__))
            ruta_auditar = os.path.join(ruta_directorio_actual, "auditar.py")

            # MITIGACIÓN DE BUG: Se reemplazó el string literal por la variable 'ruta_auditar'
            subprocess.run([sys.executable, ruta_auditar])
            print()
            if IS_WINDOWS:
                input(f"  {GRAY}Presiona Enter para volver al menú...{RESET}")
            continue

        elif accion == "DESCARGA":
            es_retry_run = payload == RETRY_FILE
            urls = []
            vistos = set()  # Control de deduplicación en RAM

            if es_retry_run:
                meta_actual = None
                try:
                    with open(payload, "r", encoding="utf-8") as f:
                        for linea in f:
                            linea = linea.strip()
                            if not linea:
                                continue

                            if linea.startswith("#META:"):
                                match_meta = re.search(
                                    r"id=(\S+)\s*\|\s*folder=(.+)", linea
                                )
                                if match_meta:
                                    meta_actual = {
                                        "id": match_meta.group(1),
                                        "folder": match_meta.group(2).strip(),
                                    }
                                continue

                            if linea.startswith("http"):
                                if linea in vistos:
                                    meta_actual = None
                                    continue
                                vistos.add(linea)

                                extra_flags = None

                                if meta_actual and meta_actual["id"] != "Desconocido":
                                    # CORRECCIÓN PUNTO 2: Uso estricto de RIPS_DIR multiplataforma
                                    ruta_destino = os.path.join(
                                        RIPS_DIR, "Simpcity", meta_actual["folder"]
                                    )
                                    prefijo_nombre = f"{meta_actual['id']}_{{filename}}.{{extension}}"
                                    extra_flags = [
                                        "-o",
                                        f"directory={ruta_destino}",
                                        "-o",
                                        f"filename={prefijo_nombre}",
                                    ]

                                urls.append({"url": linea, "extra_flags": extra_flags})
                                meta_actual = None
                except Exception as e:
                    print(
                        f"\n  {RED}[X] Error crítico leyendo archivo de reintentos: {e}{RESET}\n"
                    )
                    if IS_WINDOWS:
                        input(f"  {GRAY}Presiona Enter para volver al menú...{RESET}")
                    continue
            else:
                with open(payload, "r", encoding="utf-8") as f:
                    for u in f:
                        u = u.strip()
                        if not u or u.startswith("#"):
                            continue
                        match_id = re.search(r"\.(\d+)/?$", u)
                        id_unico = match_id.group(1) if match_id else u
                        if id_unico not in vistos:
                            vistos.add(id_unico)
                            urls.append(u)

            if not urls:
                print(
                    f"\n  {YELLOW}El archivo está vacío o no contiene URLs válidas.{RESET}\n"
                )
                if IS_WINDOWS:
                    input(f"  {GRAY}Presiona Enter para volver al menú...{RESET}")
                continue

            procesar_descargas(urls, es_retry_run=es_retry_run)

            print(
                f"\n  {GRAY}Proceso completado. Presiona Enter para volver al menú...{RESET}"
            )
            input()

            # if IS_WINDOWS:
            #     input(f"  {GRAY}Presiona Enter para volver al menú...{RESET}")
