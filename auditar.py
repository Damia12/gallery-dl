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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# =============================================================================
# CONFIGURACIÓN — mismo schema que descarga.py
# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def cargar_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"[X] No se encontró config.json en: {CONFIG_PATH}")
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

# =============================================================================
# REGEX
# =============================================================================

# Línea [RESUMEN] escrita por descarga.py — pares clave="valor"
RE_RESUMEN = re.compile(r"\[RESUMEN\](.+)", re.IGNORECASE)
RE_KV = re.compile(r'(\w+)="([^"]*)"')

# Errores en el cuerpo del log (stdout/stderr de gallery-dl)
RE_ERR = re.compile(
    r"(error|HttpError|urllib\.error|ConnectionError|ReadTimeout|"
    r"404|410|403|unable to extract|failed to parse|thread deleted|"
    r"invalid thread|unsupported url)",
    re.IGNORECASE,
)

KEYWORDS_RUIDO = ["theme-light", "color-", "--rem", "None_", "logging"]

FATAL_KEYWORDS = [
    "404 not found",
    "thread deleted",
    "410 gone",
    "410",
    "invalid thread",
    "unsupported url",
    "unable to extract",
    "failed to parse",
]

# =============================================================================
# CLASIFICACIÓN
# =============================================================================


def clasificar_error(linea: str) -> str:
    ll = linea.lower()
    if any(k in ll for k in FATAL_KEYWORDS):
        return "FATAL"
    return "TRANSITORIO"


def determinar_estado(returncode: int, fatales: int, transitorios: int) -> str:
    """
    Prioridad: FATAL > TRANSITORIO > OK
    returncode != 0 con errores conocidos se clasifica por tipo.
    returncode != 0 sin errores clasificados → TRANSITORIO (asumir reintentable).
    """
    if fatales > 0 and transitorios == 0:
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
    "Nuevos",
    "Ya_descargados",
    "Errores",
    "Duracion_s",
    "Returncode",
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
    conteo = {"ok": 0, "transitorio": 0, "fatal": 0, "sin_resumen": 0}

    for archivo in sorted(logs):
        ruta = os.path.join(LOG_DIR, archivo)
        try:
            with open(ruta, "r", encoding="utf-8", errors="replace") as f:
                lineas = f.readlines()

            # ── Extraer [RESUMEN] ────────────────────────────────────────────
            resumen = {}
            for linea in lineas:
                m = RE_RESUMEN.search(linea)
                if m:
                    resumen = dict(RE_KV.findall(m.group(1)))
                    break

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

            # ── Clasificar errores desde el cuerpo del log ───────────────────
            # (stdout+stderr de gallery-dl escritos por descarga.py)
            fatales = transitorios = 0
            if errores > 0:
                for linea in lineas:
                    ls = linea.strip()
                    if not RE_ERR.search(ls):
                        continue
                    if any(x in ls for x in KEYWORDS_RUIDO):
                        continue
                    if "[RESUMEN]" in ls:
                        continue
                    tipo = clasificar_error(ls)
                    if tipo == "FATAL":
                        fatales += 1
                    else:
                        transitorios += 1

            estado = determinar_estado(rc, fatales, transitorios)
            conteo[estado.lower()] += 1

            filas_csv.append(
                [
                    ahora_str,
                    nombre,
                    url,
                    nuevos,
                    ya,
                    errores,
                    duracion,
                    rc,
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
