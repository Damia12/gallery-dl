#!/usr/bin/env python3
import csv
import os
import re
import sys
import zipfile
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# ==========================================
# CONFIGURACIÓN DE RUTAS FIJAS (WINDOWS)
# ==========================================
RIPS_DIR = r"G:\Rips"
LOG_DIR = r"G:\Rips\logs"
RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
HISTORIAL_CSV = r"C:\gallery-dl\historial_fallos.csv"
ZIP_FILE = os.path.join(LOG_DIR, f"logs_{datetime.now().strftime('%Y-%m-%d')}.zip")

# Paletas de Color ANSI para Reporte Forense
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[90m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

# Expresiones regulares quirúrgicas
RE_LOG_ENRIQUECIDO = re.compile(
    r"(?:\[Post:\s*(\d+)\])?.*?(?:for|download)\s+'?(https?://[^\s']+)'?", re.IGNORECASE
)

# Diccionarios de clasificación de firmas de error
FATAL_KEYWORDS = ["404 not found", "thread deleted", "410 gone", "invalid thread"]
TRANSITORY_KEYWORDS = [
    "timeout",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "429 too many requests",
    "connection reset",
]

KEYWORDS_ERROR = ["error", "warning", "failed", "unsupported", "unable", "exception"]
KEYWORDS_RUIDO = ["theme-light", "color-", "--rem", "None_"]


def es_linea_error(linea):
    linea_lower = linea.lower()
    return any(k in linea_lower for k in KEYWORDS_ERROR) and not any(
        x in linea for x in KEYWORDS_RUIDO
    )


def clasificar_error(linea):
    linea_lower = linea.lower()
    if any(k in linea_lower for k in FATAL_KEYWORDS):
        return "FATAL"
    if any(k in linea_lower for k in TRANSITORY_KEYWORDS):
        return "TRANSITORIO"
    return "TRANSITORIO"


def registrar_en_csv(post_id, url, tipo_error, mensaje):
    existe = os.path.exists(HISTORIAL_CSV)
    os.makedirs(os.path.dirname(HISTORIAL_CSV), exist_ok=True)

    with open(HISTORIAL_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if not existe:
            writer.writerow(["Fecha", "Post_ID", "URL", "Tipo_Error", "Detalle_Error"])
        writer.writerow(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                post_id if post_id else "Desconocido",
                url,
                tipo_error,
                mensaje.strip(),
            ]
        )


def archivar_log_en_zip(ruta_log):
    try:
        with zipfile.ZipFile(ZIP_FILE, "a", compression=zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(ruta_log, os.path.basename(ruta_log))
        os.remove(ruta_log)
    except Exception as e:
        print(f"  {RED}[X] Error archivando {os.path.basename(ruta_log)}: {e}{RESET}")


def purgar_zip_antiguos(dias_retencion=60):
    """Busca y elimina archivos ZIP de logs que superen los días de retención establecidos."""
    import time

    if not os.path.exists(LOG_DIR):
        return

    ahora = time.time()
    limite_segundos = dias_retencion * 86400
    conteo_purgados = 0

    for archivo in os.listdir(LOG_DIR):
        if archivo.startswith("logs_") and archivo.endswith(".zip"):
            ruta_zip = os.path.join(LOG_DIR, archivo)
            try:
                tiempo_modificacion = os.path.getmtime(ruta_zip)
                if (ahora - tiempo_modificacion) > limite_segundos:
                    os.remove(ruta_zip)
                    conteo_purgados += 1
            except Exception as e:
                print(
                    f"  {YELLOW}[!] No se pudo purgar el archivo {archivo}: {e}{RESET}"
                )

    if conteo_purgados > 0:
        print(
            f"  {GRAY}└── [MANTENIMIENTO] Se eliminaron {conteo_purgados} archivos ZIP antiguos (+{dias_retencion} días).{RESET}"
        )


def buscar_part_huerfanos():
    part_detectados = []
    if not os.path.exists(RIPS_DIR):
        return part_detectados

    for raiz, _, archivos in os.walk(RIPS_DIR):
        if "logs" in raiz.lower():
            continue
        for archivo in archivos:
            if archivo.endswith(".part"):
                part_detectados.append(os.path.join(raiz, archivo))
    return part_detectados


def analizar_logs():
    if not os.path.exists(LOG_DIR):
        print(f"\n  {RED}[X] Error: La ruta de logs '{LOG_DIR}' no existe.{RESET}\n")
        return

    # Mapeo estructurado de reintentos: { URL: (Post_ID, Nombre_Carpeta) }
    diccionario_retry = {}
    mapeo_reporte_retry = {}
    conteo_fatales = 0
    conteo_transitorios = 0
    logs_a_procesar = [
        f for f in os.listdir(LOG_DIR) if f.endswith(".log") and f != "procesados.log"
    ]

    archivos_part_huerfanos = buscar_part_huerfanos()

    if not logs_a_procesar and not archivos_part_huerfanos:
        print(
            f"\n  {GREEN}[✓] Ecosistema limpio. Sin logs nuevos ni archivos .part huérfanos.{RESET}\n"
        )
        return

    for archivo in logs_a_procesar:
        ruta_completa = os.path.join(LOG_DIR, archivo)
        # El nombre del archivo log define el nombre exacto de la carpeta destino
        nombre_carpeta = os.path.splitext(archivo)[0]

        try:
            with open(ruta_completa, "r", encoding="utf-8", errors="replace") as f:
                for linea in f:
                    if not es_linea_error(linea):
                        continue

                    match = RE_LOG_ENRIQUECIDO.search(linea)
                    if match:
                        post_id = match.group(1)
                        url = match.group(2).strip()
                        tipo = clasificar_error(linea)

                        id_llave = post_id if post_id else "Desconocido"

                        if tipo == "FATAL":
                            conteo_fatales += 1
                            registrar_en_csv(id_llave, url, "FATAL", linea)
                        else:
                            conteo_transitorios += 1
                            # Guardamos la URL amarrada a sus metadatos de origen
                            diccionario_retry[url] = (id_llave, nombre_carpeta)
                            mapeo_reporte_retry[id_llave] = (
                                mapeo_reporte_retry.get(id_llave, 0) + 1
                            )
                            registrar_en_csv(id_llave, url, "TRANSITORIO", linea)

        except Exception as e:
            print(f"  {YELLOW}[!] Advertencia leyendo {archivo}: {e}{RESET}")
            continue

        archivar_log_en_zip(ruta_completa)

    # Escritura Enriquecida con Bloques de Control Meta
    if diccionario_retry:
        os.makedirs(os.path.dirname(RETRY_FILE), exist_ok=True)
        with open(RETRY_FILE, "w", encoding="utf-8") as f_out:
            f_out.write(
                "# Lista de reintentos enriquecida con metadatos contextuales\n"
            )
            for url, (p_id, folder) in sorted(diccionario_retry.items()):
                f_out.write(f"#META: id={p_id} | folder={folder}\n")
                f_out.write(f"{url}\n")
    else:
        if os.path.exists(RETRY_FILE):
            os.remove(RETRY_FILE)

    purgar_zip_antiguos(dias_retencion=60)

    imprimir_reporte(
        len(logs_a_procesar),
        conteo_fatales,
        conteo_transitorios,
        mapeo_reporte_retry,
        archivos_part_huerfanos,
    )


def imprimir_reporte(total_logs, fatales, transitorios, mapeo_reporte_retry, huerfanos):
    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}{CYAN}  REPORTE INTEGRAL DE AUDITORÍA ANALÍTICA{RESET}")
    print(f"{GRAY}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 55}{RESET}\n")

    print(f"  Archivos .log procesados y comprimidos: {BOLD}{total_logs}{RESET}")
    print(
        f"  Errores Permanentes (FATALES) purgados : {BOLD}{RED}{fatales}{RESET} {GRAY}(guardados en CSV){RESET}"
    )
    print(
        f"  Errores Transitorios aislados para retry: {BOLD}{GREEN}{transitorios}{RESET}"
    )

    if huerfanos:
        print(
            f"  Archivos .part huérfanos por Timeout   : {BOLD}{YELLOW}{len(huerfanos)}{RESET}"
        )
        for path in huerfanos:
            print(
                f"    {RED}└── Corrupto:{RESET} {GRAY}...\\{os.path.basename(os.path.dirname(path))}\\{os.path.basename(path)}{RESET}"
            )
        print()
    else:
        print(f"  Archivos .part huérfanos por Timeout   : {BOLD}{GREEN}0{RESET}\n")

    if mapeo_reporte_retry:
        print(
            f"{BOLD}{MAGENTA}  Distribución de reintentos por Publicación (Post ID):{RESET}"
        )
        for post_id, cuenta in mapeo_reporte_retry.items():
            color_id = YELLOW if post_id.isdigit() else GRAY
            print(
                f"    ├── Post ID: {color_id}{post_id}{RESET} ──> [{BOLD}{GREEN}{cuenta} URL(s) en cola{RESET}]"
            )
    else:
        print(
            f"  {GREEN}[✓] Carpeta limpia. No hay URLs caídas que requieran reintento.{RESET}"
        )

    print(f"{BOLD}\n{'═' * 55}{RESET}")


if __name__ == "__main__":
    analizar_logs()
