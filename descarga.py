#!/usr/bin/env python3
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Forzar UTF-8 en Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select

# =============================================================================
# COLORES ANSI
# =============================================================================
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

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
RE_PREFIJO_NUM = re.compile(r"^\d+\s+")
RE_PROGRESS = re.compile(
    r"(\d+)%\s+([\d.]+)\s*([KkMmGgTt]?[Bb])\s+([\d.]+)\s*([KkMmGgTt]?[Bb]/s)",
    re.IGNORECASE,
)

KEYWORDS_WARNING = ["warning", "rate limit", "sleeping", "skipping"]
KEYWORDS_ERROR = ["error", "failed", "unsupported", "unable", "exception"]
KEYWORDS_RUIDO = [
    "theme-light",
    "color-",
    "--rem",
    "None_",
    "extracted",
    "cookies from",
]

TIMEOUT_ACTIVIDAD = 900
TIMEOUT_SIN_ARCHIVOS = 1800
SLEEP_ENTRE_URLS = 10


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
def cargar_configuracion():
    entorno = "windows" if IS_WINDOWS else "linux"
    ruta_config = Path(__file__).parent / "config.json"
    if not ruta_config.exists():
        print(
            f"\n  {RED}[X] Error: no se encontró config.json en:\n      {ruta_config}{RESET}\n"
        )
        sys.exit(1)
    try:
        with open(ruta_config, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"\n  {RED}[X] Error: config.json inválido:\n      {e}{RESET}\n")
        sys.exit(1)
    try:
        cfg = data[entorno]
        paths = {k: Path(v) for k, v in cfg["paths"].items()}
        return paths, data["pipeline"], cfg["gallery_dl"]
    except KeyError as e:
        print(f"\n  {RED}[X] Error: clave faltante en config.json: {e}{RESET}\n")
        sys.exit(1)


PATHS, PIPELINE, GDL_CFG = cargar_configuracion()


# =============================================================================
# ESTADO
# =============================================================================
def obtener_hash_lista(lista_path: Path) -> str:
    return hashlib.sha256(lista_path.read_bytes()).hexdigest()


def cargar_estado() -> dict:
    state_file = PATHS["state_file"]
    lista = [
        l.strip()
        for l in PATHS["lista_file"].read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#")
    ]
    lista_len = len(lista)
    default = {"batch_index": 0}
    if not state_file.exists():
        return default
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("batch_index", 0) > lista_len:
        print(
            f"\n  {YELLOW}[!] La lista se ha acortado. Reiniciando índice desde 0.{RESET}\n"
        )
        return default
    return state


