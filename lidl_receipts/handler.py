"""Claim com.lidlplus.app:// so the login callback lands here by itself.

A desktop OAuth client normally catches the redirect with a loopback web
server. That is not available here: Lidl's IdentityServer only accepts the
mobile app's own redirect URI, and refuses a loopback one before the login
form is even shown. /connect/authorize bounces
http://localhost:8788/callback straight to /error, while
com.lidlplus.app://callback gets the login page. That is a server-side
answer, so it holds on every platform; there is no port to listen on
anywhere.

So do what the phone does: register the scheme with the desktop. The browser
then hands the whole callback URL to a handler instead of failing silently,
and nobody has to read it out of the network tab.

Three desktops, one idea:

  windows  HKCU\\Software\\Classes, written with winreg, run via pythonw.exe
           so no console window appears
  wsl      the same registry key, but written through PowerShell (winreg
           does not exist on the Linux side) and pointing back into the
           distro via wsl.exe
  xdg      a .desktop file claiming the scheme, plus xdg-mime

The registered handler is deliberately dumb. It drops the code in a file and
exits; the `lidl login` that is already waiting does the token exchange
itself, in the terminal, where a failure is visible. The handler runs
detached and usually windowless, which is the worst possible place to report an
error, so it never raises: a bad callback is written to the same file as
an error and reported by the side that has a terminal.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from base64 import b64encode
from pathlib import Path

from .config import CONFIG_DIR

SCHEME = "com.lidlplus.app"
CALLBACK_PATH = CONFIG_DIR / "callback.json"

DESKTOP_ID = "lidl-receipts-callback.desktop"
DESKTOP_PATH = Path.home() / ".local" / "share" / "applications" / DESKTOP_ID

# Same key on both Windows desktops; only the way in differs.
REG_PATH = f"Software\\Classes\\{SCHEME}"
REG_COMMAND_PATH = f"{REG_PATH}\\shell\\open\\command"
REG_DESCRIPTION = "URL:Lidl Plus login"

# System32 rather than the WindowsApps shim: the registry value is read by
# Explorer, which does not necessarily have the user's PATH.
WSL_EXE = "C:\\Windows\\System32\\wsl.exe"


# --------------------------------------------------------------------------
# Which desktop are we on
# --------------------------------------------------------------------------

def backend() -> str | None:
    """Return "windows", "wsl", "xdg", or None when the scheme cannot be
    registered (macOS, where Launch Services wants a real app bundle)."""
    if sys.platform == "win32":
        return "windows"
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() and _powershell():
        return "wsl"
    if sys.platform.startswith("linux") and shutil.which("xdg-mime"):
        return "xdg"
    return None


def _powershell() -> str | None:
    return shutil.which("powershell.exe")


def _entry_script() -> str:
    """The absolute path this program can be re-invoked through."""
    script = Path(__file__).resolve().parent.parent / "lidl.py"
    if script.exists():
        return str(script)
    return ""


def _entry_command() -> list[str]:
    """How to invoke this program again, as an absolute argv."""
    script = _entry_script()
    if script:
        return [_interpreter(), script]
    # Installed as a package rather than run from a checkout.
    return [_interpreter(), "-m", "lidl_receipts"]


def _interpreter() -> str:
    """The Python to launch the handler with.

    On Windows that is pythonw.exe where available: the handler is started by
    Explorer, and python.exe would flash a console window in the middle of a
    login for the sake of a program that prints nothing.
    """
    if sys.platform == "win32":
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return sys.executable


def _receive_argv(url_placeholder: str) -> list[str]:
    """The handler invocation, with the desktop's own URL placeholder.

    --callback is spelled out rather than inherited from the environment:
    Explorer starts the handler without the user's shell, so LIDL_RECEIPTS_HOME
    would be missing and the code would land in a directory nobody is watching.
    The placeholder goes last so the substituted URL is the final token.
    """
    return _entry_command() + [
        "login",
        "--callback",
        str(CALLBACK_PATH),
        "--receive",
        url_placeholder,
    ]


def _join(parts: list[str]) -> str:
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def _windows_style_command(prefix: list[str]) -> str:
    """A command line for Explorer, shared by the Windows and WSL backends.

    The %1 placeholder is quoted by hand rather than by _join: it contains no
    space, but the callback URL substituted into it must survive as one
    argument whatever characters Lidl puts in the code.
    """
    argv = prefix + _receive_argv("%1")
    return _join(argv[:-1]) + ' "%1"'


# --------------------------------------------------------------------------
# The drop file
# --------------------------------------------------------------------------

def clear(path: Path | None = None) -> None:
    Path(path or CALLBACK_PATH).unlink(missing_ok=True)


def deposit(pasted: str, path: Path | str | None = None) -> None:
    """Called by the handler: park the callback for the waiting login.

    Errors are written to the file rather than raised, because nobody is
    watching this process's output.
    """
    from . import auth

    target = Path(path or CALLBACK_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = {"code": auth.extract_code(pasted)}
    except ValueError as exc:
        payload = {"error": str(exc)}
    # An authorization code is a bearer credential, however short-lived.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)


def wait_for_code(timeout: float = 600.0, interval: float = 0.25) -> str | None:
    """Block until the handler drops a code. None means "do it by hand".

    Returns None on Ctrl-C or on timeout, so the caller can fall back to the
    paste prompt instead of stranding a half-finished login.
    """
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if CALLBACK_PATH.exists():
                try:
                    payload = json.loads(CALLBACK_PATH.read_text())
                except (OSError, json.JSONDecodeError):
                    # Still being written; look again next tick.
                    time.sleep(interval)
                    continue
                clear()
                if payload.get("error"):
                    raise ValueError(payload["error"])
                return payload.get("code") or None
            time.sleep(interval)
    except KeyboardInterrupt:
        return None
    return None


# --------------------------------------------------------------------------
# Windows: a URL protocol under HKCU
# --------------------------------------------------------------------------

def _windows_command() -> str:
    return _windows_style_command([])


def _windows_install() -> str:
    import winreg

    command = _windows_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, REG_DESCRIPTION)
        # Presence of this empty value is what marks the key as a protocol.
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_COMMAND_PATH) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
    return command


def _windows_registered() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_COMMAND_PATH) as key:
            return winreg.QueryValueEx(key, None)[0] or None
    except FileNotFoundError:
        return None


def _windows_uninstall() -> None:
    import winreg

    for path in (REG_COMMAND_PATH, f"{REG_PATH}\\shell\\open",
                 f"{REG_PATH}\\shell", REG_PATH):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# WSL: the same registry key, written from the Linux side
# --------------------------------------------------------------------------

def _wsl_command() -> str:
    parts = [WSL_EXE]
    distro = os.environ.get("WSL_DISTRO_NAME")
    if distro:
        # Explorer would otherwise use whichever distro happens to be default.
        parts += ["-d", distro]
    parts.append("--exec")
    return _windows_style_command(parts)


def _run_powershell(script: str) -> str:
    """Run a PowerShell script, passed base64 so nothing needs quoting.

    Building a reg.exe command line means threading quotes through WSL's argv
    into Windows' own re-parsing. -EncodedCommand sidesteps both.
    """
    exe = _powershell()
    if not exe:
        raise RuntimeError("powershell.exe not found; is WSL interop enabled?")
    encoded = b64encode(script.encode("utf-16-le")).decode()
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "powershell failed").strip()
        )
    return result.stdout.strip()


def _ps_quote(value: str) -> str:
    """Escape for a PowerShell single-quoted string."""
    return value.replace("'", "''")


def _wsl_install() -> str:
    command = _wsl_command()
    _run_powershell(f"""
