import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Garante diretório temporário de base previsível para módulos que criam pastas.
os.environ.setdefault("SIMULATOR_BASE_DIR", str(ROOT))