def guardar_estado(state: dict):
    with open(PATHS["state_file"], "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# =============================================================================
# UTILIDADES
# =============================================================================
def clear_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def formatear_tiempo(segundos):
    m = int(segundos) // 60
    s = int(segundos) % 60
    return f"{m}m {s}s" if m else f"{s}s"


def nombre_visible(ruta):
    base = os.path.basename(ruta)
    return base.split(" ", 1)[1] if " " in base else base


def es_linea_warning(linea):
    ll = linea.lower()
    return any(k in ll for k in KEYWORDS_WARNING) and not any(
        x in linea for x in KEYWORDS_RUIDO
    )


def es_linea_error(linea):
    ll = linea.lower()
    return any(k in ll for k in KEYWORDS_ERROR) and not any(
        x in linea for x in KEYWORDS_RUIDO
    )


def limpiar_error(linea):
    return re.sub(r"^\[gallery-dl\]\s*", "", linea).strip()


# =============================================================================
# SPINNER
# =============================================================================
def spinner_thread(estado, lock_print):
    idx = 0
    ancho_barra = 24
    while not estado["stop"]:
        elapsed = int(time.time() - estado["inicio"])
        tiempo = formatear_tiempo(elapsed)
        resumen = f"{estado['nuevo']} nuevos"
        if estado["done"] > 0:
            resumen += f" | {estado['done']} ya desc."

        pct = estado.get("pct", -1)
        speed = estado.get("speed", "")
        ultima_linea = estado.get("ultima_linea", "")

        if pct >= 0:
            llenos = int(ancho_barra * pct / 100)
            vacios = ancho_barra - llenos
            barra_interna = ("━" * llenos) + ("·" * vacios)
            barra_visual = f"{DIM}│{RESET}{ACCENT}{barra_interna}{RESET}{DIM}│{RESET}"
            descargado = estado.get("descargado", "")
            total_str = estado.get("total", "")
            size_info = (
                f" {descargado}/{total_str} │" if total_str else f" {descargado} │"
            )
            speed_info = f" {speed} │" if speed else ""

            anim = SPINNER[idx % len(SPINNER)]
            with lock_print:
                if ultima_linea:
                    sys.stdout.write(f"\r\033[2K{ultima_linea}\n")
                sys.stdout.write(
                    f"\r  {ACCENT}{anim}{RESET} {barra_visual} "
                    f"{pct:>3}% │{size_info}{speed_info} {DIM}{resumen} — {tiempo}{RESET}\033[K"
                )
                if ultima_linea:
                    sys.stdout.write("\033[1A")
                sys.stdout.flush()
        else:
            pos = idx % (ancho_barra + 4)
            barra_lista = ["·"] * ancho_barra
            for i in range(4):
                p = pos - i
                if 0 <= p < ancho_barra:
                    barra_lista[p] = "━"
            barra_interna = "".join(barra_lista)
            barra_visual = f"{DIM}│{RESET}{ACCENT}{barra_interna}{RESET}{DIM}│{RESET}"

            anim = SPINNER[idx % len(SPINNER)]
            speed_info = f" │ {speed}" if speed else ""
            with lock_print:
                if ultima_linea:
                    sys.stdout.write(f"\r\033[2K{ultima_linea}\n")
                sys.stdout.write(
                    f"\r  {ACCENT}{anim}{RESET} {barra_visual} "
                    f"{DIM}{resumen}{speed_info} — {tiempo}{RESET}\033[K"
                )
                if ultima_linea:
                    sys.stdout.write("\033[1A")
                sys.stdout.flush()

        idx += 1
        time.sleep(0.1)
    clear_line()


def parsear_progreso(linea_pura: str) -> dict | None:
    m = re.match(
        r"(\d+)%\s+([\d.]+)\s*([KkMmGgTt]i?[Bb])\s*/\s*([\d.]+)\s*([KkMmGgTt]i?[Bb])\s+([\d.]+)\s*([KkMmGgTt]i?[Bb]/s)",
        linea_pura.strip(),
        re.IGNORECASE,
    )
    if m:
        return {
            "pct": int(m.group(1)),
            "descargado": f"{float(m.group(2)):.1f}{m.group(3).upper()}",
            "total": f"{float(m.group(4)):.1f}{m.group(5).upper()}",
            "speed": f"{float(m.group(6)):.1f}{m.group(7).upper()}",
        }
    m = re.match(
        r"(\d+)%\s+([\d.]+)\s*([KkMmGgTt]i?[Bb])\s+([\d.]+)\s*([KkMmGgTt]i?[Bb]/s)",
        linea_pura.strip(),
        re.IGNORECASE,
    )
    if m:
        return {
            "pct": int(m.group(1)),
            "descargado": f"{float(m.group(2)):.1f}{m.group(3).upper()}",
            "total": "",
            "speed": f"{float(m.group(4)):.1f}{m.group(5).upper()}",
        }
    return None


# =============================================================================
# WATCHDOG
# =============================================================================
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


# =============================================================================
# VIGILANTE DE .part (Windows)
# =============================================================================
def vigilar_part_thread(estado_watchdog: dict, carpeta_raiz, intervalo: int = 20):
    tamanos_previos = {}
    while not estado_watchdog["stop"]:
        try:
            if os.path.exists(carpeta_raiz):
                encontrados = set()
                for raiz, _, archivos in os.walk(carpeta_raiz):
                    for archivo in archivos:
                        if archivo.endswith(".part"):
                            ruta = os.path.join(raiz, archivo)
                            encontrados.add(ruta)
                            try:
                                tam_actual = os.path.getsize(ruta)
                            except OSError:
                                continue
                            tam_previo = tamanos_previos.get(ruta)
                            if tam_previo is None or tam_actual > tam_previo:
                                estado_watchdog["ultimo_archivo"] = time.time()
                                estado_watchdog["ultimo_output"] = time.time()
                            tamanos_previos[ruta] = tam_actual
                for ruta in list(tamanos_previos.keys()):
                    if ruta not in encontrados:
                        tamanos_previos.pop(ruta, None)
        except OSError:
            pass
        time.sleep(intervalo)


# =============================================================================
# MOTOR WINDOWS
# =============================================================================
def descargar_windows(url: str, nombre_modelo: str):
    cmd = [GDL_CFG["executable"], "-c", GDL_CFG["config_file"], url]

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    warnings_hilo = []
    lock_print = threading.Lock()
    contador = {"seq": 0}

    estado_spinner = {
        "stop": False,
        "inicio": inicio,
        "nuevo": 0,
        "done": 0,
        "pct": -1,
        "speed": "",
        "descargado": "",
        "total": "",
        "ultima_linea": "",
    }
    estado_watchdog = {
        "stop": False,
        "ultimo_output": time.time(),
        "ultimo_archivo": time.time(),
        "timeout": False,
    }

    try:
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
                if not linea_strip:
                    continue
                es_warn = es_linea_warning(linea_strip)
                es_err = not es_warn and es_linea_error(linea_strip)
                if not es_warn and not es_err:
                    continue
                with lock_print:
                    estado_watchdog["ultimo_output"] = time.time()
                    contador["seq"] += 1
                    clear_line()
                    if es_warn:
                        warnings_hilo.append(linea_strip)
                        sys.stdout.write(
                            f"  {YELLOW}[{contador['seq']:>3}] [!] {limpiar_error(linea_strip)}{RESET}\n"
                        )
                    else:
                        errores_hilo.append(linea_strip)
                        sys.stdout.write(
                            f"  {RED}[{contador['seq']:>3}] [X] {limpiar_error(linea_strip)}{RESET}\n"
                        )
                    sys.stdout.flush()

        spin = threading.Thread(
            target=spinner_thread, args=(estado_spinner, lock_print), daemon=True
        )
        watch = threading.Thread(
            target=watchdog_thread,
            args=(proceso, estado_watchdog, TIMEOUT_ACTIVIDAD),
            daemon=True,
        )
        stderr_thr = threading.Thread(target=leer_stderr, daemon=True)
        part_watch = threading.Thread(
            target=vigilar_part_thread,
            args=(estado_watchdog, PATHS["rips_dir"]),
            daemon=True,
        )

        spin.start()
        watch.start()
        stderr_thr.start()
        part_watch.start()

        try:
            for linea in proceso.stdout:
                linea_strip = linea.strip()
                if not linea_strip:
                    continue

                with lock_print:
                    estado_watchdog["ultimo_output"] = time.time()
                    estado_watchdog["ultimo_archivo"] = time.time()
                    contador["seq"] += 1

                    if linea_strip.startswith("#"):
                        estado_spinner["done"] += 1
                        ruta = linea_strip[1:].strip()
                        clear_line()
                        sys.stdout.write(
                            f"  {GRAY}[{contador['seq']:>3}] [DONE] {nombre_modelo} - {nombre_visible(ruta)}{RESET}\n"
                        )
                        sys.stdout.flush()
                        estado_spinner["ultima_linea"] = ""
                    else:
                        estado_spinner["nuevo"] += 1
                        archivos_nuevos.append(linea_strip)
                        clear_line()
                        sys.stdout.write(
                            f"  {GREEN}[{contador['seq']:>3}] {nombre_modelo} - {nombre_visible(linea_strip)}{RESET}\n"
                        )
                        sys.stdout.flush()
                        estado_spinner["ultima_linea"] = ""
        finally:
            try:
                proceso.wait()
            except OSError:
                pass
            stderr_thr.join(timeout=5)
            estado_spinner["stop"] = True
            estado_watchdog["stop"] = True
            spin.join(timeout=2)
            watch.join(timeout=2)
            part_watch.join(timeout=2)

        returncode = proceso.returncode if proceso.returncode is not None else -1

        return (
            archivos_nuevos,
            errores_hilo,
            warnings_hilo,
            estado_spinner["nuevo"],
            estado_spinner["done"],
            estado_watchdog["timeout"],
            int(time.time() - inicio),
            returncode,
        )
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# MOTOR LINUX
# =============================================================================
def descargar_linux(url: str, nombre_modelo: str):
    cmd = [GDL_CFG["executable"], "-c", GDL_CFG["config_file"], url]

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    warnings_hilo = []
    contador_nuevo = 0
    contador_done = 0
    contador_warnings = 0
    contador_seq = 0
    ultima_ruta = ""
    timeout_ocurrido = False
    ultimo_archivo = time.time()

    env_vars = os.environ.copy()
    env_vars["PYTHONUNBUFFERED"] = "1"

    lock_print = threading.Lock()
    estado_spinner = {
        "stop": False,
        "inicio": inicio,
        "nuevo": 0,
        "done": 0,
        "pct": -1,
        "speed": "",
        "descargado": "",
        "total": "",
        "ultima_linea": "",
    }

    try:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

        spin = threading.Thread(
            target=spinner_thread, args=(estado_spinner, lock_print), daemon=True
        )
        spin.start()

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
                        texto_antes = buffer[:idx_r]

                        if texto_antes.strip() and not re.search(r"\d+%", texto_antes):
                            linea = texto_antes
                            buffer = buffer[idx_r + 1 :]
                            es_linea_complete = True
                        else:
                            if idx_r == len(buffer) - 1:
                                break
                            buffer = buffer[idx_r + 1 :]
                            prog_limpio = ANSI_ESCAPE.sub("", texto_antes).strip()
                            if prog_limpio and "%" in prog_limpio:
                                datos = parsear_progreso(prog_limpio)
                                if datos:
                                    estado_spinner["pct"] = datos["pct"]
                                    estado_spinner["descargado"] = datos["descargado"]
                                    estado_spinner["total"] = datos["total"]
                                    estado_spinner["speed"] = datos["speed"]
                                    ultimo_archivo = time.time()
                            continue

                    if es_linea_complete:
                        linea_limpia = linea.strip()
                        if not linea_limpia:
                            continue

                        linea_pura_check = ANSI_ESCAPE.sub("", linea_limpia).lower()
                        if any(k in linea_pura_check for k in KEYWORDS_RUIDO):
                            continue

                        if "%" in linea_limpia and any(
                            x in linea_limpia
                            for x in ["MB", "KB", "B/s", "MiB", "KiB", "GiB"]
                        ):
                            prog_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()
                            datos = parsear_progreso(prog_pura)
                            if datos:
                                estado_spinner["pct"] = datos["pct"]
                                estado_spinner["descargado"] = datos["descargado"]
                                estado_spinner["total"] = datos["total"]
                                estado_spinner["speed"] = datos["speed"]
                                ultimo_archivo = time.time()
                            continue

                        es_done = "\x1b[2m" in linea_limpia
                        es_warning = not es_done and es_linea_warning(linea_limpia)
                        es_error = (
                            not es_done
                            and not es_warning
                            and (
                                es_linea_error(linea_limpia)
                                or "connection broken" in linea_limpia.lower()
                                or "incompleteread" in linea_limpia.lower()
                            )
                        )
                        linea_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()

                        if not linea_pura or linea_pura == ultima_ruta:
                            continue
                        ultima_ruta = linea_pura

                        nombre_sin_prefijo = (
                            RE_PREFIJO_NUM.sub("", linea_pura)
                            if RE_PREFIJO_NUM.match(linea_pura)
                            else linea_pura
                        )
                        carpeta_visible = os.path.basename(
                            os.path.dirname(nombre_sin_prefijo)
                        )
                        archivo_visible = os.path.basename(nombre_sin_prefijo)
                        nombre_visible_str = (
                            f"{carpeta_visible} {archivo_visible}"
                            if carpeta_visible
                            else archivo_visible
                        )
                        contador_seq += 1

                        with lock_print:
                            estado_spinner["pct"] = -1
                            estado_spinner["descargado"] = ""
                            estado_spinner["total"] = ""
                            estado_spinner["speed"] = ""
                            estado_spinner["ultima_linea"] = ""

                            clear_line()
                            if es_done:
                                contador_done += 1
                                ultimo_archivo = time.time()
                                estado_spinner["done"] = contador_done
                                sys.stdout.write(
                                    f"  {DIM}[{contador_seq:>3}] [DONE] {nombre_visible_str}{RESET}\n"
                                )
                            elif es_warning:
                                contador_warnings += 1
                                warnings_hilo.append(linea_pura)
                                sys.stdout.write(
                                    f"  {YELLOW}[{contador_seq:>3}] [!] {limpiar_error(linea_pura)}{RESET}\n"
                                )
                            elif es_error:
                                errores_hilo.append(linea_pura)
                                sys.stdout.write(
                                    f"  {RED}[{contador_seq:>3}] [X] {limpiar_error(linea_pura)}{RESET}\n"
                                )
                            else:
                                contador_nuevo += 1
                                ultimo_archivo = time.time()
                                archivos_nuevos.append(linea_pura)
                                estado_spinner["nuevo"] = contador_nuevo
                                sys.stdout.write(
                                    f"  {GREEN}[{contador_seq:>3}] {nombre_visible_str}{RESET}\n"
                                )
                            sys.stdout.flush()

        finally:
            if proceso is not None:
                try:
                    proceso.wait()
                except OSError:
                    pass
            estado_spinner["stop"] = True
            spin.join(timeout=2)
            clear_line()
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass

        returncode = (
            proceso.returncode
            if proceso is not None and proceso.returncode is not None
            else -1
        )

        return (
            archivos_nuevos,
            errores_hilo,
            warnings_hilo,
            contador_nuevo,
            contador_done,
            timeout_ocurrido,
            int(time.time() - inicio),
            returncode,
        )
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# EJECUTOR DE URL
# =============================================================================
def ejecutar_url(url: str) -> dict:
    nombre = url.rstrip("/").split("/")[-1][:60]
    log_path = PATHS["log_dir"] / f"{nombre}.log"
    PATHS["log_dir"].mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        archivos, errs, warns, nuevos, done, timeout, duracion, returncode = (
            descargar_windows(url, nombre)
        )
    else:
        archivos, errs, warns, nuevos, done, timeout, duracion, returncode = (
            descargar_linux(url, nombre)
        )

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nURL: {url}\n")
        if archivos:
            f.write("\n".join(archivos) + "\n")
        if errs:
            f.write("ERRORES:\n" + "\n".join(errs) + "\n")
        else:
            f.write("Sin errores.\n")
        if warns:
            f.write("WARNINGS:\n" + "\n".join(warns) + "\n")
        resumen_forense = (
            f'[RESUMEN] nombre_modelo="{nombre}" url="{url}" nuevos="{nuevos}" '
            f'ya_descargados="{done}" errores="{len(errs)}" warnings="{len(warns)}" '
            f'duracion="{duracion}" returncode="{returncode}" timeout="{timeout}"\n'
        )
        f.write(resumen_forense)
        f.write("=" * 60 + "\n\n")

    return {
        "nombre": nombre,
        "ok": not timeout and len(errs) == 0,
        "nuevos": nuevos,
        "done": done,
        "errores": len(errs),
        "warnings": len(warns),
        "timeout": timeout,
        "duracion": duracion,
        "returncode": returncode,
        "archivos": archivos,
        "errs": errs,
        "warns": warns,
    }


# =============================================================================
# PROCESADOR DE LOTE
# =============================================================================
def _imprimir_resumen_url(res: dict):
    nombre = res["nombre"]
    nuevos = res["nuevos"]
    done = res["done"]
    warnings = res.get("warnings", 0)
    tiempo_str = formatear_tiempo(res["duracion"])
    resumen = f"{nuevos} nuevos" + (f" | {done} ya descargados" if done > 0 else "")
    warn_str = f" | {warnings} warning(s)" if warnings > 0 else ""

    print()

    if res["timeout"]:
        print(
            f"  {RED}[T] {nombre} — timeout ({TIMEOUT_ACTIVIDAD}s sin actividad){RESET}"
        )
    elif res["errores"] > 0:
        print(
            f"  {RED}[X] {nombre} — {resumen}{warn_str} — {res['errores']} error(es) (ver log) — {tiempo_str}{RESET}"
        )
    elif nuevos > 0:
        print(f"  {GREEN}[+] {nombre} — {resumen}{warn_str} — {tiempo_str}{RESET}")
    elif warnings > 0:
        print(f"  {YELLOW}[!] {nombre} — {resumen}{warn_str} — {tiempo_str}{RESET}")
    elif done > 0:
        print(f"  {GRAY}[OK] {nombre} — todo ya descargado ({done} archivos){RESET}")
    else:
        print(f"  {GRAY}[OK] {nombre} — sin archivos nuevos{RESET}")


def procesar_lote(lote: list):
    lote_dedup = []
    vistos: set = set()
    for url in lote:
        match_id = re.search(r"\.(\d+)/?$", url)
        clave = match_id.group(1) if match_id else url
        if clave not in vistos:
            vistos.add(clave)
            lote_dedup.append(url)

    omitidas = len(lote) - len(lote_dedup)
    total = len(lote_dedup)

    print(f"{BOLD}{'═' * 55}{RESET}")
    print(
        f"  Iniciando lote — {total} URL{'s' if total != 1 else ''}"
        + (f" ({omitidas} duplicadas omitidas)" if omitidas else "")
        + f" ({'Windows' if IS_WINDOWS else 'Linux/WSL'})"
    )
    print(f"  {GRAY}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 55}{RESET}\n")

    errores_totales = []
    timeouts_totales = []
    totales = {"nuevos": 0, "done": 0, "duracion": 0}
    warnings_totales = []

    for i, url in enumerate(lote_dedup, 1):
        print(
            f"{BOLD}[{i}/{total}]{RESET} {CYAN}{url.rstrip('/').split('/')[-1][:60]}{RESET}"
        )
        print(f"  {GRAY}{url}{RESET}\n")

        res = ejecutar_url(url)
        _imprimir_resumen_url(res)

        totales["nuevos"] += res["nuevos"]
        totales["done"] += res["done"]
        totales["duracion"] += res["duracion"]
        if res.get("warnings", 0) > 0:
            warnings_totales.append((res["nombre"], res["warnings"]))

        if res["timeout"]:
            timeouts_totales.append(res["nombre"])
        elif res["errores"] > 0:
            errores_totales.append((res["nombre"], res["errores"]))

        if i < total:
            print(f"\n  {DIM}Esperando {SLEEP_ENTRE_URLS}s...{RESET}\n")
            time.sleep(SLEEP_ENTRE_URLS)

    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"  Lote terminado — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if errores_totales:
        print(f"\n  {YELLOW}Con errores:{RESET}")
        for n, c in errores_totales:
            print(f"    {n}: {c} error(es)")

    if timeouts_totales:
        print(f"\n  {RED}Con timeout:{RESET}")
        for n in timeouts_totales:
            print(f"    {n}")

    if not errores_totales and not timeouts_totales:
        print(f"  {GREEN}Todos los hilos sin errores.{RESET}")

    if warnings_totales:
        print(f"\n  {YELLOW}Con warnings:{RESET}")
        for n, c in warnings_totales:
            print(f"    {n}: {c} warning(s)")

    print(f"\n  Archivos nuevos     : {BOLD}{totales['nuevos']}{RESET}")
    print(f"  Ya descargados      : {BOLD}{totales['done']}{RESET}")
    print(
        f"  Tiempo total        : {BOLD}{formatear_tiempo(totales['duracion'])}{RESET}"
    )
    print(f"{BOLD}{'═' * 55}{RESET}\n")


# =============================================================================
# MAIN
# =============================================================================
def main():
    import shutil

    CENTINELA = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "descarga.running"
    )

    if os.path.exists(CENTINELA):
        if time.time() - os.path.getmtime(CENTINELA) > 60:
            os.remove(CENTINELA)

    # Abrir monitor en split de Windows Terminal
    if IS_WINDOWS and os.environ.get("WT_SESSION"):
        monitor_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "monitor.py"
        )
        if os.path.exists(monitor_script):
            subprocess.Popen(
                [
                    "wt",
                    "-w",
                    "0",
                    "split-pane",
                    "--horizontal",
                    "--size",
                    "0.35",
                    "--title",
                    "Monitor",
                    "python",
                    monitor_script,
                ],
                shell=False,
            )
            # Devolver el foco al panel principal (llamada separada, más segura
            # que meter ';focus-pane' en el mismo comando)
            time.sleep(1)
            try:
                subprocess.Popen(
                    ["wt", "-w", "0", "focus-pane", "-t", "0"],
                    shell=False,
                )
            except OSError:
                pass

    open(CENTINELA, "w").close()

    try:
        if not shutil.which(GDL_CFG["executable"]):
            print(
                f"\n  {RED}[X] Error: '{GDL_CFG['executable']}' no encontrado en PATH.{RESET}"
            )
            print("      Instala gallery-dl y asegúrate de que esté en el PATH.\n")
            if IS_WINDOWS:
                input(f"  {GRAY}Presiona Enter para salir...{RESET}")
            sys.exit(1)

        state = cargar_estado()
        lista = [
            l.strip()
            for l in PATHS["lista_file"].read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")
        ]

        batch_size = PIPELINE["batch_size"]
        lote = lista[state["batch_index"] : state["batch_index"] + batch_size]

        if not lote:
            print()
            print(f"  {GREEN}[+] Lista completada. Reiniciando índice.{RESET}")
            print()
            nuevo_state = {"batch_index": 0}
            guardar_estado(nuevo_state)
            lote = lista[:batch_size]
            state = nuevo_state

        procesar_lote(lote)

        state["batch_index"] += len(lote)
        guardar_estado(state)

        auditar_py = Path(__file__).parent / "auditar.py"
        if auditar_py.exists():
            print(f"  {CYAN}Ejecutando auditoría...{RESET}\n")
            subprocess.run([sys.executable, str(auditar_py)])
        else:
            print(f"  {DIM}(auditar.py no encontrado, se omite){RESET}")

        if IS_WINDOWS:
            input(f"\n  {GRAY}Presiona Enter para salir...{RESET}")

    except KeyboardInterrupt:
        print(f"\n  {YELLOW}[!] Interrupción por usuario.{RESET}\n")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    finally:
        if os.path.exists(CENTINELA):
            os.remove(CENTINELA)


if __name__ == "__main__":
    main()
