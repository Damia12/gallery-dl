#!/usr/bin/env python3
import csv
import os
import re
import sys
import time
import zipfile
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

IS_WINDOWS = sys.platform == "win32"

# =============================================================================
# CONFIGURACIÓN DE RUTAS MULTIPLATAFORMA (WINDOWS / FEDORA LINUX)
# =============================================================================
if IS_WINDOWS:
    RIPS_DIR = r"G:\Rips"
    LOG_DIR = r"G:\Rips\logs"
    RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
    HISTORIAL_CSV = r"G:\Rips\logs\auditoria_errores.csv"
else:
    RIPS_DIR = os.path.expanduser("~/Rips")
    LOG_DIR = os.path.expanduser("~/Rips/logs")
    RETRY_FILE = os.path.expanduser("~/gallery-dl/lista_retry.txt")
    HISTORIAL_CSV = os.path.expanduser("~/Rips/logs/auditoria_errores.csv")

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

# ── Expresiones Regulares Optimizadas ────────────────────────────────────────

RE_URL_HEADER = re.compile(
    r"URL:\s*(https?://([^/]+)/threads/([^/.]+)\.(\d+))", re.IGNORECASE
)
RE_TIMEOUT = re.compile(r"TIMEOUT:\s*sin\s+actividad", re.IGNORECASE)
RE_EXTRAER_URL_CDN = re.compile(
    r"(?:for|download)\s+'?(https?://[^\s']+)'?", re.IGNORECASE
)
RE_HOST = re.compile(r"host='([^']+)'|Connection to (\S+) timed out", re.IGNORECASE)
RE_ERR_LINEA = re.compile(r"\[Post:\s*(\d+|None)\]|HttpError:", re.IGNORECASE)

FATAL_KEYWORDS = [
    "404 not found",
    "thread deleted",
    "410 gone",
    "invalid thread",
    "unsupported url",
    "unable to extract",
    "failed to parse",
]

KEYWORDS_RUIDO = ["theme-light", "color-", "--rem", "None_"]


# ── Funciones de Clasificación Analítica ─────────────────────────────────────


def clasificar_error(linea):
    linea_lower = linea.lower()
    if any(k in linea_lower for k in FATAL_KEYWORDS):
        return "FATAL"
    return "TRANSITORIO"


def extraer_contexto_cabecera(lineas):
    """Extrae URL, Slug e ID Maestro desde las cabeceras ampliando la tolerancia."""
    for linea in lineas[:100]:
        match = RE_URL_HEADER.search(linea.strip())
        if match:
            url_completa = match.group(1).rstrip("/")
            slug_hilo = match.group(3)
            thread_id = match.group(4)
            return url_completa, slug_hilo, thread_id
    return None, None, None


def extraer_nombre_carpeta(lineas, slug_fallback):
    """Infiere la carpeta destino desacoplando literales fijos del foro."""
    for linea in lineas:
        linea_strip = linea.strip()
        if "Rips" in linea_strip:
            partes = re.split(r"[\\/]", linea_strip)
            try:
                idx = partes.index("Rips")
                if idx + 2 < len(partes) and partes[idx + 2]:
                    return partes[idx + 2]
            except ValueError:
                pass
        if "Simpcity" in linea_strip:
            partes = re.split(r"[\\/]", linea_strip)
            try:
                idx = partes.index("Simpcity")
                if idx + 1 < len(partes) and partes[idx + 1]:
                    return partes[idx + 1]
            except ValueError:
                pass
    if slug_fallback:
        return slug_fallback.replace("-", " ").title()
    return "Desconocido"


# ── Operaciones de Archivo con Persistencia Concurrente y Atómica ────────────


def registrar_en_csv(fecha, carpeta, thread_id, url_cdn, tipo_error, detalle):
    os.makedirs(os.path.dirname(HISTORIAL_CSV), exist_ok=True)

    for intento in range(5):
        try:
            with open(HISTORIAL_CSV, "a+", newline="", encoding="utf-8") as f:
                f.seek(0)
                inicio = f.read(4)

                if not inicio:
                    f.write("sep=;\n")
                    writer = csv.writer(f, delimiter=";")
                    writer.writerow(
                        [
                            "Fecha",
                            "Carpeta",
                            "Thread_ID",
                            "URL_CDN",
                            "Tipo_Error",
                            "Detalle",
                        ]
                    )

                f.seek(0, os.SEEK_END)
                writer = csv.writer(f, delimiter=";")
                writer.writerow(
                    [fecha, carpeta, thread_id, url_cdn, tipo_error, detalle.strip()]
                )
                f.flush()
                os.fsync(f.fileno())
            break
        except OSError:
            time.sleep(0.1)


