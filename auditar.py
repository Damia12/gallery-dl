#!/usr/bin/env python3
r"""
auditar.py — Analiza logs de descarga, detecta .part huérfanos y genera lista_retry.txt
Uso: python auditar.py [--rips-dir G:/Rips]
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    LOG_DIR = r"G:\Rips\logs"
    RIPS_DIR = r"G:\Rips"
    RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
    LISTA = r"C:\gallery-dl\lista.txt"
else:
    LOG_DIR = os.path.expanduser("~/Rips/logs")
    RIPS_DIR = os.path.expanduser("~/Rips")
    RETRY_FILE = os.path.expanduser("~/gallery-dl/lista_retry.txt")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"
WHITE = "\033[37m"

RE_URL = re.compile(r"^URL:\s*(.+)$", re.MULTILINE)
RE_FECHA = re.compile(r"^Fecha:\s*(.+)$", re.MULTILINE)
RE_TIMEOUT = re.compile(r"TIMEOUT", re.MULTILINE)


# ── Helpers ──────────────────────────────────────────────────────────────────


def leer_lista(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def agregar_a_retry(urls_nuevas):
    """Agrega URLs a lista_retry.txt evitando duplicados."""
    existentes = set()
    if os.path.exists(RETRY_FILE):
        with open(RETRY_FILE, "r", encoding="utf-8") as f:
            existentes = {l.strip() for l in f if l.strip() and not l.startswith("#")}

    agregadas = []
    with open(RETRY_FILE, "a", encoding="utf-8") as f:
        for url in urls_nuevas:
            if url not in existentes:
                f.write(url + "\n")
                existentes.add(url)
                agregadas.append(url)
    return agregadas


def cargar_urls_lista():
    """Lee lista.txt para poder mapear nombre → URL."""
    urls = leer_lista(LISTA)
    mapa = {}
    for url in urls:
        nombre = url.rstrip("/").split("/")[-1][:60]
        mapa[nombre] = url
    return mapa


# ── Análisis de logs ──────────────────────────────────────────────────────────


def parsear_log(log_path):
    """Parsea un archivo .log y devuelve lista de sesiones con sus datos."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        contenido = f.read()

    # Dividir por bloques de sesión (separados por ====)
    bloques = re.split(r"={10,}", contenido)
    sesiones = []

    for bloque in bloques:
        bloque = bloque.strip()
        if not bloque:
            continue

        url_match = RE_URL.search(bloque)
        fecha_match = RE_FECHA.search(bloque)
        timeout = bool(RE_TIMEOUT.search(bloque))

        # Parseo robusto de errores: captura todas las líneas después de "ERRORES:"
        errores = []
        en_bloque_errores = False
        for linea in bloque.splitlines():
            if linea.strip() == "ERRORES:":
                en_bloque_errores = True
                continue
            if en_bloque_errores:
                if linea.strip():
                    errores.append(linea.strip())
                else:
                    en_bloque_errores = False

        url = url_match.group(1).strip() if url_match else None
        fecha = fecha_match.group(1).strip() if fecha_match else None

        if url:
            sesiones.append(
                {
                    "url": url,
                    "fecha": fecha,
                    "timeout": timeout,
                    "errores": errores,
                }
            )

    return sesiones


def analizar_logs():
    """Escanea todos los .log y agrupa por URL la última sesión."""
    if not os.path.exists(LOG_DIR):
        print(f"  {RED}No se encontró el directorio de logs: {LOG_DIR}{RESET}")
        return {}

    logs = sorted(Path(LOG_DIR).glob("*.log"))
    if not logs:
        print(f"  {GRAY}No hay archivos .log en {LOG_DIR}{RESET}")
        return {}

    resultados = {}  # nombre_log → última sesión

    for log_path in logs:
        nombre = log_path.stem
        try:
            sesiones = parsear_log(log_path)
        except Exception as e:
            print(f"  {YELLOW}⚠ Error leyendo {log_path.name}: {e}{RESET}")
            continue

        if not sesiones:
            continue

        # Quedarse con la última sesión de cada log
        ultima = sesiones[-1]
        resultados[nombre] = ultima

    return resultados


# ── Detección de .part huérfanos ──────────────────────────────────────────────


