"""Set up a local Python environment and open Laptop Screen.

Double-click the launcher for this laptop, or run: python launch.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _venv_python() -> Path:
    folder = "Scripts" if sys.platform == "win32" else "bin"
    name = "python.exe" if sys.platform == "win32" else "python"
    return ROOT / ".venv" / folder / name


def _base_python() -> str:
    if sys.version_info >= (3, 10):
        return sys.executable
    for name in ("python3", "python", "py"):
        if name == "py":
            try:
                subprocess.run(["py", "-3", "-c", "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)"], check=True)
            except (OSError, subprocess.CalledProcessError):
                continue
            return "py"
        try:
            subprocess.run([name, "-c", "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)"], check=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        return name
    raise SystemExit("Python 3.10 or newer is required. Install it from python.org, then open this again.")


def _pip(python: Path) -> None:
    subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])


def _deps_ready(python: Path) -> bool:
    probe = subprocess.run(
        [str(python), "-c", "import PySide6, yt_dlp"],
        capture_output=True,
    )
    return probe.returncode == 0


def main() -> None:
    os.chdir(ROOT)
    venv_python = _venv_python()
    if Path(sys.executable).resolve() != venv_python.resolve():
        if not venv_python.is_file():
            base = _base_python()
            command = [base, "-3", "-m", "venv", str(ROOT / ".venv")] if base == "py" else [base, "-m", "venv", str(ROOT / ".venv")]
            subprocess.check_call(command)
        if not _deps_ready(venv_python):
            _pip(venv_python)
        os.execv(str(venv_python), [str(venv_python), str(ROOT / "launch.py"), *sys.argv[1:]])
    if not _deps_ready(venv_python):
        _pip(venv_python)
    os.execv(str(venv_python), [str(venv_python), str(ROOT / "main.py"), *sys.argv[1:]])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Setup failed ({exc.returncode}).") from exc
