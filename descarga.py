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

# El .log de texto se renderiza de los mismos eventos que el .jsonl, usando el
# resumen y la clasificación de auditar. Importarlo en vez de recalcular acá es
# lo que garantiza que lo que se ve en pantalla, en el .log y en el CSV sea el
# mismo número. auditar es puro: no lee config.json ni toca disco al importarse.
import auditar

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

# Regex para capturar post_id desde logs de gallery-dl (parent-metadata)
# Ej: [bunkr][debug] post 3309088: Using archive...
RE_POST_ID = re.compile(r"\[([^\]]+)\]\[(?:debug|info|warning|error)\] post (\d+): ")
RE_LOG_LINE = re.compile(r"^\[[^\]]+\]\[(?:debug|info|warning|error)\]")
# Igual que RE_LOG_LINE pero capturando el nivel real, para no confundir
# ruido de "debug" (ej. "Sleeping X seconds") con warnings/errores genuinos.
RE_LOG_TAG = re.compile(r"^\[[^\]]+\]\[(debug|info|warning|error)\]")
# El módulo que emitió la línea ("download", "downloader.http", "bunkr"...).
# Va como campo `origen` de los eventos error/warning del .jsonl.
RE_LOG_ORIGEN = re.compile(r"^\[([^\]]+)\]\[(?:debug|info|warning|error)\]")
# Prefijo completo, para quedarse solo con el mensaje: el origen y el post_id
# ya viajan como campos propios del evento, repetirlos dentro del texto los
# volvería a mezclar.
# `post None:` aparece de verdad: los extractores que no son de XenForo no
# tienen keywords.post, y el format del .conf igual imprime el campo.
RE_LOG_PREFIJO = re.compile(
    r"^\[[^\]]+\]\[(?:debug|info|warning|error)\]\s*(?:post (?:\d+|None):\s*)?"
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
    # Traceback de formateo cuando keywords['post'] queda en None (ej. tras
    # un fallo de autenticación del extractor de twitter) — no es un fallo
    # de descarga real, es un artefacto del formato custom de log.
    "NoneType' object is not subscriptable",
]

TIMEOUT_ACTIVIDAD = 900
TIMEOUT_ACTIVIDAD_LENTO = 7200  # 2 horas para Bunkr / Simpcity
TIMEOUT_SIN_ARCHIVOS = 1800
TIMEOUT_SIN_ARCHIVOS_LENTO = 3600  # 1 hora para Bunkr / Simpcity
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
# EVENTOS  (.jsonl — contrato descarga.py -> auditar.py)
# =============================================================================
class EventLog:
    """Escribe el .jsonl de eventos y conserva la misma lista en memoria.

    Los eventos son el único registro de lo que pasó en una corrida. El .jsonl
    lo consume auditar.py; el .log de texto se renderiza de esta misma lista.
    Al salir las dos vistas de la misma fuente, no pueden contradecirse — que es
    justo lo que pasaba cuando auditar.py re-parseaba el texto plano.

    Dos detalles deliberados:

    - **El lock.** stdout lo lee el thread principal y stderr un thread
      dedicado; sin el lock las líneas se entrelazan y el .jsonl queda ilegible.
    - **El flush por evento.** Si el watchdog mata el proceso, el .jsonl queda
      truncado pero válido hasta donde llegó, y `auditar.resumir()` lo marca
      con `completo: False` en vez de perder la corrida entera.

    Se abre en modo "w": el .jsonl describe la última corrida de esa URL. Si un
    lote se cae antes de que auditar comprima los logs, la corrida siguiente lo
    reemplaza en vez de concatenarse (dos bloques inicio/fin en un mismo archivo
    sumarían los archivos de las dos).
    """

    def __init__(self, ruta):
        self.eventos = []
        self._lock = threading.Lock()
        self._f = open(ruta, "w", encoding="utf-8")

    def emitir(self, tipo: str, **campos):
        evento = {"t": tipo, **campos}
        linea = json.dumps(evento, ensure_ascii=False)
        with self._lock:
            self.eventos.append(evento)
            self._f.write(linea + "\n")
            self._f.flush()

    def cerrar(self):
        with self._lock:
            try:
                self._f.close()
            except OSError:
                pass


def partes_de_log(linea: str) -> tuple[str, int | None, str]:
    """Descompone una línea de log de gallery-dl en (origen, post_id, mensaje).

    El post_id sale de LA LÍNEA MISMA, nunca de una variable corriente: stdout y
    stderr son streams separados leídos por threads distintos y sin orden
    garantizado entre sí. Confiar en "el último post visto" es el bug que hacía
    que posts_fallidos.json acusara al post equivocado.
    """
    m_origen = RE_LOG_ORIGEN.match(linea)
    origen = m_origen.group(1) if m_origen else ""
    m_post = RE_POST_ID.search(linea)
    post_id = int(m_post.group(2)) if m_post else None
    return origen, post_id, RE_LOG_PREFIJO.sub("", linea).strip()


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
    limite_superior = max_skip + 999999

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


