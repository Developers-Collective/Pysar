from pathlib import Path
from pysar.main import main as desktop_main

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

def main() -> SystemExit:
    return SystemExit(int(desktop_main()))