def archivar_logs_en_zip_seguro(lista_rutas_logs):
    if not lista_rutas_logs:
        return
    try:
        with zipfile.ZipFile(ZIP_FILE, "a", compression=zipfile.ZIP_DEFLATED) as zipf:
            for ruta in lista_rutas_logs:
                if os.path.exists(ruta):
                    hora_archivo = datetime.now().strftime("%H%M%S")
                    nombre_interno = f"{hora_archivo}_{os.path.basename(ruta)}"
                    zipf.write(ruta, arcname=nombre_interno)

        for ruta in lista_rutas_logs:
            try:
                os.remove(ruta)
            except OSError:
                pass
    except Exception as e:
        print(f"  {RED}[X] Error en el lote de compresion ZIP: {e}{RESET}")


def purgar_zip_antiguos(dias_retencion=60):
    if not os.path.exists(LOG_DIR):
        return
    ahora = datetime.now()
    conteo_purgados = 0

    for archivo in os.listdir(LOG_DIR):
        if archivo.startswith("logs_") and archivo.endswith(".zip"):
            ruta_zip = os.path.join(LOG_DIR, archivo)
            match = re.match(r"logs_(\d{4}-\d{2}-\d{2})\.zip", archivo)
            if match:
                try:
                    fecha_zip = datetime.strptime(match.group(1), "%Y-%m-%d")
                    if (ahora - fecha_zip).days > dias_retencion:
                        os.remove(ruta_zip)
                        conteo_purgados += 1
                except Exception as e:
                    print(
                        f"  {YELLOW}[!] No se pudo procesar la fecha de {archivo}: {e}{RESET}"
                    )

    if conteo_purgados > 0:
        print(
            f"  {GRAY}└── [MANTENIMIENTO] {conteo_purgados} ZIP(s) antiguos eliminados (+{dias_retencion} dias).{RESET}"
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


# ── Motor Principal de Control Contextual ────────────────────────────────────


def analizar_logs():
    if not os.path.exists(LOG_DIR):
        print(f"\n  {RED}[X] El directorio de logs no existe: {LOG_DIR}{RESET}\n")
        return

    logs_a_procesar = [
        f for f in os.listdir(LOG_DIR) if f.endswith(".log") and f != "procesados.log"
    ]
    archivos_part_huerfanos = buscar_part_huerfanos()

    if not logs_a_procesar and not archivos_part_huerfanos:
        print(
            f"\n  {GREEN}[+] Ecosistema limpio. Sin logs nuevos ni archivos .part huerfanos.{RESET}\n"
        )
        return

    diccionario_retry = {}
    mapeo_reporte_retry = {}
    total_fatales_global = 0
    total_transitorios_global = 0
    logs_comprimidos_exito = []
    ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for archivo in logs_a_procesar:
        ruta_completa = os.path.join(LOG_DIR, archivo)
        conteo_fatales_local = 0
        conteo_transitorios_local = 0

        try:
            with open(ruta_completa, "r", encoding="utf-8", errors="replace") as f:
                lineas = f.readlines()

            url_hilo, slug_hilo, thread_id = extraer_contexto_cabecera(lineas)
            if not url_hilo:
                continue

            nombre_carpeta = extraer_nombre_carpeta(lineas, slug_fallback=slug_hilo)

            tiene_timeout = False
            hosts_fallidos = set()
            hay_errores_interceptados = False

            for linea in lineas:
                linea_strip = linea.strip()

                if RE_TIMEOUT.search(linea_strip):
                    tiene_timeout = True
                    continue

                if not RE_ERR_LINEA.search(linea_strip):
                    continue
                if any(x in linea_strip for x in KEYWORDS_RUIDO):
                    continue

                hay_errores_interceptados = True
                tipo = clasificar_error(linea_strip)

                match_cdn = RE_EXTRAER_URL_CDN.search(linea_strip)
                url_cdn = match_cdn.group(1) if match_cdn else "N/A"

                match_host = RE_HOST.search(linea_strip)
                if match_host:
                    host = match_host.group(1) or match_host.group(2)
                    if host:
                        hosts_fallidos.add(host)

                registrar_en_csv(
                    ahora_str, nombre_carpeta, thread_id, url_cdn, tipo, linea_strip
                )

                if tipo == "FATAL":
                    conteo_fatales_local += 1
                else:
                    conteo_transitorios_local += 1

            tiene_contingencia = conteo_transitorios_local > 0 or tiene_timeout

            if tiene_timeout or (hay_errores_interceptados and tiene_contingencia):
                host_str = (
                    ", ".join(hosts_fallidos) if hosts_fallidos else "Desconocido"
                )
                motivo = "TIMEOUT" if tiene_timeout else f"CDN ({host_str})"

                diccionario_retry[thread_id] = {
                    "url": url_hilo,
                    "folder": nombre_carpeta,
                    "host": host_str,
                    "motivo": motivo,
                }
                mapeo_reporte_retry[thread_id] = (
                    mapeo_reporte_retry.get(thread_id, 0) + 1
                )
                total_transitorios_global += 1
            else:
                if hay_errores_interceptados and conteo_fatales_local > 0:
                    total_fatales_global += 1

            logs_comprimidos_exito.append(ruta_completa)

        except Exception as e:
            print(f"  {YELLOW}[!] Advertencia leyendo {archivo}: {e}{RESET}")
            continue

    if diccionario_retry:
        os.makedirs(os.path.dirname(RETRY_FILE), exist_ok=True)
        with open(RETRY_FILE, "w", encoding="utf-8") as f_out:
            f_out.write(
                "# Lista de reintentos enriquecida con metadatos contextuales\n"
            )
            for thread_id, datos in diccionario_retry.items():
                f_out.write(f"#META: id={thread_id} | folder={datos['folder']}\n")
                f_out.write(f"# HOST_FALLIDO: {datos['host']} ({datos['motivo']})\n")
                f_out.write(f"{datos['url']}\n\n")
    else:
        if os.path.exists(RETRY_FILE):
            try:
                os.remove(RETRY_FILE)
            except OSError:
                pass

    archivar_logs_en_zip_seguro(logs_comprimidos_exito)
    purgar_zip_antiguos(dias_retencion=60)

    imprimir_reporte(
        len(logs_comprimidos_exito),
        total_fatales_global,
        total_transitorios_global,
        mapeo_reporte_retry,
        archivos_part_huerfanos,
    )


# ── Reporte Visual Forense ───────────────────────────────────────────────────


def imprimir_reporte(total_logs, fatales, transitorios, mapeo_reporte_retry, huerfanos):
    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}{CYAN}  REPORTE INTEGRAL DE AUDITORÍA ANALÍTICA{RESET}")
    print(f"{GRAY}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 55}{RESET}\n")

    print(f"  Archivos .log procesados y comprimidos: {BOLD}{total_logs}{RESET}")
    print(
        f"  Hilos Permanentes (FATALES) purgados   : {BOLD}{RED}{fatales}{RESET} {GRAY}(guardados en CSV){RESET}"
    )
    print(
        f"  Hilos Transitorios aislados para retry : {BOLD}{GREEN}{transitorios}{RESET}"
    )

    if huerfanos:
        print(
            f"  Archivos .part huérfanos por Timeout   : {BOLD}{YELLOW}{len(huerfanos)}{RESET}"
        )
        sep = "\\" if IS_WINDOWS else "/"
        for path in huerfanos:
            print(
                f"    {RED}└── Corrupto:{RESET} {GRAY}...{sep}{os.path.basename(os.path.dirname(path))}{sep}{os.path.basename(path)}{RESET}"
            )
        print()
    else:
        print(f"  Archivos .part huérfanos por Timeout   : {BOLD}{GREEN}0{RESET}\n")

    if mapeo_reporte_retry:
        print(f"{BOLD}{MAGENTA}  Hilos en cola de rescate:{RESET}")
        for thread_id, cuenta in mapeo_reporte_retry.items():
            color_id = YELLOW if thread_id.isdigit() else GRAY
            print(
                f"    ├── Thread ID: {color_id}{thread_id}{RESET} ──> [{BOLD}{GREEN}{cuenta} sesion(es){RESET}]"
            )
    else:
        print(
            f"  {GREEN}[+] Carpeta limpia. No hay URLs caídas que requieran reintento.{RESET}"
        )

    print(f"{BOLD}\n{'═' * 55}{RESET}")


if __name__ == "__main__":
    analizar_logs()
