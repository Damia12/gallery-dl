#!/usr/bin/env python3
"""
Mock de gallery-dl para tests.
Simula un extractor que descarga 3 archivos y genera logs en stderr.
"""

import sys
import time

# Simula: "estoy procesando posts..."
for i in range(3):
    # stdout = archivo descargado (lo que gallery-dl imprime)
    sys.stdout.write(f"G:\\Rips\\Simpcity\\Modelo\\foto_{i:03d}.jpg\n")
    sys.stdout.flush()

    # stderr = log de debug con post_id (lo que gallery-dl loguea)
    sys.stderr.write(f"[simpcity][debug] post {1000 + i}: Sleeping 1.00 seconds\n")
    sys.stderr.flush()

    # Simula trabajo real (como si estuviera descargando)
    time.sleep(0.3)

# Al final, marca uno como "ya descargado" (con #)
sys.stdout.write("# done_foto_000.jpg\n")
sys.stdout.flush()

# Cierra limpio
sys.exit(0)
