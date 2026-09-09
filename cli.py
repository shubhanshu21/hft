#!/usr/bin/env python3
"""
Root CLI entrypoint: delegates to backend/cli.py regardless of the parent folder name.
"""
import sys
import subprocess
from pathlib import Path

backend_cli = Path(__file__).resolve().parent / "backend" / "cli.py"
venv_py = Path(__file__).resolve().parent / "backend" / ".venv" / "bin" / "python3"
py_exec = str(venv_py) if venv_py.exists() else sys.executable

if __name__ == "__main__":
    if not backend_cli.exists():
        sys.exit(f"Error: {backend_cli} not found.")
    cmd = [py_exec, str(backend_cli)] + sys.argv[1:]
    sys.exit(subprocess.run(cmd).returncode)