$ErrorActionPreference = 'Stop'
$root = 'HKCU:\\{REG_PATH}'
New-Item -Path $root -Force | Out-Null
Set-ItemProperty -Path $root -Name '(default)' -Value '{REG_DESCRIPTION}'
Set-ItemProperty -Path $root -Name 'URL Protocol' -Value ''
$cmd = 'HKCU:\\{REG_COMMAND_PATH}'
New-Item -Path $cmd -Force | Out-Null
Set-ItemProperty -Path $cmd -Name '(default)' -Value '{_ps_quote(command)}'
""")
    return command


def _wsl_registered() -> str | None:
    return _run_powershell(f"""
$p = 'HKCU:\\{REG_COMMAND_PATH}'
if (Test-Path $p) {{ (Get-ItemProperty -Path $p).'(default)' }}
""") or None


def _wsl_uninstall() -> None:
    _run_powershell(
        f"Remove-Item -Path 'HKCU:\\{REG_PATH}' -Recurse -Force "
        "-ErrorAction SilentlyContinue"
    )


# --------------------------------------------------------------------------
# Linux desktop: a .desktop file claiming the scheme
# --------------------------------------------------------------------------

def _xdg_exec() -> str:
    # %u, not %1: the freedesktop spec's placeholder for a single URL.
    return _join(_receive_argv("%u"))


def _xdg_install() -> str:
    exec_line = _xdg_exec()
    DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Lidl Plus login callback\n"
        f"Exec={exec_line}\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        f"MimeType=x-scheme-handler/{SCHEME};\n"
    )
    _update_desktop_database()
    subprocess.run(
        ["xdg-mime", "default", DESKTOP_ID, f"x-scheme-handler/{SCHEME}"],
        capture_output=True,
        check=True,
    )
    return exec_line


def _update_desktop_database() -> None:
    # Absent on minimal installs; xdg-mime alone is enough there.
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(DESKTOP_PATH.parent)],
            capture_output=True,
            check=False,
        )


def _xdg_registered() -> str | None:
    result = subprocess.run(
        ["xdg-mime", "query", "default", f"x-scheme-handler/{SCHEME}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip() != DESKTOP_ID or not DESKTOP_PATH.exists():
        return None
    for line in DESKTOP_PATH.read_text().splitlines():
        if line.startswith("Exec="):
            return line[len("Exec="):]
    return None


def _xdg_uninstall() -> None:
    DESKTOP_PATH.unlink(missing_ok=True)
    _update_desktop_database()


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

_BACKENDS = {
    "windows": (_windows_install, _windows_registered, _windows_uninstall),
    "wsl": (_wsl_install, _wsl_registered, _wsl_uninstall),
    "xdg": (_xdg_install, _xdg_registered, _xdg_uninstall),
}

UNSUPPORTED = (
    "No way to register a URL scheme on this system. Supported: Windows, "
    "WSL with Windows interop, and a Linux desktop with xdg-mime."
)

_FAILURES = (RuntimeError, OSError, subprocess.SubprocessError)


def install() -> str:
    kind = backend()
    if kind is None:
        raise RuntimeError(UNSUPPORTED)
    return _BACKENDS[kind][0]()


def registered_command() -> str | None:
    """The command the desktop would run, or None when nothing is claimed."""
    kind = backend()
    if kind is None:
        return None
    try:
        return _BACKENDS[kind][1]()
    except _FAILURES:
        return None


def uninstall() -> None:
    kind = backend()
    if kind is not None:
        _BACKENDS[kind][2]()


def expected_command() -> str:
    kind = backend()
    if kind == "windows":
        return _windows_command()
    if kind == "wsl":
        return _wsl_command()
    if kind == "xdg":
        return _xdg_exec()
    return ""


def installed() -> bool:
    """True when the scheme points at *this* checkout.

    A handler left behind by a copy of the project somewhere else would drop
    the code in another directory, and this login would wait for a file that
    never appears, so a stale registration counts as absent.
    """
    current = registered_command()
    if not current:
        return False
    return str(CALLBACK_PATH) in current and _entry_command()[-1] in current


def ensure() -> str | None:
    """Register the scheme unless it is already ours. Best effort.

    Called from `lidl login`, so it must never be the reason a login fails:
    every failure just means falling back to pasting the URL by hand.
    """
    if installed():
        return None
    try:
        install()
    except _FAILURES:
        return None
    return backend()
