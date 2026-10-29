#!/usr/bin/env python3
"""Guided launcher for qrencode: pick a file, answer a few questions, and the slideshow opens.

Double-click run.bat (Windows) or run.command (macOS / Linux), or run:  python run.py
Nothing is written to disk; see README.md.
"""
import io
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# (label, columns, rows). All use the same code size, so you can still switch in the slideshow.
SPEEDS = (
    ("Fast: 12 codes per page (recommended)", 4, 3),
    ("Normal: 6 codes per page", 3, 2),
    ("Careful: 2 codes per page, for a weak camera or a small screen", 2, 1),
)
CODE_BYTES = 390  # payload per code at the default size
CODES_PER_SECOND = {12: 30, 6: 15, 2: 5}  # at the default 2.5 pages per second


def say(text=""):
    print(text, flush=True)


def ask(prompt: str, default: str = "") -> str:
    answer = input(prompt).strip()
    return answer or default


def yes_no(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = ask(f"{prompt} {hint} ").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def choose(title: str, options: list[str], default: int = 1) -> int:
    say(title)
    for i, label in enumerate(options, 1):
        say(f"  {i}) {label}")
    while True:
        answer = ask(f"Choose 1-{len(options)} [{default}]: ", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)


def pick_file() -> str | None:
    """Native file dialog; None if there is no display or the dialog was cancelled."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askopenfilename(title="Choose the file to send") or None
        finally:
            root.destroy()
    except Exception:
        return None


def clean_path(text: str) -> str:
    """Terminals add quotes or backslashes when you drag a file in."""
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return os.path.expanduser(text.replace("\\ ", " "))


def get_input() -> tuple[str, bytes | None]:
    """-> (path or '-', message bytes when typing a message)."""
    kind = choose("What do you want to send?", ["A file", "A short text message"])
    if kind == 2:
        say("Type or paste the message. Finish with an empty line.")
        lines = []
        while (line := input()) != "":
            lines.append(line)
        return "-", ("\n".join(lines) + "\n").encode()
    path = pick_file()
    while not path or not Path(path).is_file():
        if path:
            say(f"Not a file: {path}")
        path = clean_path(ask("Type the path, or drag the file onto this window (Enter to quit): "))
        if not path:
            sys.exit(0)
    return path, None


def estimate(size: int, cols: int, rows: int) -> str:
    codes = max(1, size // CODE_BYTES)
    seconds = codes / CODES_PER_SECOND.get(cols * rows, 15)
    shown = f"{seconds:.0f} seconds" if seconds < 60 else f"{seconds / 60:.1f} minutes"
    return (f"about {codes} codes if it doesn't compress, roughly {shown} to scan at best "
            f"(longer if your phone misses some)")


def ensure_requirements() -> None:
    try:
        import numpy, segno, zstandard  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return
    except ImportError as e:
        say(f"A required package is missing ({e.name}).")
    if not yes_no("Install the encoder's packages now (pip install -r requirements.txt)?", True):
        sys.exit(1)
    done = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])
    if done.returncode != 0:
        say("\nThat didn't work. If pip says the environment is 'externally managed', create one:\n"
            f"  python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python run.py")
        sys.exit(1)


def main() -> None:
    say("qrtransfer: send a file to your phone through the screen\n"
        "Nothing is written to disk. The slideshow is shown from memory in a private window.\n")
    ensure_requirements()
    import qrencode

    path, message = get_input()
    size = len(message) if message is not None else Path(path).stat().st_size
    password = yes_no("Protect it with a password?", False)
    speed = choose("\nHow many codes per page to start with?", [o[0] for o in SPEEDS])
    _, cols, rows = SPEEDS[speed - 1]
    color = yes_no("Start in colour mode? (3x data per page, slower to read; you can toggle it in the slideshow)", False)

    say(f"\n{size:,} bytes: " + estimate(size, cols, rows))
    argv = [path, "--live", "--cols", str(cols), "--rows", str(rows)]
    if message is not None:
        argv += ["--name", "message.txt"]
    if password:
        argv.append("-p")
    if color:
        argv.append("--color")
    say("")
    old_stdin = sys.stdin
    try:
        if message is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(message), encoding="utf-8")
        qrencode.main(argv)
    finally:
        sys.stdin = old_stdin


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
