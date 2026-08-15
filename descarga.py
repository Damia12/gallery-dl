#!/usr/bin/env python3
import errno
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
TIMEOUT_ACTIVIDAD_LENTO = 7200  # 2 horas para Bunkr / Simpcity
TIMEOUT_SIN_ARCHIVOS = 1800
SLEEP_ENTRE_URLS = 10


# =============================================================================
# CONFIGURACION
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
# SKIP POSTS  (soporta string directo o dict con "skip")
# =============================================================================
def cargar_skip_posts() -> dict:
    """Carga el mapa URL → --post-range directo (string).

    Soporta dos formatos:

    Formato A (directo): URL -> "post-range" string
    {
      "https://simpcity.su/threads/nombre.12345": "6-"
    }

    Formato B (skip): URL -> dict con lista de posts a saltear
    {
      "https://simpcity.su/threads/nombre.12345": {"skip": [6]},
      "https://simpcity.su/threads/otro.67890": {"skip": [3, 6, 9]}
    }
    """
    skip_file = PATHS.get("skip_posts_file")
    if not skip_file or not skip_file.exists():
        return {}
    try:
        with open(skip_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        validado = {}
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str) or (
                isinstance(v, dict) and ("skip" in v or "omitir" in v)
            ):
                validado[k.rstrip("/")] = v
            else:
                print(
                    f"  {YELLOW}[!] Skip posts: valor inválido para '{k}' "
                    f"(se esperaba string o dict con 'skip'). Se ignora.{RESET}"
                )
        return validado
    except (json.JSONDecodeError, OSError):
        return {}


def convertir_skip_a_range(valor: dict) -> str | None:
    """Convierte {'skip': [3, 6, '1-5', 50]} en string de --post-range."""
    rangos = valor.get("skip") or valor.get("omitir", [])
    if not rangos:
        return None

    skip_set = set()
    for r in rangos:
        r = str(r).strip()
        if "-" in r:
            partes = r.split("-")
            if len(partes) == 2 and partes[0] and partes[1]:
                try:
                    ini, fin = int(partes[0]), int(partes[1])
                    skip_set.update(range(ini, fin + 1))
                except ValueError:
                    continue
        else:
            try:
                skip_set.add(int(r))
            except ValueError:
                continue

    if not skip_set:
        return None

    max_skip = max(skip_set)
    limite_superior = max_skip + 10000

    descargar = []
    inicio = 1
    while inicio <= limite_superior:
        if inicio in skip_set:
            inicio += 1
            continue
        fin = inicio
        while fin + 1 <= limite_superior and (fin + 1) not in skip_set:
            fin += 1
        if fin >= limite_superior:
            descargar.append(f"{inicio}-")
            break
        else:
            descargar.append(f"{inicio}-{fin}")
            inicio = fin + 1

    if not descargar:
        return None

    return ",".join(descargar)


# =============================================================================
# POSTS FALLIDOS  (reporte de sugerencias, NO se aplica automáticamente)
# =============================================================================


def obtener_posts_fallidos_path() -> Path:
    """Devuelve la ruta de posts_fallidos.json (misma carpeta que skip_posts)."""
    skip_file = PATHS.get("skip_posts_file")
    if skip_file:
        return skip_file.parent / "posts_fallidos.json"
    return Path(__file__).parent / "posts_fallidos.json"