def cargar_posts_fallidos() -> dict:
    """Carga el reporte de posts fallidos sugeridos."""
    pf_path = PATHS["posts_fallidos_file"]
    if not pf_path.exists():
        return {}
    try:
        with open(pf_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def guardar_posts_fallidos(data: dict):
    """Guarda el reporte de posts fallidos."""
    pf_path = PATHS["posts_fallidos_file"]
    try:
        with open(pf_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"  {YELLOW}[!] No se pudo guardar posts_fallidos.json: {e}{RESET}")


def purgar_posts_fallidos(dias: int = 7, min_intentos: int = 3):
    """Elimina entradas antiguas de posts_fallidos.json.

    Reglas:
    - Si intentos < min_intentos Y fecha > dias -> eliminar.
    - Si intentos >= min_intentos -> conservar (fallo crónico).
    """
    pf_path = PATHS["posts_fallidos_file"]
    if not pf_path.exists():
        return

    fallidos = cargar_posts_fallidos()
    if not fallidos:
        return

    ahora = datetime.now()
    eliminados = 0

    for url_key in list(fallidos.keys()):
        entrada = fallidos[url_key]
        intentos = entrada.get("intentos", 0)
        fecha_str = entrada.get("fecha", "")

        # Conservar crónicos (muchas fallas)
        if intentos >= min_intentos:
            continue

        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d")
            dias_diff = (ahora - fecha).days
            if dias_diff >= dias:
                del fallidos[url_key]
                eliminados += 1
        except (ValueError, TypeError):
            # Fecha inválida, eliminar por seguridad
            del fallidos[url_key]
            eliminados += 1

    if eliminados:
        guardar_posts_fallidos(fallidos)
        print(
            f"  {GRAY}[PURGA] {eliminados} entrada(s) antigua(s) eliminada(s) "
            f"de posts_fallidos.json{RESET}"
        )


def extraer_dominio(url: str) -> str:
    """
    Extrae el dominio de una URL de hilo Simpcity/XenForo.
    Ej: 'https://simpcity.cr/threads/1709203' → 'simpcity.cr'
    """
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1) if match else "simpcity.cr"  # fallback conservador


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
    posts_con_error: list | None = None,
):
    """Detecta posts candidatos a fallidos y guarda en posts_fallidos.json como reporte.

    `posts_con_error` son los post_id que produjeron al menos un error, cada uno
    leído de su propia línea de log. Antes se recibía un único `post_id_activo`
    —el último post visto por cualquiera de los dos streams— que solía ser el
    equivocado: en una corrida real registró el post 51013156, que no había
    generado ni un error, mientras los 49 errores estaban en otros cuatro posts.

    El reporte entrega el post_id y el link clickeable. NO dice "copiar a
    skip_posts.json": ese archivo espera la POSICIÓN ORDINAL del post en el
    hilo (`--post-range` cuenta posiciones, no ids), y el post_id es apenas el
    primer paso — abrir el link y leer el `#N` que muestra XenForo.
    """
    fallidos = cargar_posts_fallidos()
    url_key = url.rstrip("/")
    resultado_thread = extraer_thread_id(url)
    if resultado_thread:
        thread_id, dominio = resultado_thread
    else:
        thread_id, dominio = None, extraer_dominio(url)

    # La razón sale del MISMO clasificador que llena el CSV: `res["estado"]` ya
    # es `auditar.clasificar(resumen)`. Antes acá se recalculaba a mano y se
    # escribía "fatal" cuando `errores > 0 and returncode != 0` — que es
    # exactamente el predicado de TRANSITORIO en auditar.py. La misma corrida
    # salía "fatal" en este JSON y TRANSITORIO en auditoria.csv; y leer "fatal"
    # invita a skipear posts que se bajan bien al reintentar.
    if res["timeout"]:
        # Único matiz que clasificar() no distingue, y no es un umbral
        # inventado: o alcanzó a bajar algo antes de morir, o no bajó nada.
        razon = "TIMEOUT_ATASCADO" if res["nuevos"] == 0 else "TIMEOUT_PARCIAL"
    elif res["errores"] > 0 and res["returncode"] != 0:
        razon = res["estado"]  # FATAL o TRANSITORIO, la misma palabra que el CSV
    else:
        return  # No reportar nada si está OK

    # -------------------------------------------------------------------------
    # CASO PRIORITARIO: post_id reales capturados de las líneas de error
    # -------------------------------------------------------------------------
    if posts_con_error:
        acumulados = set(fallidos.get(url_key, {}).get("posts_con_error", []))
        acumulados.update(posts_con_error)
        acumulados = sorted(acumulados)

        fallidos[url_key] = {
            "posts_con_error": acumulados,
            "post_urls": [
                f"https://{dominio}/threads/{thread_id}/post-{p}" for p in acumulados
            ]
            if thread_id
            else [],
            "razon": razon,
            "fecha": datetime.now().strftime("%Y-%m-%d"),
            "intentos": fallidos.get(url_key, {}).get("intentos", 0) + 1,
        }

        intentos = fallidos[url_key]["intentos"]
        if intentos >= 3:
            print(
                f"  {RED}[ALERTA CRÍTICA] URL con {intentos} fallos acumulados — "
                f"revisar manualmente{RESET}"
            )

        guardar_posts_fallidos(fallidos)
        print(
            f"  {YELLOW}[FALLIDOS] {len(posts_con_error)} post(s) con errores: "
            f"{', '.join(map(str, posts_con_error))}{RESET}"
        )
        return

    # -------------------------------------------------------------------------
    # CASO A: Sin post_range pero hay fallo -> reportar con marcador "revisar"
    # -------------------------------------------------------------------------
    posts_intentados = extraer_posts_desde_range(post_range)
    if not posts_intentados:
        # Sin `if razon in (...)`: llegar hasta acá ya implica que la corrida no
        # fue OK — el bloque de arriba retorna en ese caso.
        if url_key not in fallidos:
            fallidos[url_key] = {
                "posts_con_error": [],
                "razon": razon,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "intentos": 0,
                "nota": "Falló sin errores atribuibles a un post. Revisar el log.",
            }
        else:
            fallidos[url_key]["razon"] = razon
            fallidos[url_key]["fecha"] = datetime.now().strftime("%Y-%m-%d")
            fallidos[url_key]["intentos"] = fallidos[url_key].get("intentos", 0) + 1

        guardar_posts_fallidos(fallidos)
        print(
            f"  {YELLOW}[FALLIDOS] URL marcada para revision manual — "
            f"revisar posts_fallidos.json{RESET}"
        )
        return

    # -------------------------------------------------------------------------
    # CASO B: con post_range y sin errores atribuibles, solo queda registrar qué
    # se intentó. OJO CON LA UNIDAD: esto son POSICIONES ORDINALES sacadas del
    # --post-range, no post_id de XenForo. Por eso va en un campo aparte: son la
    # misma unidad que skip_posts.json y la contraria a `posts_con_error`, y
    # mezclarlas es justo la confusión que este rewrite viene a deshacer.
    # -------------------------------------------------------------------------
    ya_skipeados = set()
    if rangos_skip_previos:
        ya_skipeados.update(rangos_skip_previos)

    candidatos = sorted(posts_intentados - ya_skipeados)
    if not candidatos:
        return

    entrada = fallidos.setdefault(
        url_key,
        {
            "ordinales_intentados": [],
            "razon": razon,
            "fecha": datetime.now().strftime("%Y-%m-%d"),
            "intentos": 0,
        },
    )
    entrada["ordinales_intentados"] = sorted(
        set(entrada.get("ordinales_intentados", [])) | set(candidatos)
    )
    entrada["razon"] = razon
    entrada["fecha"] = datetime.now().strftime("%Y-%m-%d")
    entrada["intentos"] = entrada.get("intentos", 0) + 1

    guardar_posts_fallidos(fallidos)
    print(
        f"  {YELLOW}[FALLIDOS] falló con {len(candidatos)} post(s) en rango, "
        f"sin error atribuible a uno solo — revisar posts_fallidos.json{RESET}"
    )


