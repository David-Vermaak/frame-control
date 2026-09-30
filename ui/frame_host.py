"""What differs between the computers Frame Control runs on: macOS, Linux, Windows.

Everything here runs on your computer, not the Frame. Python stdlib only.

CLI (used by the Electron app, so terminal handling lives in one place):
  python3 ui/frame_host.py terminal -- CMD [ARG...]   # open CMD in a terminal window
"""
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

MAC = sys.platform == "darwin"
WINDOWS = os.name == "nt"
LINUX = not MAC and not WINDOWS
NAME = "macOS" if MAC else "Windows" if WINDOWS else "Linux"
FILE_MANAGER = "Finder" if MAC else "File Explorer" if WINDOWS else "your file manager"

# Windows' OpenSSH client can't share one connection between commands
# (no ControlMaster), so there each command opens its own.
MUX = not WINDOWS

# Popen() keyword arguments that detach a child from our console and signals.
DETACHED = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
            else {"start_new_session": True})


class HostError(RuntimeError):
    pass


class NotListening(HostError):
    """The Frame answers, but a service on it doesn't: something the person can turn on."""


def data_dir(*parts):
    """Per-user app data: ~/Library/Application Support, %APPDATA% or $XDG_DATA_HOME
    (or $FRAME_CONTROL_DATA_DIR, which the tests point at a throwaway directory)."""
    if os.environ.get("FRAME_CONTROL_DATA_DIR"):
        base = Path(os.environ["FRAME_CONTROL_DATA_DIR"])
    elif MAC:
        base = Path.home() / "Library" / "Application Support" / "Frame Control"
    elif WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Frame Control"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "frame-control"
    return base.joinpath(*parts)


def cache_dir(*parts):
    if MAC:
        base = Path.home() / "Library" / "Caches" / "Frame Control"
    elif WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Frame Control" / "Cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "frame-control"
    return base.joinpath(*parts)


def control_path(tag="x", *, private=None):
    """ssh ControlPath for the shared connection, or None where it isn't supported.

    `tag` names the headset: ssh's %C hashes only the address, user and port, so two
    headsets reached at the same address (one of them moved) would otherwise share a
    connection, and one's commands would run on the other.
    /tmp, not $TMPDIR: macOS's per-user temp path overflows the unix socket path limit.
    """
    # A private server (the MCP adapter's) keeps its own masters: FRAME_PRIVATE_SSH=1.
    if private is None:
        private = os.environ.get("FRAME_PRIVATE_SSH") == "1"
    suffix = f"-{os.getpid()}" if private else ""
    return f"/tmp/frame-ui-{os.getuid()}{suffix}-{tag}-%C" if MUX else None


def which(name, *extra):
    """First executable among PATH and the extra candidate paths."""
    for cand in (shutil.which(name), *extra):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def install_hint(tool):
    """How to get a missing command-line tool on this computer."""
    hints = {
        "adb": {"mac": "brew install android-platform-tools",
                "win": "winget install Google.PlatformTools",
                "linux": "install your distribution's adb package (e.g. sudo apt install adb)"},
    }
    return hints[tool]["mac" if MAC else "win" if WINDOWS else "linux"]