def buscar_parts(rips_dir):
    """Busca archivos .part recursivamente en el directorio de rips."""
    parts = []
    for root, _, files in os.walk(rips_dir):
        for fname in files:
            if fname.endswith(".part"):
                ruta = os.path.join(root, fname)
                tam = os.path.getsize(ruta)
                parts.append({"ruta": ruta, "nombre": fname, "tam": tam})
    return parts


def mapear_parts_a_urls(parts, mapa_urls):
    """Intenta mapear cada .part a una URL conocida por la carpeta padre."""
    mapeados = []
    sin_mapear = []

    for p in parts:
        # Buscar si algún segmento del path coincide con un nombre de thread conocido
        partes_ruta = Path(p["ruta"]).parts
        url_encontrada = None
        for segmento in partes_ruta:
            segmento_clean = segmento[:60]
            if segmento_clean in mapa_urls:
                url_encontrada = mapa_urls[segmento_clean]
                break

        if url_encontrada:
            mapeados.append({**p, "url": url_encontrada})
        else:
            sin_mapear.append(p)

    return mapeados, sin_mapear


# ── Reporte ───────────────────────────────────────────────────────────────────


def imprimir_reporte(resultados_logs, parts_mapeados, parts_sin_mapear, urls_retry):
    ancho = 56
    print(f"\n{BOLD}{'═' * ancho}{RESET}")
    print(
        f"{BOLD}  REPORTE DE AUDITORÍA — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}"
    )
    print(f"{BOLD}{'═' * ancho}{RESET}\n")

    # ── Resumen de logs
    con_errores = [(n, s) for n, s in resultados_logs.items() if s["errores"]]
    con_timeout = [(n, s) for n, s in resultados_logs.items() if s["timeout"]]
    sin_problemas = [
        (n, s)
        for n, s in resultados_logs.items()
        if not s["errores"] and not s["timeout"]
    ]

    print(f"{BOLD}  LOGS ANALIZADOS: {len(resultados_logs)}{RESET}")
    print(f"  {GREEN}✓ Sin problemas:  {len(sin_problemas)}{RESET}")
    print(f"  {YELLOW}⚠ Con errores:    {len(con_errores)}{RESET}")
    print(f"  {RED}⏱ Con timeout:    {len(con_timeout)}{RESET}")

    if con_errores:
        print(f"\n{BOLD}  HILOS CON ERRORES:{RESET}")
        for nombre, sesion in con_errores:
            print(f"\n  {YELLOW}▸ {nombre}{RESET}")
            print(f"    {GRAY}URL:   {sesion['url']}{RESET}")
            print(f"    {GRAY}Fecha: {sesion['fecha']}{RESET}")
            for err in sesion["errores"][:5]:
                print(f"    {RED}· {err}{RESET}")
            if len(sesion["errores"]) > 5:
                print(
                    f"    {DIM}  ... y {len(sesion['errores']) - 5} errores más (ver log){RESET}"
                )

    if con_timeout:
        print(f"\n{BOLD}  HILOS CON TIMEOUT:{RESET}")
        for nombre, sesion in con_timeout:
            print(f"  {RED}▸ {nombre}{RESET}")
            print(f"    {GRAY}{sesion['url']}{RESET}")

    # ── .part huérfanos
    print(f"\n{BOLD}{'─' * ancho}{RESET}")
    print(
        f"{BOLD}  ARCHIVOS .PART HUÉRFANOS: {len(parts_mapeados) + len(parts_sin_mapear)}{RESET}"
    )

    if parts_mapeados:
        print(f"\n  {YELLOW}Mapeados a URL conocida ({len(parts_mapeados)}):{RESET}")
        for p in parts_mapeados:
            tam_kb = p["tam"] / 1024
            tam_str = f"{tam_kb:.1f} KB" if tam_kb < 1024 else f"{tam_kb / 1024:.1f} MB"
            print(f"  {YELLOW}▸ {p['nombre']}{RESET} {GRAY}({tam_str}){RESET}")
            print(f"    {GRAY}{p['ruta']}{RESET}")

    if parts_sin_mapear:
        print(
            f"\n  {DIM}Sin URL conocida ({len(parts_sin_mapear)}) — solo reporte:{RESET}"
        )
        for p in parts_sin_mapear:
            tam_kb = p["tam"] / 1024
            tam_str = f"{tam_kb:.1f} KB" if tam_kb < 1024 else f"{tam_kb / 1024:.1f} MB"
            print(f"  {DIM}▸ {p['nombre']} ({tam_str}){RESET}")
            print(f"    {GRAY}{p['ruta']}{RESET}")

    if not parts_mapeados and not parts_sin_mapear:
        print(f"  {GREEN}No se encontraron archivos .part{RESET}")

    # ── URLs agregadas a retry
    print(f"\n{BOLD}{'─' * ancho}{RESET}")
    print(f"{BOLD}  LISTA_RETRY.TXT{RESET}")
    if urls_retry:
        print(f"  {YELLOW}→ {len(urls_retry)} URLs agregadas:{RESET}")
        for url in urls_retry:
            print(f"    {GRAY}{url}{RESET}")
    else:
        print(f"  {GREEN}No hay URLs nuevas para agregar{RESET}")

    # Total en retry
    total_retry = leer_lista(RETRY_FILE)
    if total_retry:
        print(f"\n  {BOLD}Total en lista_retry.txt: {len(total_retry)}{RESET}")
        print(
            f"  {DIM}Corré: python descarga.py (con lista_retry.txt como LISTA) para procesarlas{RESET}"
        )

    print(f"\n{BOLD}{'═' * ancho}{RESET}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Soporte para --rips-dir personalizado
    rips_dir = RIPS_DIR
    if "--rips-dir" in sys.argv:
        idx = sys.argv.index("--rips-dir")
        if idx + 1 < len(sys.argv):
            rips_dir = sys.argv[idx + 1]

    print(f"{BOLD}{'═' * 56}{RESET}")
    print(f"{BOLD}  Auditando logs en: {LOG_DIR}{RESET}")
    print(f"{BOLD}  Buscando .part en: {rips_dir}{RESET}")
    print(f"{BOLD}{'═' * 56}{RESET}\n")

    # 1. Analizar logs
    print(f"  {DIM}Analizando logs...{RESET}")
    resultados_logs = analizar_logs()

    # 2. Detectar .part
    print(f"  {DIM}Buscando archivos .part...{RESET}")
    parts = buscar_parts(rips_dir)
    mapa_urls = cargar_urls_lista()
    parts_mapeados, parts_sin_mapear = mapear_parts_a_urls(parts, mapa_urls)

    # 3. Determinar URLs para retry
    urls_para_retry = []

    for nombre, sesion in resultados_logs.items():
        if sesion["errores"] or sesion["timeout"]:
            urls_para_retry.append(sesion["url"])

    # --- CORRECCIÓN AQUÍ ---
    for p in parts_mapeados:
        if p["url"] not in urls_para_retry:
            urls_para_retry.append(p["url"])
    # ------------------------

    # 4. Escribir lista_retry.txt si hay fallos
    if urls_para_retry:
        with open(RETRY_FILE, "w", encoding="utf-8") as f:
            for url in urls_para_retry:
                f.write(url + "\n")

    # 5. Imprimir el reporte visual en la terminal
    imprimir_reporte(resultados_logs, parts_mapeados, parts_sin_mapear, urls_para_retry)

    # 6. ── NUEVA LÓGICA DE AUTO-ARCHIVADO DE LOGS ──
    if resultados_logs:
        archivo_dir = os.path.join(LOG_DIR, "archivo")
        os.makedirs(archivo_dir, exist_ok=True)

        # Movemos los archivos analizados para dejar la carpeta logs limpia y fresca
        for filename in os.listdir(LOG_DIR):
            if filename.endswith(".log"):
                ruta_original = os.path.join(LOG_DIR, filename)
                ruta_destino = os.path.join(archivo_dir, filename)
                try:
                    # Si el archivo de destino ya existe, lo removemos antes de mover
                    if os.path.exists(ruta_destino):
                        os.remove(ruta_destino)
                    os.rename(ruta_original, ruta_destino)
                except OSError:
                    pass
        print(
            f"  {GRAY}🧹 Logs analizados movidos a la carpeta '\\logs\\archivo\\'. Carpeta principal limpia.{RESET}\n"
        )

    if IS_WINDOWS:
        input("Presiona Enter para cerrar...")