def imprimir_resumen_fallidos():
    """Muestra al final del lote cuántos posts fallidos hay acumulados."""
    fallidos = cargar_posts_fallidos()
    if not fallidos:
        return
    total = sum(len(v.get("posts_con_error", [])) for v in fallidos.values())
    urls = len(fallidos)
    print(
        f"  {YELLOW}⚠️  Posts con errores (acumulados): {total} en {urls} URL(s){RESET}"
    )
    # Lo que va en skip_posts.json es la POSICIÓN del post en el hilo, no su id.
    # El link del reporte es el primer paso: abrirlo y leer el "#N" de XenForo.
    print(
        f"  {GRAY}   Revisar posts_fallidos.json: abrir el post_url y anotar en "
        f"skip_posts.json el #N que muestra XenForo{RESET}"
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


def es_host_lento(url: str) -> bool:
    """Bunkr / Simpcity: sitios que suelen tardar mucho o tener rate-limit agresivo."""
    u = url.lower()
    return any(x in u for x in ["bunkr", "bunkrr", "simpcity"])


def obtener_timeout_por_url(url: str) -> int:
    """Devuelve timeout largo (sin output) para sitios lentos."""
    return TIMEOUT_ACTIVIDAD_LENTO if es_host_lento(url) else TIMEOUT_ACTIVIDAD


def obtener_timeout_sin_archivos_por_url(url: str) -> int:
    """Igual que obtener_timeout_por_url, pero para el reloj de archivos nuevos."""
    return TIMEOUT_SIN_ARCHIVOS_LENTO if es_host_lento(url) else TIMEOUT_SIN_ARCHIVOS


def extraer_thread_id(url: str) -> tuple[str, str] | None:
    """
    Ahora devuelve (thread_id, dominio) para propagar el dominio real.
    """
    match = re.search(r"/threads/[^.]+\.(\d+)", url)
    if match:
        thread_id = match.group(1)
        dominio = extraer_dominio(url)
        return thread_id, dominio
    return None


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
    """Write-ahead atómico: nunca deja state.json a medias.

    Patrón WAL (Write-Ahead Log):
      1. Escribe a archivo temporal (.tmp)
      2. fsync() fuerza volcado a disco físico
      3. os.replace() atómico en NTFS/ext4

    Si el proceso muere en el paso 1 o 2, state.json original
    permanece intacto. Si muere en el paso 3, NTFS journal
    completa o revierte la operación al arrancar.
    """
    state_path = PATHS["state_file"]
    tmp_path = state_path.with_suffix(".json.tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())  # Fuerza escritura del buffer del SO al disco

    # replace() es atómico: nunca existe un state.json truncado visible
    os.replace(tmp_path, state_path)


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


def clasificar_por_tag(linea: str) -> tuple[bool, bool]:
    """Clasifica una línea como (es_warning, es_error).

    Si la línea trae un tag explícito de gallery-dl ([...][debug|info|warning|error]),
    se confía en ese nivel real en vez de adivinar por keywords — así el ruido de
    debug (ej. "post 13501: Sleeping 1.00 seconds") usado solo para capturar el
    post_id no se cuenta como warning solo porque contiene la palabra "sleeping".
    Si no hay tag explícito, se usa el heurístico de keywords como antes.
    """
    m_tag = RE_LOG_TAG.match(linea)
    if m_tag:
        nivel = m_tag.group(1)
        if nivel == "warning":
            return True, False
        if nivel == "error":
            return False, True
        # debug / info: ruido informativo, no cuenta como warning ni error
        return False, False
    es_warn = es_linea_warning(linea)
    es_err = not es_warn and es_linea_error(linea)
    return es_warn, es_err


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
def watchdog_thread(proceso, estado, timeout, timeout_sin_archivos):
    while not estado["stop"]:
        ahora = time.time()
        if ahora - estado["ultimo_output"] > timeout:
            try:
                proceso.kill()
            except OSError:
                pass
            estado["timeout"] = True
            break
        if ahora - estado["ultimo_archivo"] > timeout_sin_archivos:
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


def descargar_windows(
    url: str,
    nombre_modelo: str,
    extra_args: list | None = None,
    eventos: "EventLog" = None,
):
    cmd = [
        GDL_CFG["executable"],
        "-c",
        GDL_CFG["config_file"],
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    warnings_hilo = []
    # Posts que produjeron al menos un error, cada uno leído de su propia línea.
    # Reemplaza al viejo `post_id_activo`, que era el último post visto por
    # cualquiera de los dos streams y por eso acusaba al post equivocado.
    posts_con_error = set()
    warnings_pendientes = {}
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

                es_warn, es_err = clasificar_por_tag(linea_strip)

                if not es_warn and not es_err:
                    continue

                # Origen, post_id y mensaje salen de ESTA línea, no de una
                # variable compartida entre threads.
                origen, post_id, mensaje = partes_de_log(linea_strip)

                # -----------------------------------------------------------------
                # WARNING: guardar temporalmente, no mostrar todavía
                # -----------------------------------------------------------------
                if es_warn:
                    warnings_hilo.append(linea_strip)
                    eventos.emitir(
                        "warning", post_id=post_id, origen=origen, msg=mensaje
                    )
                    # Solo retenemos warnings del downloader HTTP.
                    if "[downloader.http][warning]" in linea_strip:
                        warnings_pendientes[post_id] = linea_strip
                        continue

                # -----------------------------------------------------------------
                # ERROR: si existe warning HTTP previo del mismo post, incorporarlo
                # -----------------------------------------------------------------
                with lock_print:
                    estado_watchdog["ultimo_output"] = time.time()
                    contador["seq"] += 1
                    clear_line()

                    if es_err:
                        texto_error = limpiar_error(linea_strip)

                        # El error de gallery-dl no dice POR QUÉ falló: la línea
                        # es "Failed to download X.jpg" y la causa real
                        # ("Read timed out", "HTML response", un 404) viaja en el
                        # warning previo del downloader. Por eso el evento lleva
                        # el texto fusionado: es lo único que auditar.es_fatal()
                        # puede clasificar.
                        warning_previo = warnings_pendientes.pop(post_id, None)

                        if warning_previo:
                            causa = limpiar_error(warning_previo)
                            texto_error = f"{texto_error} — {causa}"
                            _, _, causa_msg = partes_de_log(warning_previo)
                            mensaje = f"{mensaje} — {causa_msg}"

                        errores_hilo.append(linea_strip)
                        if post_id is not None:
                            posts_con_error.add(post_id)
                        eventos.emitir(
                            "error", post_id=post_id, origen=origen, msg=mensaje
                        )

                        sys.stdout.write(
                            f"  {RED}[{contador['seq']:>3}] [X] {texto_error}{RESET}\n"
                        )

                    elif es_warn:
                        # Warning que NO es downloader.http:
                        # se comporta como antes y sí se muestra.
                        sys.stdout.write(
                            f"  {YELLOW}[{contador['seq']:>3}] [!] "
                            f"{limpiar_error(linea_strip)}{RESET}\n"
                        )

                    sys.stdout.flush()

        spin = threading.Thread(
            target=spinner_thread,
            args=(estado_spinner, lock_print),
            daemon=True,
        )

        watch = threading.Thread(
            target=watchdog_thread,
            args=(
                proceso,
                estado_watchdog,
                obtener_timeout_por_url(url),
                obtener_timeout_sin_archivos_por_url(url),
            ),
            daemon=True,
        )

        stderr_thr = threading.Thread(
            target=leer_stderr,
            daemon=True,
        )

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

                # Limpiar ANSI para filtrado robusto de logs (v3.2)
                linea_pura = ANSI_ESCAPE.sub("", linea_strip).strip()

                # Filtrar líneas de log puro para que no cuenten como archivos
                if RE_LOG_LINE.search(linea_pura):
                    continue

                with lock_print:
                    estado_watchdog["ultimo_output"] = time.time()
                    estado_watchdog["ultimo_archivo"] = time.time()
                    contador["seq"] += 1

                    if linea_pura.startswith("#"):
                        estado_spinner["done"] += 1
                        ruta = linea_pura[1:].strip()
                        # El "#" es el marcador de gallery-dl para "esta ruta ya
                        # estaba en archive.db": archivo conocido, no descargado.
                        eventos.emitir("archivo", path=ruta, nuevo=False)
                        clear_line()
                        sys.stdout.write(
                            f"  {GRAY}[{contador['seq']:>3}] [DONE] {nombre_modelo} - {nombre_visible(ruta)}{RESET}\n"
                        )
                        sys.stdout.flush()
                        estado_spinner["ultima_linea"] = ""
                    else:
                        estado_spinner["nuevo"] += 1
                        archivos_nuevos.append(linea_pura)
                        eventos.emitir("archivo", path=linea_pura, nuevo=True)
                        clear_line()
                        sys.stdout.write(
                            f"  {GREEN}[{contador['seq']:>3}] {nombre_modelo} - {nombre_visible(linea_pura)}{RESET}\n"
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
            sorted(posts_con_error),
        )
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# MOTOR LINUX
# =============================================================================
def descargar_linux(url: str, nombre_modelo: str, extra_args: list | None = None):
    cmd = [
        GDL_CFG["executable"],
        "-c",
        GDL_CFG["config_file"],
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    inicio = time.time()
    archivos_nuevos = []
    errores_hilo = []
    warnings_hilo = []
    post_id_activo = None
    warnings_pendientes = {}
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
        timeout_sin_archivos_val = obtener_timeout_sin_archivos_por_url(url)

        try:
            master_fd, slave_fd = pty.openpty()
            proceso = subprocess.Popen(
                cmd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env_vars
            )
            os.close(slave_fd)

            watch = threading.Thread(
                target=watchdog_thread,
                args=(
                    proceso,
                    estado_watchdog,
                    timeout_val,
                    timeout_sin_archivos_val,
                ),
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
                    if (
                        ahora - estado_watchdog["ultimo_archivo"]
                        > timeout_sin_archivos_val
                    ):
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
                        linea_pura = ANSI_ESCAPE.sub("", linea_limpia).strip()

                        if es_done:
                            es_warning = False
                            es_error = False
                        else:
                            es_warning, es_error = clasificar_por_tag(linea_pura)
                            if not es_warning and not es_error:
                                es_error = (
                                    "connection broken" in linea_pura.lower()
                                    or "incompleteread" in linea_pura.lower()
                                )

                        if not linea_pura or linea_pura == ultima_ruta:
                            continue
                        ultima_ruta = linea_pura

                        # Capturar post_id real desde logs de gallery-dl
                        m = RE_POST_ID.search(linea_pura)
                        if m:
                            post_id_activo = int(m.group(2))

                        # Si es una línea de log puro (no warning/error/done), ignorar
                        if (
                            RE_LOG_LINE.search(linea_pura)
                            and not es_done
                            and not es_warning
                            and not es_error
                        ):
                            continue

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

                        with lock_print:
                            estado_spinner["pct"] = -1
                            estado_spinner["descargado"] = ""
                            estado_spinner["total"] = ""
                            estado_spinner["speed"] = ""
                            estado_spinner["ultima_linea"] = ""

                            if es_done:
                                contador_seq += 1
                                contador_done += 1
                                estado_watchdog["ultimo_archivo"] = time.time()
                                estado_spinner["done"] = contador_done
                                clear_line()
                                sys.stdout.write(
                                    f"  {DIM}[{contador_seq:>3}] [DONE] {nombre_visible_str}{RESET}\n"
                                )
                                sys.stdout.flush()
                            elif es_warning:
                                contador_warnings += 1
                                warnings_hilo.append(linea_pura)

                                # Igual que en el motor Windows: los warnings
                                # de downloader.http se retienen sin mostrar,
                                # por si preceden a un error del mismo post
                                # (se fusionan en una sola línea más informativa).
                                if "[downloader.http][warning]" in linea_pura:
                                    warnings_pendientes[post_id_activo] = linea_pura
                                else:
                                    # Warning que no es downloader.http: se
                                    # muestra igual, como antes.
                                    contador_seq += 1
                                    clear_line()
                                    sys.stdout.write(
                                        f"  {YELLOW}[{contador_seq:>3}] [!] {limpiar_error(linea_pura)}{RESET}\n"
                                    )
                                    sys.stdout.flush()
                            elif es_error:
                                errores_hilo.append(linea_pura)
                                contador_seq += 1
                                clear_line()

                                texto_error = limpiar_error(linea_pura)
                                warning_previo = warnings_pendientes.pop(
                                    post_id_activo, None
                                )
                                if warning_previo:
                                    causa = limpiar_error(warning_previo)
                                    texto_error = f"{texto_error} — {causa}"

                                sys.stdout.write(
                                    f"  {RED}[{contador_seq:>3}] [X] {texto_error}{RESET}\n"
                                )
                                sys.stdout.flush()
                            else:
                                contador_seq += 1
                                contador_nuevo += 1
                                estado_watchdog["ultimo_archivo"] = time.time()
                                archivos_nuevos.append(linea_pura)
                                estado_spinner["nuevo"] = contador_nuevo
                                clear_line()
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
            post_id_activo,
        )
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# LOG DE TEXTO  (vista humana de los mismos eventos)
# =============================================================================
SEPARADOR = "═" * 60


def escribir_log_texto(
    ruta,
    url: str,
    resumen: dict,
    eventos: list,
    post_range: str | None = None,
    rangos_skip: list | None = None,
):
    """Renderiza el .log legible a partir de los mismos eventos que el .jsonl.

    El .log dejó de ser un contrato. Antes lo era vía la línea
    `[RESUMEN] clave="valor"`, que auditar.py parseaba con regex; ahora el
    contrato es el evento `fin` del .jsonl y este archivo es solo una vista.
    Cambiar el formato de acá no rompe la auditoría.

    Los errores van agrupados por post porque esa es la unidad accionable: lo
    que se skipea en XenForo es un post, no un archivo suelto.
    """
    resultado = extraer_thread_id(url)
    thread_id, dominio = resultado if resultado else (None, extraer_dominio(url))

    # Agrupar los mensajes por post, en el orden en que ocurrieron.
    errores_por_post = {}
    warnings_por_post = {}
    for e in eventos:
        if e.get("t") == "error":
            errores_por_post.setdefault(e.get("post_id"), []).append(e.get("msg", ""))
        elif e.get("t") == "warning":
            warnings_por_post.setdefault(e.get("post_id"), []).append(e.get("msg", ""))

    nuevos = [e["path"] for e in eventos if e.get("t") == "archivo" and e.get("nuevo")]

    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"{SEPARADOR}\n")
        f.write(
            f" {resumen['nombre_modelo']}"
            f"  ·  {resumen['ts'].replace('T', ' ')}"
            f"  ·  {formatear_tiempo(resumen['duracion'])}\n"
        )
        f.write(f" {url}\n")
        f.write(f"{SEPARADOR}\n\n")

        f.write(
            f"Resumen: {resumen['nuevos']} nuevos, {resumen['ya']} ya descargados, "
            f"{resumen['errores']} errores, {resumen['warnings']} warnings\n"
        )
        f.write(
            f"Estado:  {auditar.clasificar(resumen)} "
            f"(returncode {resumen['returncode']})\n"
        )
        if not resumen["completo"]:
            f.write(
                "         [!] Sin evento de cierre: el proceso murió antes de "
                "terminar.\n"
            )
        if post_range:
            f.write(f"Post range: {post_range}\n")
        if rangos_skip:
            f.write(f"Posts omitidos: {', '.join(map(str, rangos_skip))}\n")

        def escribir_grupo(titulo, grupos):
            f.write(f"\n{titulo}\n")
            for post_id, msgs in grupos.items():
                etiqueta = f"post {post_id}" if post_id is not None else "sin post"
                link = (
                    f"   https://{dominio}/threads/{thread_id}/post-{post_id}"
                    if thread_id and post_id is not None
                    else ""
                )
                f.write(f"  {etiqueta}  ({len(msgs)}){link}\n")
                f.write(f"       {msgs[0]}\n")
                if len(msgs) > 1:
                    f.write(f"       (+{len(msgs) - 1} más)\n")

        if errores_por_post:
            escribir_grupo("ERRORES POR POST", errores_por_post)
        else:
            f.write("\nSin errores.\n")

        # Warnings SIN un error del mismo post. Los que sí lo tienen ya se leen
        # arriba: descarga.py fusiona la causa del warning dentro del mensaje
        # del error. Estos no aparecían en ninguna parte —solo como número en
        # el Resumen— y suelen decir algo que importa, como el
        # "Unable to access premium content (type: paid)" de deviantart.
        sueltos = {
            pid: msgs
            for pid, msgs in warnings_por_post.items()
            if pid not in errores_por_post
        }
        if sueltos:
            total = sum(len(m) for m in sueltos.values())
            escribir_grupo(f"WARNINGS SIN ERROR ({total})", sueltos)

        if nuevos:
            f.write(f"\nARCHIVOS ({len(nuevos)})\n")
            for path in nuevos:
                f.write(f"  {path}\n")


# =============================================================================
# EJECUTOR DE URL
# =============================================================================
def ejecutar_url(url: str, skip_posts: dict | None = None) -> dict:
    nombre = re.sub(r'[\\/*?:"<>|]', "_", url.rstrip("/").split("/")[-1])[:60]
    log_path = PATHS["log_dir"] / f"{nombre}.log"
    jsonl_path = PATHS["log_dir"] / f"{nombre}.jsonl"
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

    eventos = EventLog(jsonl_path)
    eventos.emitir(
        "inicio",
        url=url,
        # El nombre se calcula UNA vez, acá, y viaja dentro del evento.
        # auditar lo lee en vez de re-derivarlo de la URL: si esta regla de
        # nombrado cambia, el CSV la sigue sin que haya que tocar dos archivos.
        nombre=nombre,
        ts=datetime.now().isoformat(timespec="seconds"),
        post_range=post_range,
    )

    try:
        if IS_WINDOWS:
            (
                archivos,
                errs,
                warns,
                _nuevos,
                _done,
                timeout,
                duracion,
                returncode,
                posts_con_error,
            ) = descargar_windows(url, nombre, extra_args, eventos=eventos)
        else:
            # Rama congelada (ver CLAUDE.md): descargar_linux no se instrumenta,
            # así que su .jsonl sale con inicio y fin pero sin eventos `archivo`.
            (
                archivos,
                errs,
                warns,
                _nuevos,
                _done,
                timeout,
                duracion,
                returncode,
                post_id_activo,
            ) = descargar_linux(url, nombre, extra_args)
            posts_con_error = [post_id_activo] if post_id_activo else []

        eventos.emitir(
            "fin",
            duracion=duracion,
            returncode=returncode,
            timeout=timeout,
        )
    finally:
        eventos.cerrar()

    # Una sola fuente para los totales: los mismos eventos que quedaron en el
    # .jsonl. Lo que se imprime en pantalla, lo que dice el .log y lo que va al
    # CSV salen todos de acá, así que no pueden discrepar.
    resumen = auditar.resumir(eventos.eventos)
    escribir_log_texto(log_path, url, resumen, eventos.eventos, post_range, rangos_skip)

    res = {
        "nombre": resumen["nombre_modelo"],
        "ok": not timeout and resumen["errores"] == 0,
        "nuevos": resumen["nuevos"],
        "done": resumen["ya"],
        "errores": resumen["errores"],
        "warnings": resumen["warnings"],
        "timeout": timeout,
        "duracion": duracion,
        "returncode": returncode,
        "estado": auditar.clasificar(resumen),
        "archivos": archivos,
        "errs": errs,
        "warns": warns,
    }

    # Reportar posts candidatos a fallidos (no se aplica automáticamente)
    detectar_y_reportar_fallidos(url, res, post_range, rangos_skip, posts_con_error)

    # Autolimpieza: si la descarga fue exitosa, sacar de posts_fallidos
    limpiar_fallidos_si_exito(url, res)

    return res


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

        try:
            res = ejecutar_url(url, skip_posts=skip_posts)
        except Exception as e:
            nombre_err = url.rstrip("/").split("/")[-1][:60]
            print(
                f"\n  {RED}[X] Excepcion no controlada procesando {nombre_err}: {e}{RESET}"
            )
            print(
                f"  {DIM}(se registra como fallida y el lote continua con la siguiente URL){RESET}"
            )
            # Una excepción acá también tiene que quedar auditable: se emite un
            # .jsonl mínimo (inicio + el error + fin con returncode -1) y su
            # .log correspondiente. Si no, la URL desaparece del CSV como si
            # nunca se hubiera intentado.
            try:
                PATHS["log_dir"].mkdir(parents=True, exist_ok=True)
                eventos_err = EventLog(PATHS["log_dir"] / f"{nombre_err}.jsonl")
                eventos_err.emitir(
                    "inicio",
                    url=url,
                    nombre=nombre_err,
                    ts=datetime.now().isoformat(timespec="seconds"),
                    post_range=None,
                )
                eventos_err.emitir(
                    "error", post_id=None, origen="descarga", msg=f"Excepción: {e}"
                )
                eventos_err.emitir(
                    "fin", duracion=0, returncode=-1, timeout=False
                )
                eventos_err.cerrar()
                escribir_log_texto(
                    PATHS["log_dir"] / f"{nombre_err}.log",
                    url,
                    auditar.resumir(eventos_err.eventos),
                    eventos_err.eventos,
                )
            except OSError:
                pass
            res = {
                "nombre": nombre_err,
                "nuevos": 0,
                "done": 0,
                "errores": 1,
                "warnings": 0,
                "timeout": False,
                "duracion": 0,
                "returncode": -1,
            }
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
def _pid_vivo(pid: int) -> bool:
    """Verifica si un PID corresponde a un proceso vivo.

    En Windows, os.kill(pid, 0) NO es un probe de vida: signal.CTRL_C_EVENT
    vale 0, asi que CPython lo enruta a GenerateConsoleCtrlEvent, que
    devuelve éxito incluso para un PID ya muerto (falso positivo permanente).
    Por eso acá se consulta al SO directamente vía OpenProcess.
    """
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            codigo_salida = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo_salida)):
                return False
            return codigo_salida.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


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
            if _pid_vivo(pid):
                return True
        except (ValueError, OSError):
            pass
        # PID inválido o proceso muerto: centinela huérfano
        try:
            os.remove(CENTINELA)
        except OSError:
            pass
        return False

    def iniciar_heartbeat(intervalo: int = 60) -> threading.Event:
        """Refresca el mtime de CENTINELA cada `intervalo` segundos.

        Sin esto, el mtime queda congelado desde el arranque del proceso.
        El chequeo de stale en monitor.py (2h) terminaba disparando por
        simple paso del tiempo -- no por inactividad real -- en lotes
        largos o con hilos de bunkr/simpcity que ya de por si pueden
        tardar hasta 2h. Con el heartbeat, el monitor solo se cierra por
        stale si descarga.py de verdad dejo de responder.
        """
        detener = threading.Event()

        def _tick():
            while not detener.is_set():
                try:
                    os.utime(CENTINELA, None)
                except OSError:
                    pass
                detener.wait(intervalo)

        threading.Thread(target=_tick, daemon=True).start()
        return detener

    if ya_esta_corriendo():
        print(
            f"\n  {RED}[X] descarga.py ya está en ejecución (PID en {CENTINELA}).{RESET}"
        )
        sys.exit(1)

    with open(CENTINELA, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    heartbeat_stop = iniciar_heartbeat()

    # Purgar posts fallidos antiguos al inicio de cada ejecución
    purgar_posts_fallidos()

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
        heartbeat_stop.set()
        if os.path.exists(CENTINELA):
            os.remove(CENTINELA)


if __name__ == "__main__":
    main()