def android_sdk_dirs():
    """Where the Android SDK usually lives, for adb."""
    dirs = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if MAC:
        dirs += ["~/Library/Android/sdk", "/opt/homebrew/share/android-commandlinetools",
                 "~/.homebrew/share/android-commandlinetools"]
    elif WINDOWS:
        dirs += [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")]
    else:
        dirs += ["~/Android/Sdk", "/usr/lib/android-sdk"]
    return [os.path.expanduser(d) for d in dirs if d]


def adb():
    exe = "adb.exe" if WINDOWS else "adb"
    extra = [os.path.join(d, "platform-tools", exe) for d in android_sdk_dirs()]
    if MAC:
        extra += ["/opt/homebrew/bin/adb", str(Path.home() / ".homebrew/bin/adb"), "/usr/local/bin/adb"]
    # The app bundles adb as a last resort: an adb you already use goes first, so
    # two different adb versions don't keep restarting each other's server.
    tools = os.environ.get("FRAME_CONTROL_TOOLS")
    if tools:
        extra.append(os.path.join(tools, exe))
    env = os.environ.get("ADB")
    found = (env if env and os.access(env, os.X_OK) else None) or which("adb", *extra)
    if not found:
        raise HostError(f"adb isn't installed on this computer: {install_hint('adb')}")
    return found


def trust_bundled_cas():
    """Trust the app's CA bundle for HTTPS as well as the system's certificates.

    Python on Windows only sees the root certificates already in the Windows
    store, and a fresh install fetches those lazily, so Steam and F-Droid can
    fail with CERTIFICATE_VERIFY_FAILED. The app bundles curl's copy of Mozilla's
    CA list (app/build/fetch-deps.js); outside the app this does nothing. Call it
    before the first urlopen: urllib keeps the HTTPS context it builds then.
    """
    tools = os.environ.get("FRAME_CONTROL_TOOLS")
    cafile = os.path.join(tools, "cacert.pem") if tools else None
    if not cafile or not os.path.isfile(cafile):
        return

    def context(*args, **kwargs):
        ctx = ssl.create_default_context(*args, **kwargs)
        ctx.load_verify_locations(cafile)
        return ctx
    ssl._create_default_https_context = context  # urllib's default for HTTPS


def open_path(path):
    """Show a folder or file in the file manager."""
    path = str(path)
    if WINDOWS:
        os.startfile(path)  # noqa: pylint only on Windows
        return
    opener = "open" if MAC else which("xdg-open")
    if not opener:
        raise HostError("xdg-open isn't installed, so the folder can't be opened")
    subprocess.Popen([opener, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **DETACHED)


open_url = open_path  # the same openers hand URLs to the default browser


def _spawn(argv):
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **DETACHED)


def open_terminal(argv, title="Frame Control"):
    """Run argv in a new terminal window, for anything that asks for a password.

    The window stays open after the command ends, so its output can be read.
    """
    argv = [str(a) for a in argv]
    if MAC:
        command = shlex.join(argv).replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(["osascript", "-e", 'tell application "Terminal"',
                            "-e", f'do script "{command}"', "-e", "activate", "-e", "end tell"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # Usually macOS Automation consent for Terminal was denied.
            raise HostError(f"Couldn't open Terminal: {r.stderr.strip()}")
        return "Terminal"
    if WINDOWS:
        # `start` gives the command its own console window; cmd /k keeps it open.
        # One hand-built command line: quoting it twice through list2cmdline would
        # produce backslash-escaped quotes, which cmd doesn't understand.
        # Every argument is quoted, so cmd treats & | < > ^ in them literally. cmd has
        # no escape for a quote inside quotes (and expands %VAR% regardless), so refuse those.
        if any(c in a for a in argv for c in '"%\r\n'):
            raise HostError("Can't pass quotes or % to a Windows terminal")
        inner = " ".join(f'"{a}"' for a in argv)
        subprocess.Popen(f'cmd.exe /c start "{title}" cmd.exe /k "{inner}"', **DETACHED)
        return "a terminal window"
    script = f'{shlex.join(argv)}; echo; read -r -p "Press Enter to close. " _'
    # flags=None: the terminal takes the whole command as one string after -e.
    for name, flags in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("ptyxis", ["--"]),
                        ("kgx", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
                        ("tilix", None), ("lxterminal", None), ("kitty", []), ("alacritty", ["-e"]),
                        ("wezterm", ["start", "--"]), ("foot", []), ("xterm", ["-e"])):
        exe = which(name)
        if not exe:
            continue
        if flags is None:
            _spawn([exe, "-e", "bash -c " + shlex.quote(script)])
        elif name == "x-terminal-emulator" and "lxterminal" in os.path.realpath(exe):
            _spawn([exe, "-e", "bash -c " + shlex.quote(script)])  # Debian alternative -> lxterminal
        else:
            _spawn([exe, *flags, "bash", "-c", script])
        return name
    raise HostError("No terminal program found (tried gnome-terminal, konsole, xterm and others)")


def clipboard_text():
    """The text on this computer's clipboard."""
    if MAC:
        cmds = [["pbpaste"]]
    elif WINDOWS:
        cmds = [["powershell.exe", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"]]
    else:
        cmds = [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]]
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        if r.returncode == 0:
            text = r.stdout.decode("utf-8", errors="replace")
            return text[:-2] if WINDOWS and text.endswith("\r\n") else text
    if LINUX:
        raise HostError("Can't read the clipboard: install wl-clipboard (Wayland) or xclip (X11)")
    raise HostError("Can't read the clipboard")


def ssh_hostname(alias):
    """The real host name an ssh alias points at (`ssh -G`), for non-SSH clients like RDP."""
    try:
        out = subprocess.run(["ssh", "-G", alias], capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return alias
    for line in out.splitlines():
        if line.startswith("hostname "):
            return line.split(None, 1)[1].strip()
    return alias


# Apps the UI can hand off to, per platform: (installed-check, launch argv) pairs,
# and where to get the app when none is installed.
def open_steam_link():
    if MAC:
        if subprocess.run(["open", "-a", "Steam Link"], capture_output=True).returncode == 0:
            return "Opened Steam Link"
    elif WINDOWS:
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            exe = base and os.path.join(base, "Steam Link", "SteamLink.exe")
            if exe and os.path.isfile(exe):
                _spawn([exe])
                return "Opened Steam Link"
    else:
        if which("steamlink"):
            _spawn([which("steamlink")])
            return "Opened Steam Link"
        if which("flatpak") and subprocess.run(["flatpak", "info", "com.valvesoftware.SteamLink"],
                                               capture_output=True).returncode == 0:
            _spawn(["flatpak", "run", "com.valvesoftware.SteamLink"])
            return "Opened Steam Link"
    open_url("https://store.steampowered.com/remoteplay")
    return "Steam Link isn't installed; opened its download page"


RDP_PORT = 3389
RDP_USER = "steamos"  # xrdp signs in with the Developer Mode password, not this computer's
# xrdp's certificate is its own, so every client warns about it first.
RDP_LOGIN = (f"accept the warning about the Frame's certificate, then sign in as {RDP_USER} "
             "with your Developer Mode password")


def rdp_reachable(host, timeout=3):
    """Whether anything answers on the Frame's RDP port."""
    try:
        with socket.create_connection((host, RDP_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def rdp_file(host):
    """A Remote Desktop connection file for the Frame. mstsc /v: alone offers this
    computer's Windows account, which xrdp turns away; the file names steamos instead."""
    if any(c in host for c in "\r\n"):
        raise HostError("That headset address can't be used for remote desktop")
    # One file per address, so two launches close together can't swap headsets.
    path = cache_dir(f"frame-{re.sub(r'[^A-Za-z0-9.-]', '_', host)}.rdp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:  # Path.write_text(newline=) is 3.10+
        f.write(f"full address:s:{host}\nusername:s:{RDP_USER}\n")
    return path


def open_rdp(alias, host=None):
    """Remote desktop to the Frame's xrdp (user steamos), at `host` or where the alias points."""
    host = host or ssh_hostname(alias)
    # The client would open either way and then fail on its own, with nothing said here.
    if not rdp_reachable(host):
        raise NotListening(f"The Frame isn't accepting remote desktop at {host} (nothing answered on port "
                           f"{RDP_PORT}). Turn on Developer Mode in Steam Settings > System on the headset, "
                           "then restart it and try again.")
    if MAC:
        if subprocess.run(["open", "-a", "Windows App"], capture_output=True).returncode == 0:
            return f"Opened Windows App: connect to {host} and {RDP_LOGIN}"
        open_url("https://apps.apple.com/app/windows-app/id1295203466")
        return "Windows App isn't installed; opened its App Store page"
    if WINDOWS:
        _spawn(["mstsc.exe", str(rdp_file(host))])
        # Windows asks about the unsigned connection file first.
        return f"Opened Remote Desktop to {host}: choose Connect, {RDP_LOGIN}"
    if which("remmina"):
        _spawn(["remmina", "-c", f"rdp://{RDP_USER}@{host}"])
        return f"Opened Remmina to {host}: {RDP_LOGIN}"
    for name in ("xfreerdp3", "xfreerdp"):
        if which(name):
            _spawn([name, f"/v:{host}", f"/u:{RDP_USER}", "/dynamic-resolution"])
            return f"Opened FreeRDP to {host}: {RDP_LOGIN}"
    raise HostError("No RDP client found: install Remmina or FreeRDP")


def main(argv):
    if len(argv) >= 3 and argv[0] == "terminal" and argv[1] == "--":
        try:
            print(f"Opened {open_terminal(argv[2:])}")
        except HostError as e:
            sys.exit(str(e))
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
