#!/usr/bin/env python3
"""
Mock de gallery-dl para el escenario de FUSIÓN warning+error.

Simula el caso documentado en descarga.py: un warning transitorio de
[downloader.http] seguido de un error sobre el MISMO post_id. descarga.py
debe fusionar ambos en una sola línea ("<error> — <causa>") en vez de
imprimir dos líneas sueltas.
"""

import sys
import time

# 1) Log de parent-metadata: identifica el post_id activo (5555)
sys.stderr.write("[bunkr][debug] post 5555: Using archive\n")
sys.stderr.flush()
time.sleep(0.05)

# 2) Warning HTTP transitorio sobre ese post — descarga.py debe RETENERLO
#    (no imprimirlo todavía) mientras espera a ver si viene un error después.
sys.stderr.write("[downloader.http][warning] Rate limit exceeded, retrying\n")
sys.stderr.flush()
time.sleep(0.05)

# 3) Error fatal sobre el MISMO post_id — debe fusionarse con el warning previo.
sys.stderr.write("[downloader.http][error] 404 Not Found\n")
sys.stderr.flush()
time.sleep(0.05)

# Un archivo nuevo, solo para que el hilo principal tenga algo en stdout.
sys.stdout.write("G:\\Rips\\Simpcity\\Modelo\\foto_fallida.jpg\n")
sys.stdout.flush()

sys.exit(1)