def cargar_posts_fallidos() -> dict:
    """Carga el reporte de posts fallidos sugeridos."""
    pf_path = obtener_posts_fallidos_path()
    if not pf_path.exists():
        return {}
    try:
        with open(pf_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def guardar_posts_fallidos(data: dict):
    """Guarda el reporte de posts fallidos."""
    pf_path = obtener_posts_fallidos_path()
    try:
        with open(pf_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"  {YELLOW}[!] No se pudo guardar posts_fallidos.json: {e}{RESET}")


def extraer_posts_desde_range(post_range: str | None) -> set:
    """Extrae los posts numéricos de un string de post-range."""
    posts = set()
    if not post_range or post_range == "none":
        return posts
    for parte in post_range.split(","):
        parte = parte.strip()
        if "-" in parte:
            ini_fin = parte.split("-")
            if len(ini_fin) == 2:
                try:
                    ini = int(ini_fin[0])
                    fin_str = ini_fin[1].strip()
                    if fin_str:
                        posts.update(range(ini, int(fin_str) + 1))
                    else:
                        posts.add(ini)
                except ValueError:
                    pass
        else:
            try:
                posts.add(int(parte))
            except ValueError:
                pass
    return posts


def detectar_y_reportar_fallidos(
    url: str,
    res: dict,
    post_range: str | None,
    rangos_skip_previos: list | None,
):
    """Detecta posts candidatos a fallidos y guarda en posts_fallidos.json como reporte."""
    fallidos = cargar_posts_fallidos()
    url_key = url.rstrip("/")

    # Solo reportar si hay timeout total o fatal
    if res["timeout"] and res["nuevos"] == 0:
        estado = "timeout_atascado"
    elif res["errores"] > 0 and res["returncode"] != 0:
        estado = "fatal"
    else:
        return  # No reportar nada si hubo progreso o está OK

    posts_intentados = extraer_posts_desde_range(post_range)
    if not posts_intentados:
        return

    ya_skipeados = set()
    if rangos_skip_previos:
        ya_skipeados.update(rangos_skip_previos)

    candidatos = sorted(posts_intentados - ya_skipeados)
    if not candidatos:
        return

    if url_key not in fallidos:
        fallidos[url_key] = {
            "skip": [],
            "razon": estado,
            "fecha": datetime.now().strftime("%Y-%m-%d"),
            "intentos": 0,
        }

    existing = set(fallidos[url_key].get("skip", []))
    existing.update(candidatos)
    fallidos[url_key]["skip"] = sorted(existing)
    fallidos[url_key]["razon"] = estado
    fallidos[url_key]["fecha"] = datetime.now().strftime("%Y-%m-%d")
    fallidos[url_key]["intentos"] = fallidos[url_key].get("intentos", 0) + 1

    guardar_posts_fallidos(fallidos)
    print(
        f"  {YELLOW}[FALLIDOS] {len(candidatos)} post(s) sugerido(s) para skip — "
        f"revisar posts_fallidos.json antes de aplicar{RESET}"
    )


def imprimir_resumen_fallidos():
    """Muestra al final del lote cuántos posts fallidos hay acumulados."""
    fallidos = cargar_posts_fallidos()
    if not fallidos:
        return
    total = sum(len(v.get("skip", [])) for v in fallidos.values())
    urls = len(fallidos)
    print(
        f"  {YELLOW}⚠️  Posts fallidos detectados (acumulados): {total} en {urls} URL(s){RESET}"
    )
    print(
        f"  {GRAY}   Revisar posts_fallidos.json para aplicar manualmente a skip_posts.json{RESET}"
    )


def limpiar_fallidos_si_exito(url: str, res: dict):
    """Si la URL se descargó con éxito, la elimina de posts_fallidos.json."""
    if not res.get("ok"):
        return
    fallidos = cargar_posts_fallidos()
    url_key = url.rstrip("/")
    if url_key in fallidos:
        del fallidos[url_key]
        guardar_posts_fallidos(fallidos)
        print(
            f"  {GREEN}[LIMPIEZA] URL removida de posts_fallidos.json (descarga OK){RESET}"
        )


def obtener_timeout_por_url(url: str) -> int:
    """Devuelve timeout largo para sitios que suelen tardar mucho (Bunkr, Simpcity)."""
    u = url.lower()
    if any(x in u for x in ["bunkr", "bunkrr", "simpcity"]):
        return TIMEOUT_ACTIVIDAD_LENTO
    return TIMEOUT_ACTIVIDAD


# =============================================================================
# ESTADO
# =============================================================================


def cargar_estado(lista: list) -> dict:
    state_file = PATHS["state_file"]
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
# VIGILANTE DE .part
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


def descargar_windows(url: str, nombre_modelo: str, extra_args: list | None = None):
    cmd = [GDL_CFG["executable"], "-c", GDL_CFG["config_file"]]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

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
            args=(proceso, estado_watchdog, obtener_timeout_por_url(url)),
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
def descargar_linux(url: str, nombre_modelo: str, extra_args: list | None = None):
    cmd = [GDL_CFG["executable"], "-c", GDL_CFG["config_file"]]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    warnings_hilo = []
    contador_nuevo = 0
    contador_done = 0
    contador_warnings = 0
    contador_seq = 0
    ultima_ruta = ""
    estado_watchdog = {
        "stop": False,
        "ultimo_output": time.time(),
        "ultimo_archivo": time.time(),
        "timeout": False,
    }

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

        part_watch = threading.Thread(
            target=vigilar_part_thread,
            args=(estado_watchdog, PATHS["rips_dir"]),
            daemon=True,
        )
        part_watch.start()

        master_fd = None
        proceso = None
        buffer = ""
        estado_watchdog["ultimo_output"] = time.time()
        timeout_val = obtener_timeout_por_url(url)

        try:
            master_fd, slave_fd = pty.openpty()
            proceso = subprocess.Popen(
                cmd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env_vars
            )
            os.close(slave_fd)

            watch = threading.Thread(
                target=watchdog_thread,
                args=(proceso, estado_watchdog, obtener_timeout_por_url(url)),
                daemon=True,
            )
            watch.start()

            while True:
                r, _, _ = select.select([master_fd], [], [], 1.0)

                if not r:
                    ahora = time.time()
                    if ahora - estado_watchdog["ultimo_output"] > timeout_val:
                        try:
                            proceso.kill()
                        except OSError:
                            pass
                        estado_watchdog["timeout"] = True
                        break
                    if ahora - estado_watchdog["ultimo_archivo"] > TIMEOUT_SIN_ARCHIVOS:
                        try:
                            proceso.kill()
                        except OSError:
                            pass
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

                estado_watchdog["ultimo_output"] = time.time()
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
                                    estado_watchdog["ultimo_archivo"] = time.time()
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
                                estado_watchdog["ultimo_archivo"] = time.time()
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
                                estado_watchdog["ultimo_archivo"] = time.time()
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
                                estado_watchdog["ultimo_archivo"] = time.time()
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
            estado_watchdog["stop"] = True
            watch.join(timeout=2)
            part_watch.join(timeout=2)
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
            estado_watchdog["timeout"],
            int(time.time() - inicio),
            returncode,
        )
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# EJECUTOR DE URL
# =============================================================================
def ejecutar_url(url: str, skip_posts: dict | None = None) -> dict:
    nombre = re.sub(r'[\\/*?:"<>|]', "_", url.rstrip("/").split("/")[-1])[:60]
    log_path = PATHS["log_dir"] / f"{nombre}.log"
    PATHS["log_dir"].mkdir(parents=True, exist_ok=True)
    extra_args = []
    post_range = None
    rangos_skip = None

    if skip_posts:
        url_key = url.rstrip("/")
        post_range = skip_posts.get(url_key) or skip_posts.get(url_key + "/")
        if post_range:
            if isinstance(post_range, dict) and (
                "skip" in post_range or "omitir" in post_range
            ):
                rangos_skip = post_range.get("skip") or post_range.get("omitir", [])
                post_range = convertir_skip_a_range(post_range)
                if post_range:
                    extra_args.extend(["--post-range", post_range])
                    print(
                        f"  {YELLOW}[RANGE] Descargando posts: {post_range}  (Posts saltados: {', '.join(map(str, rangos_skip))}){RESET}"
                    )
            else:
                extra_args.extend(["--post-range", post_range])
                print(f"  {YELLOW}[RANGE] Descargando posts: {post_range}{RESET}")

    if IS_WINDOWS:
        archivos, errs, warns, nuevos, done, timeout, duracion, returncode = (
            descargar_windows(url, nombre, extra_args)
        )
    else:
        archivos, errs, warns, nuevos, done, timeout, duracion, returncode = (
            descargar_linux(url, nombre, extra_args)
        )
    # Deduplicar archivos (gallery-dl puede listar duplicados en stdout)
    archivos_unicos = list(dict.fromkeys(archivos))
    archivos = archivos_unicos
    nuevos = len(archivos)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nURL: {url}\n")
        if extra_args:
            f.write(f"Extra args: {' '.join(extra_args)}\n")
        if post_range:
            f.write(f"Post range: {post_range}\n")
        if rangos_skip:
            f.write(f"Posts omitidos: {', '.join(map(str, rangos_skip))}\n")
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
            f'duracion="{duracion}" returncode="{returncode}" timeout="{timeout}" '
            f'post_range="{post_range or "none"}"\n'
        )
        f.write(resumen_forense)
        f.write("=" * 60 + "\n\n")

    # Reportar posts candidatos a fallidos (no se aplica automáticamente)
    detectar_y_reportar_fallidos(
        url,
        {
            "nombre": nombre,
            "ok": not timeout and len(errs) == 0,
            "nuevos": nuevos,
            "done": done,
            "errores": len(errs),
            "warnings": len(warns),
            "timeout": timeout,
            "duracion": duracion,
            "returncode": returncode,
        },
        post_range,
        rangos_skip,
    )

    # Autolimpieza: si la descarga fue exitosa, sacar de posts_fallidos
    limpiar_fallidos_si_exito(
        url,
        {
            "nombre": nombre,
            "ok": not timeout and len(errs) == 0,
            "nuevos": nuevos,
            "done": done,
            "errores": len(errs),
            "warnings": len(warns),
            "timeout": timeout,
            "duracion": duracion,
            "returncode": returncode,
        },
    )

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
    skip_posts = cargar_skip_posts()

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

        res = ejecutar_url(url, skip_posts=skip_posts)
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
    imprimir_resumen_fallidos()


