#!/usr/bin/env python3
"""
auditar.py — Auditoría forense de logs generados por descarga.py
Modelo: append-only CSV + compresión de logs + detección de .part huérfanos
"""

import csv
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime

# =============================================================================
# ANSI
# =============================================================================
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[90m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# =============================================================================
# CONFIGURACIÓN — mismo schema que descarga.py
# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def cargar_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"[X]  {RED}No se encontró config.json en: {CONFIG_PATH}{RESET}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    entorno = "windows" if sys.platform == "win32" else "linux"
    cfg = raw[entorno]

    # Paths resueltos
    paths = {k: os.path.expanduser(v) for k, v in cfg["paths"].items()}
    return {
        "log_dir": paths["log_dir"],
        "rips_dir": paths["rips_dir"],
        "audit_csv": paths["audit_csv"],
    }


CFG = cargar_config()
LOG_DIR = CFG["log_dir"]
RIPS_DIR = CFG["rips_dir"]
AUDIT_CSV = CFG["audit_csv"]

# =============================================================================
# REGEX
# =============================================================================

# Línea [RESUMEN] escrita por descarga.py — pares clave="valor"
RE_RESUMEN = re.compile(r"\[RESUMEN\](.+)", re.IGNORECASE)
RE_KV = re.compile(r'(\w+)="([^"]*)"')
RE_POSTS_OMITIDOS = re.compile(r"Post[s]? omitidos: (.+)", re.IGNORECASE)

# Errores en el cuerpo del log (stdout/stderr de gallery-dl)
RE_ERR = re.compile(
    r"(error|HttpError|urllib\.error|ConnectionError|ReadTimeout|"
    r"404|410|403|unable to extract|failed to parse|thread deleted|"
    r"invalid thread|unsupported url)",
    re.IGNORECASE,
)

KEYWORDS_RUIDO = [
    "theme-light",
    "color-",
    "--rem",
    "None_",
    "logging",
    "extracted",
    "cookies from",
]

FATAL_PATTERNS = [
    re.compile(r"404", re.IGNORECASE),
    re.compile(r"410", re.IGNORECASE),
    re.compile(r"thread.*deleted", re.IGNORECASE),
    re.compile(r"invalid.*thread", re.IGNORECASE),
    re.compile(r"unsupported.*url", re.IGNORECASE),
    re.compile(r"unable to extract", re.IGNORECASE),
    re.compile(r"failed to parse", re.IGNORECASE),
]

# =============================================================================
# CLASIFICACIÓN
# =============================================================================


def clasificar_error(linea: str) -> str:
    for pattern in FATAL_PATTERNS:
        if pattern.search(linea):
            return "FATAL"
    return "TRANSITORIO"


def determinar_estado(
    returncode: int,
    fatales: int,
    transitorios: int,
    timeout: bool = False,
    nuevos: int = 0,
    errores: int = 0,
) -> str:
    """
    Evolución v2.0: Clasificación inteligente de estados.
    Prioridad: TIMEOUT (Diferenciado) > FATAL > TRANSITORIO > OK
    """
    if timeout:
        # Si hubo progreso real y la tasa de error es baja (< 20% del contenido nuevo)
        if nuevos > 0 and errores < (nuevos * 0.20):
            return "TIMEOUT_SANANDO"  # Hilo masivo en progreso saludable
        else:
            return "TIMEOUT_ATASCADO"  # Atascado por hosts caídos (ej. TurboCDN)

    if fatales > 0:
        return "FATAL"
    if transitorios > 0:
        return "TRANSITORIO"
    if returncode != 0:
        return "TRANSITORIO"
    return "OK"


# =============================================================================
# CSV — append-only
# =============================================================================

CSV_HEADER = [
    "Fecha",
    "Nombre_Modelo",
    "URL",
    "Post_omitidos",
    "Nuevos",
    "Ya_descargados",
    "Errores",
    "Errores_detalle",
    "Duracion_s",
    "Returncode",
    "Timeout",
    "Estado",
]


def registrar_filas_en_csv(filas: list):
    """Escribe todas las filas del lote en una sola apertura del CSV."""
    if not filas:
        return
    os.makedirs(os.path.dirname(AUDIT_CSV), exist_ok=True)
    for _ in range(5):
        try:
            with open(AUDIT_CSV, "a+", newline="", encoding="utf-8") as f:
                f.seek(0)
                es_nuevo = not f.read(4)
                if es_nuevo:
                    f.write("sep=;\n")
                    csv.writer(f, delimiter=";").writerow(CSV_HEADER)
                f.seek(0, os.SEEK_END)
                w = csv.writer(f, delimiter=";")
                for fila in filas:
                    w.writerow(fila)
                f.flush()
                os.fsync(f.fileno())
            return
        except OSError:
            time.sleep(0.1)


# =============================================================================
# LOGS — compresión y purga
# =============================================================================


def archivar_logs_en_zip(rutas: list):
    if not rutas:
        return
    zip_file = os.path.join(LOG_DIR, f"logs_{datetime.now().strftime('%Y-%m-%d')}.zip")
    try:
        with zipfile.ZipFile(zip_file, "a", compression=zipfile.ZIP_DEFLATED) as zipf:
            for ruta in rutas:
                if os.path.exists(ruta):
                    ts = datetime.now().strftime("%H%M%S")
                    zipf.write(ruta, arcname=f"{ts}_{os.path.basename(ruta)}")
        for ruta in rutas:
            try:
                os.remove(ruta)
            except OSError:
                pass
    except Exception as e:
        print(f"  {RED}[X] Error comprimiendo logs: {e}{RESET}")


def purgar_zip_antiguos(dias: int = 60):
    if not os.path.exists(LOG_DIR):
        return
    ahora = datetime.now()
    purgados = 0
    for archivo in os.listdir(LOG_DIR):
        m = re.match(r"logs_(\d{4}-\d{2}-\d{2})\.zip", archivo)
        if not m:
            continue
        try:
            fecha = datetime.strptime(m.group(1), "%Y-%m-%d")
            if (ahora - fecha).days > dias:
                os.remove(os.path.join(LOG_DIR, archivo))
                purgados += 1
        except Exception:
            pass
    if purgados:
        print(
            f"  {GRAY}[MANTENIMIENTO] {purgados} ZIP(s) eliminados (+{dias} días).{RESET}"
        )


# =============================================================================
# ARCHIVOS .part HUÉRFANOS
# =============================================================================


def buscar_part_huerfanos() -> list:
    encontrados = []
    if not os.path.exists(RIPS_DIR):
        return encontrados
    for raiz, _, archivos in os.walk(RIPS_DIR):
        if "logs" in raiz.lower():
            continue
        for archivo in archivos:
            if archivo.endswith(".part"):
                encontrados.append(os.path.join(raiz, archivo))
    return encontrados


# =============================================================================
# MOTOR PRINCIPAL
# =============================================================================


def analizar_logs():
    if not os.path.exists(LOG_DIR):
        print(f"\n  {RED}[X] Directorio de logs no existe: {LOG_DIR}{RESET}\n")
        return

    logs = [
        f for f in os.listdir(LOG_DIR) if f.endswith(".log") and f != "procesados.log"
    ]
    huerfanos = buscar_part_huerfanos()

    if not logs and not huerfanos:
        print(f"\n  {GREEN}[+] Sin logs nuevos ni archivos .part huérfanos.{RESET}\n")
        return

    ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logs_a_comprimir = []
    filas_csv = []

    conteo = {
        "ok": 0,
        "transitorio": 0,
        "fatal": 0,
        "timeout_sanando": 0,
        "timeout_atascado": 0,
        "timeout": 0,
        "sin_resumen": 0,
    }
    for archivo in sorted(
        logs, key=lambda f: os.path.getmtime(os.path.join(LOG_DIR, f))
    ):
        ruta = os.path.join(LOG_DIR, archivo)
        try:
            # ── Streaming: un solo paso por el log ───────────────────────────
            resumen = {}
            posts_omitidos = ""
            errores_detalle = []
            en_seccion_errores = False
            fatales = transitorios = 0

            with open(ruta, "r", encoding="utf-8", errors="replace") as f:
                for linea in f:
                    ls = linea.strip()

                    # [RESUMEN]
                    if not resumen:
                        m = RE_RESUMEN.search(linea)
                        if m:
                            resumen = dict(RE_KV.findall(m.group(1)))

                    # Posts omitidos
                    if not posts_omitidos:
                        m = RE_POSTS_OMITIDOS.search(linea)
                        if m:
                            posts_omitidos = m.group(1).strip()

                    # Errores detalle
                    if ls == "ERRORES:":
                        en_seccion_errores = True
                        continue
                    if en_seccion_errores:
                        if (
                            not ls
                            or ls in (
                                "WARNINGS:",
                                "[RESUMEN]",
                                "Sin errores.",
                                "Sin warnings.",
                            )
                            or ls.startswith("=")
                        ):
                            en_seccion_errores = False
                        elif ls not in errores_detalle:
                            errores_detalle.append(ls)

                    # Clasificar errores (fatales vs transitorios)
                    if (
                        RE_ERR.search(ls)
                        and not any(x in ls for x in KEYWORDS_RUIDO)
                        and "[RESUMEN]" not in ls
                    ):
                        tipo = clasificar_error(ls)
                        if tipo == "FATAL":
                            fatales += 1
                        else:
                            transitorios += 1

            errores_str = " | ".join(errores_detalle[:5])  # máx 5 errores únicos
            if len(errores_str) > 500:
                errores_str = errores_str[:497] + "..."
            if not errores_str:
                errores_str = "Ninguno"

            if not resumen:
                # Log sin [RESUMEN]: proceso interrumpido o log vacío
                conteo["sin_resumen"] += 1
                logs_a_comprimir.append(ruta)
                continue

            nombre = resumen.get("nombre_modelo", archivo.replace(".log", ""))
            url = resumen.get("url", "")
            nuevos = int(resumen.get("nuevos", 0))
            ya = int(resumen.get("ya_descargados", 0))
            errores = int(resumen.get("errores", 0))
            duracion = int(resumen.get("duracion", 0))
            rc = int(resumen.get("returncode", 0))
            timeout = resumen.get("timeout", "false").lower() == "true"

            estado = determinar_estado(
                rc, fatales, transitorios, timeout, nuevos, errores
            )
            conteo[estado.lower()] += 1

            filas_csv.append(
                [
                    ahora_str,
                    nombre,
                    url,
                    posts_omitidos,
                    nuevos,
                    ya,
                    errores,
                    errores_str,
                    duracion,
                    rc,
                    "Sí" if timeout else "No",
                    estado,
                ]
            )
            logs_a_comprimir.append(ruta)

        except Exception as e:
            print(f"  {YELLOW}[!] Error procesando {archivo}: {e}{RESET}")
            continue

    registrar_filas_en_csv(filas_csv)
    archivar_logs_en_zip(logs_a_comprimir)
    purgar_zip_antiguos(dias=60)
    imprimir_reporte(conteo, huerfanos, len(logs_a_comprimir))

    # Aviso de posts_fallidos.json si existe
    pf_path = os.path.join(os.path.dirname(AUDIT_CSV), "posts_fallidos.json")
    if os.path.exists(pf_path):
        try:
            with open(pf_path, "r", encoding="utf-8") as f:
                fallidos = json.load(f)
            total = sum(len(v.get("skip", [])) for v in fallidos.values())
            if total:
                print(
                    f"  {YELLOW}⚠️  Revisar posts_fallidos.json: {total} post(s) sugerido(s) en {len(fallidos)} URL(s){RESET}"
                )
                print(
                    f"  {GRAY}   Copiar manualmente a skip_posts.json para aplicar{RESET}\n"
                )
        except Exception:
            pass


# =============================================================================
# REPORTE VISUAL
# =============================================================================


def imprimir_reporte(conteo: dict, huerfanos: list, total_logs: int):
    sep = "\\" if sys.platform == "win32" else "/"
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}{MAGENTA}  REPORTE DE AUDITORÍA{RESET}")
    print(f"{GRAY}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 55}{RESET}\n")

    print(f"  Logs procesados     : {BOLD}{total_logs}{RESET}")
    print(f"  OK                  : {BOLD}{GREEN}{conteo['ok']}{RESET}")
    print(f"  Transitorios        : {BOLD}{YELLOW}{conteo['transitorio']}{RESET}")
    print(
        f"  Timeout Sanando     : {BOLD}{CYAN}{conteo.get('timeout_sanando', 0)}{RESET} {GRAY}(Hilo gigante estable){RESET}"
    )
    print(
        f"  Timeout Atascado    : {BOLD}{RED}{conteo.get('timeout_atascado', 0)}{RESET} {GRAY}(Host caído/Atascado){RESET}"
    )
    print(f"  Fatales             : {BOLD}{RED}{conteo['fatal']}{RESET}")

    if conteo["sin_resumen"]:
        print(
            f"  Sin [RESUMEN]       : {BOLD}{GRAY}{conteo['sin_resumen']}{RESET}"
            f"  {GRAY}(proceso interrumpido — comprimidos sin registrar){RESET}"
        )

    print()

    if huerfanos:
        print(f"  {YELLOW}Archivos .part huérfanos: {len(huerfanos)}{RESET}")
        for path in huerfanos:
            carpeta_vis = os.path.basename(os.path.dirname(path))
            archivo_vis = os.path.basename(path)
            print(
                f"    {RED}└──{RESET} {GRAY}...{sep}{carpeta_vis}{sep}{archivo_vis}{RESET}"
            )
        print()
    else:
        print(f"  Archivos .part      : {BOLD}{GREEN}0{RESET}\n")

    print(f"{BOLD}{'═' * 55}{RESET}\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    analizar_logs()
