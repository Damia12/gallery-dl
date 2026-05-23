#!/usr/bin/env python3
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# =============================================================================
# COLORES ANSI
# =============================================================================
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
CYAN, GREEN, YELLOW, RED, GRAY = (
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[90m",
)


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
def cargar_configuracion():
    entorno = "windows" if sys.platform == "win32" else "linux"
    ruta_config = Path(__file__).parent / "config.json"
    with open(ruta_config, "r", encoding="utf-8") as f:
        data = json.load(f)
    cfg = data[entorno]
    paths = {k: Path(v) for k, v in cfg["paths"].items()}
    return paths, data["pipeline"], cfg["gallery_dl"]


PATHS, PIPELINE, GDL_CFG = cargar_configuracion()


# =============================================================================
# GESTIÓN DE ESTADO
# =============================================================================
def obtener_hash_lista(lista_path: Path) -> str:
    return hashlib.sha256(lista_path.read_bytes()).hexdigest()


def cargar_estado() -> dict:
    state_file = PATHS["state_file"]
    lista_hash = obtener_hash_lista(PATHS["lista_file"])
    default = {"batch_index": 0, "hash": lista_hash}
    if not state_file.exists():
        return default
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("hash") != lista_hash:
        return default
    return state


def guardar_estado(state: dict):
    with open(PATHS["state_file"], "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# =============================================================================
# LÓGICA DE EJECUCIÓN (MODO DASHBOARD)
# =============================================================================
def parsear_metricas(stdout: str, stderr: str) -> dict:
    nuevos = 0
    ya_descargados = 0
    errores = 0
    for linea in (stdout + "\n" + stderr).splitlines():
        ll = linea.lower()
        if any(x in ll for x in ["skipping", "already", "skip"]):
            ya_descargados += 1
        elif any(x in ll for x in ["error", "failed", "http"]) and "warning" not in ll:
            errores += 1
        elif Path(linea.strip()).suffix.lower() in [
            ".jpg",
            ".png",
            ".gif",
            ".webp",
            ".mp4",
        ]:
            nuevos += 1
    return {"nuevos": nuevos, "ya_descargados": ya_descargados, "errores": errores}


def ejecutar_url(url: str) -> dict:
    nombre = url.rstrip("/").split("/")[-1]
    log_path = PATHS["log_dir"] / f"{nombre}.log"
    PATHS["log_dir"].mkdir(parents=True, exist_ok=True)

    cmd = [GDL_CFG["executable"], "-c", GDL_CFG["config_file"], url]
    start = datetime.now()

    # Ejecutamos en silencio (capturamos output)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    # Escribimos log completo en segundo plano
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout + "\n" + proc.stderr)

    metricas = parsear_metricas(proc.stdout, proc.stderr)
    duracion = int((datetime.now() - start).total_seconds())

    # Bloque de resumen para auditoría (forense)
    resumen = (
        f'[RESUMEN] nombre_modelo="{nombre}" url="{url}" nuevos="{metricas["nuevos"]}" '
        f'ya_descargados="{metricas["ya_descargados"]}" errores="{metricas["errores"]}" '
        f'duracion="{duracion}" returncode="{proc.returncode}"'
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{resumen}\n")

    return {
        "nombre": nombre,
        "ok": proc.returncode == 0,
        **metricas,
        "duracion": duracion,
    }


# =============================================================================
# MAIN
# =============================================================================
def main():
    state = cargar_estado()
    lista = [
        l.strip()
        for l in PATHS["lista_file"].read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#")
    ]

    batch_size = PIPELINE["batch_size"]
    lote = lista[state["batch_index"] : state["batch_index"] + batch_size]

    if not lote:
        print("[+] Lista completada. Reiniciando.")
        guardar_estado(
            {"batch_index": 0, "hash": obtener_hash_lista(PATHS["lista_file"])}
        )
        return

    print(f"{BOLD}{CYAN}>>> INICIANDO LOTE ({len(lote)} URLs){RESET}\n")

    with ThreadPoolExecutor(max_workers=PIPELINE.get("workers", 1)) as pool:
        for futuro in as_completed([pool.submit(ejecutar_url, url) for url in lote]):
            res = futuro.result()
            icon = f"{GREEN}✓{RESET}" if res["ok"] else f"{RED}✗{RESET}"
            print(
                f"  {icon} {BOLD}{res['nombre']:<35}{RESET} "
                f"| {GREEN}Nuevos:{res['nuevos']:<3}{RESET} "
                f"| {GRAY}Ya:{res['ya_descargados']:<3}{RESET} "
                f"| {YELLOW if res['errores'] > 0 else GRAY}Err:{res['errores']:<2}{RESET} "
                f"| {GRAY}{res['duracion']}s{RESET}"
            )

    state["batch_index"] += len(lote)
    guardar_estado(state)

    print(f"\n{CYAN}[+] Ejecutando auditoría...{RESET}")
    subprocess.run([sys.executable, str(Path(__file__).parent / "auditar.py")])


if __name__ == "__main__":
    main()