# =============================================================================
# MAIN
# =============================================================================
def main():
    import shutil

    CENTINELA = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "descarga.running"
    )

    def ya_esta_corriendo() -> bool:
        """Verifica si hay otra instancia viva usando el PID del centinela."""
        if not os.path.exists(CENTINELA):
            return False
        try:
            with open(CENTINELA, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            # os.kill(pid, 0) verifica si el proceso existe sin matarlo
            os.kill(pid, 0)
            return True
        except (ValueError, OSError, ProcessLookupError):
            # PID inválido o proceso muerto
            try:
                os.remove(CENTINELA)
            except OSError:
                pass
            return False

    if ya_esta_corriendo():
        print(
            f"\n  {RED}[X] descarga.py ya está en ejecución (PID en {CENTINELA}).{RESET}"
        )
        sys.exit(1)

    with open(CENTINELA, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

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
            time.sleep(1)
            try:
                subprocess.Popen(
                    ["wt", "-w", "0", "focus-pane", "-t", "0"],
                    shell=False,
                )
            except OSError:
                pass

    try:
        if not shutil.which(GDL_CFG["executable"]):
            print(
                f"\n  {RED}[X] Error: '{GDL_CFG['executable']}' no encontrado en PATH.{RESET}"
            )
            print("      Instala gallery-dl y asegúrate de que esté en el PATH.\n")
            if IS_WINDOWS:
                input(f"  {GRAY}Presiona Enter para salir...{RESET}")
            sys.exit(1)

        lista = [
            l.strip()
            for l in PATHS["lista_file"].read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")
        ]

        state = cargar_estado(lista)

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
