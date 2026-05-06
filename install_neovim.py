#!/usr/bin/env python3
"""
install_neovim.py

Install Neovim from the latest official GitHub binary release, then install
LazyVim and patch the Telescope config to show hidden files.

Targets:
    Windows 10/11 (x86_64), macOS (arm64 / x86_64), Linux (x86_64 / arm64).

Requirements:
    Python 3.8+, git on PATH, network access to api.github.com and github.com.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

LOG = logging.getLogger("install-nvim")

NVIM_LATEST_API = "https://api.github.com/repos/neovim/neovim/releases/latest"
LAZYVIM_STARTER_REPO = "https://github.com/LazyVim/starter"


# ---------------------------------------------------------------------------
# Target detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    os_name: str                       # "windows" | "macos" | "linux"
    arch: str                          # "x86_64" | "arm64"
    asset_patterns: Tuple[str, ...]    # substrings to look for, in priority order
    archive_kind: str                  # "zip" | "tar.gz"


TARGETS = {
    ("windows", "x86_64"): Target(
        "windows", "x86_64",
        ("nvim-win64.zip", "win64.zip"),
        "zip",
    ),
    ("macos", "arm64"): Target(
        "macos", "arm64",
        ("nvim-macos-arm64.tar.gz", "macos-arm64.tar.gz"),
        "tar.gz",
    ),
    ("macos", "x86_64"): Target(
        "macos", "x86_64",
        ("nvim-macos-x86_64.tar.gz", "macos-x86_64.tar.gz"),
        "tar.gz",
    ),
    ("linux", "x86_64"): Target(
        "linux", "x86_64",
        # Asset naming has shifted across recent releases; try newer names first.
        ("nvim-linux-x86_64.tar.gz", "nvim-linux64.tar.gz",
         "linux-x86_64.tar.gz", "linux64.tar.gz"),
        "tar.gz",
    ),
    ("linux", "arm64"): Target(
        "linux", "arm64",
        ("nvim-linux-arm64.tar.gz", "linux-arm64.tar.gz"),
        "tar.gz",
    ),
}


def detect_target() -> Optional[Target]:
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if sys_name.startswith("win"):
        return TARGETS.get(("windows", "x86_64"))
    if sys_name == "darwin":
        if machine in ("arm64", "aarch64"):
            return TARGETS[("macos", "arm64")]
        if machine in ("x86_64", "amd64"):
            return TARGETS[("macos", "x86_64")]
    if sys_name == "linux":
        if machine in ("aarch64", "arm64"):
            return TARGETS[("linux", "arm64")]
        if machine in ("x86_64", "amd64"):
            return TARGETS[("linux", "x86_64")]
    return None


def prompt_target() -> Target:
    print()
    print("Could not auto-detect a supported OS/architecture.")
    print("Please choose a target:")
    options = [
        ("Windows (x86_64)",      ("windows", "x86_64")),
        ("macOS (Apple Silicon)", ("macos",   "arm64")),
        ("macOS (Intel)",         ("macos",   "x86_64")),
        ("Linux (x86_64)",        ("linux",   "x86_64")),
        ("Linux (ARM64)",         ("linux",   "arm64")),
    ]
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}) {label}")
    while True:
        choice = input(f"Select [1-{len(options)}] or q to quit: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return TARGETS[options[int(choice) - 1][1]]
        print("Invalid selection.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class InstallError(RuntimeError):
    """Raised on user-facing install failures."""


def die(msg: str, code: int = 1) -> None:
    LOG.error(msg)
    sys.exit(code)


def require_git() -> None:
    if shutil.which("git") is None:
        die("git is required but was not found on PATH. "
            "Install git first, then re-run this script.")


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "install-neovim.py"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise InstallError(f"Failed to reach {url}: {e.reason}") from e


def http_download(url: str, dest: Path) -> None:
    LOG.info("Downloading %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "install-neovim.py"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.URLError as e:
        raise InstallError(f"Download failed: {e.reason}") from e


def find_release_asset(release: dict, patterns: Tuple[str, ...]) -> Tuple[str, str]:
    assets = release.get("assets", [])
    for pat in patterns:
        for asset in assets:
            if pat in asset["name"]:
                return asset["name"], asset["browser_download_url"]
    available = ", ".join(a["name"] for a in assets) or "<none>"
    raise InstallError(
        f"No matching release asset for patterns {patterns}.\n"
        f"Available assets: {available}"
    )


def _single_top_level(names) -> str:
    tops = {n.split("/", 1)[0].rstrip("\\") for n in names if n and not n.startswith("/")}
    tops.discard("")
    if len(tops) != 1:
        raise InstallError(f"Archive does not have a single top-level directory: {tops}")
    return next(iter(tops))


def extract_archive(archive: Path, into: Path, kind: str) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            top = _single_top_level(zf.namelist())
            zf.extractall(into)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            if not members:
                raise InstallError("Archive is empty.")
            top = _single_top_level(m.name for m in members)
            try:
                tf.extractall(into, filter="data")  # Python 3.12+
            except TypeError:
                tf.extractall(into)
    extracted = into / top
    if not extracted.is_dir():
        raise InstallError(
            f"Extraction did not produce expected top-level directory: {extracted}"
        )
    return extracted


# ---------------------------------------------------------------------------
# Privilege detection
# ---------------------------------------------------------------------------

def is_admin_windows() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_root_unix() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def relaunch_as_admin_windows() -> bool:
    """
    Re-launch the current script under UAC elevation.

    Returns True if a new elevated process was successfully started (the
    caller should then exit). Returns False if elevation was declined by
    the user or unavailable, in which case the caller should continue
    running unprivileged.
    """
    # Pass a sentinel flag so the elevated process does not try to elevate again.
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params_list = list(sys.argv[1:]) + ["--elevated-relaunch"]
    else:
        executable = sys.executable
        params_list = [sys.argv[0]] + list(sys.argv[1:]) + ["--elevated-relaunch"]

    params = subprocess.list2cmdline(params_list)

    SW_SHOWNORMAL = 1
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, SW_SHOWNORMAL,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning("Failed to invoke UAC elevation: %s", e)
        return False

    # ShellExecuteW returns an HINSTANCE cast to int.
    # Values > 32 indicate success; <= 32 indicate failure.
    # SE_ERR_ACCESSDENIED (5) is what you get when the user clicks "No" on UAC.
    if ret <= 32:
        LOG.warning("UAC elevation declined or unavailable (code %s).", ret)
        return False
    return True


# ---------------------------------------------------------------------------
# Install paths
# ---------------------------------------------------------------------------

def system_install_dir(target: Target) -> Path:
    if target.os_name == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return Path(program_files) / "Neovim"
    if target.os_name == "macos":
        return Path("/Applications/Neovim")
    return Path("/opt/nvim")  # /usr/bin holds individual binaries; /opt is the conventional home for self-contained trees.


def user_install_dir(target: Target) -> Path:
    home = Path.home()
    if target.os_name == "windows":
        return home / ".bin" / "Neovim"
    return home / ".bin" / "nvim"


def bin_dir_for(install_dir: Path) -> Path:
    return install_dir / "bin"


def installed_nvim_version(install_dir: Path, target: Target) -> Optional[str]:
    """
    Return the version token reported by `nvim --version` (e.g. 'v0.10.2')
    if a working binary exists under `install_dir/bin`, else None.
    """
    nvim_exe = "nvim.exe" if target.os_name == "windows" else "nvim"
    binary = install_dir / "bin" / nvim_exe
    if not binary.exists():
        return None
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    parts = first_line.strip().split()
    if len(parts) >= 2 and parts[0].upper() == "NVIM":
        return parts[1]
    return None


def find_existing_install(target: Target,
                          expected_tag: str) -> Optional[Tuple[Path, bool]]:
    """
    If Neovim with version `expected_tag` is already installed at the system
    or user location, return (install_dir, system_wide). Else None.

    Prefers the system location if both happen to satisfy the version match.
    """
    if not expected_tag:
        return None
    sys_dir = system_install_dir(target)
    user_dir = user_install_dir(target)

    sys_ver = installed_nvim_version(sys_dir, target)
    if sys_ver and sys_ver == expected_tag:
        return sys_dir, True
    user_ver = installed_nvim_version(user_dir, target)
    if user_ver and user_ver == expected_tag:
        return user_dir, False
    return None


# ---------------------------------------------------------------------------
# Move / install with optional privilege escalation
# ---------------------------------------------------------------------------

def _replace_dir_no_sudo(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _replace_dir_sudo(src: Path, dest: Path) -> None:
    if dest.exists():
        subprocess.run(["sudo", "rm", "-rf", str(dest)], check=True)
    subprocess.run(["sudo", "mkdir", "-p", str(dest.parent)], check=True)
    subprocess.run(["sudo", "mv", str(src), str(dest)], check=True)


def install_to_dir(src: Path, dest: Path, *, allow_sudo: bool) -> bool:
    try:
        _replace_dir_no_sudo(src, dest)
        return True
    except (PermissionError, OSError) as e:
        if not allow_sudo:
            LOG.debug("Direct install to %s failed: %s", dest, e)
            return False
    try:
        _replace_dir_sudo(src, dest)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        LOG.debug("sudo install to %s failed: %s", dest, e)
        return False


def place_neovim(extracted: Path, target: Target) -> Tuple[Path, bool]:
    """
    Place the extracted Neovim folder. Returns (final_install_dir, system_wide).
    Tries the system location first; falls back to a user-local directory.
    """
    sys_dir = system_install_dir(target)
    user_dir = user_install_dir(target)

    if target.os_name == "windows":
        if is_admin_windows():
            LOG.info("Running as admin. Installing to %s", sys_dir)
            if install_to_dir(extracted, sys_dir, allow_sudo=False):
                return sys_dir, True
            LOG.warning("Admin install to %s failed; falling back to user install.", sys_dir)
        else:
            LOG.info("Not running as admin; installing for the current user only.")
    else:
        LOG.info("Attempting system-wide install to %s "
                 "(you may be prompted for your password).", sys_dir)
        allow_sudo = not is_root_unix()
        if install_to_dir(extracted, sys_dir, allow_sudo=allow_sudo):
            return sys_dir, True
        LOG.warning("System-wide install failed; falling back to user install.")

    if install_to_dir(extracted, user_dir, allow_sudo=False):
        return user_dir, False
    raise InstallError(
        f"Could not install Neovim to {sys_dir} or {user_dir}. "
        f"Check filesystem permissions and disk space."
    )


# ---------------------------------------------------------------------------
# PATH updates
# ---------------------------------------------------------------------------

def update_path_windows(bin_dir: Path, system_wide: bool) -> None:
    import winreg  # Windows only

    if system_wide:
        root = winreg.HKEY_LOCAL_MACHINE
        sub = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        scope = "system"
    else:
        root = winreg.HKEY_CURRENT_USER
        sub = "Environment"
        scope = "user"

    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            entries = [p for p in current.split(";") if p]
            target_str = str(bin_dir).rstrip("\\")
            if any(e.rstrip("\\").lower() == target_str.lower() for e in entries):
                LOG.info("%s PATH already contains %s", scope, bin_dir)
                return
            entries.append(str(bin_dir))
            new_value = ";".join(entries)
            winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ, new_value)
    except PermissionError as e:
        raise InstallError(
            f"Permission denied updating the {scope} PATH. "
            f"Re-run this script as administrator, or do a user-only install."
        ) from e

    # Notify other processes (Explorer, new shells) that the environment changed.
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
    )
    LOG.info("Added %s to %s PATH.", bin_dir, scope)


def update_path_unix(bin_dir: Path) -> None:
    zshrc = Path.home() / ".zshrc"
    bin_str = str(bin_dir)
    if zshrc.exists() and bin_str in zshrc.read_text(encoding="utf-8", errors="replace"):
        LOG.info("%s already references %s", zshrc, bin_dir)
        return

    block = (
        "\n# >>> neovim install script >>>\n"
        f'export PATH="{bin_str}:$PATH"\n'
        "# <<< neovim install script <<<\n"
    )
    try:
        with zshrc.open("a", encoding="utf-8") as f:
            f.write(block)
    except OSError as e:
        raise InstallError(f"Failed to write to {zshrc}: {e}") from e
    LOG.info("Appended PATH entry to %s", zshrc)

    if os.environ.get("SHELL", "").endswith("bash"):
        LOG.warning(
            "Your current shell appears to be bash. The script wrote to ~/.zshrc "
            "as requested; if you primarily use bash, append the same export line to ~/.bashrc."
        )


# ---------------------------------------------------------------------------
# Verify install
# ---------------------------------------------------------------------------

def verify_neovim(bin_dir: Path, target: Target) -> Path:
    nvim_exe = "nvim.exe" if target.os_name == "windows" else "nvim"
    binary = bin_dir / nvim_exe
    if not binary.exists():
        raise InstallError(f"Neovim binary missing at {binary}.")
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise InstallError(f"Neovim binary at {binary} failed to run: {e}") from e
    first_line = result.stdout.splitlines()[0] if result.stdout else "(no output)"
    LOG.info("Neovim is installed: %s", first_line)
    return binary


# ---------------------------------------------------------------------------
# LazyVim install
# ---------------------------------------------------------------------------

def lazyvim_paths(target: Target) -> dict:
    home = Path.home()
    if target.os_name == "windows":
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        return {
            "config": local / "nvim",
            "data":   local / "nvim-data",
        }
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    xdg_data   = Path(os.environ.get("XDG_DATA_HOME",   str(home / ".local" / "share")))
    xdg_state  = Path(os.environ.get("XDG_STATE_HOME",  str(home / ".local" / "state")))
    xdg_cache  = Path(os.environ.get("XDG_CACHE_HOME",  str(home / ".cache")))
    return {
        "config": xdg_config / "nvim",
        "data":   xdg_data   / "nvim",
        "state":  xdg_state  / "nvim",
        "cache":  xdg_cache  / "nvim",
    }


def backup_path(p: Path) -> None:
    if not p.exists():
        return
    candidate = p.with_name(p.name + ".bak")
    i = 1
    while candidate.exists():
        candidate = p.with_name(f"{p.name}.bak.{i}")
        i += 1
    LOG.info("Backing up existing %s -> %s", p, candidate)
    p.rename(candidate)


def lazyvim_already_installed(config_dir: Path) -> bool:
    """
    Heuristic: the LazyVim starter places its bootstrapping in
    lua/config/lazy.lua. If that file exists, treat LazyVim as installed.
    """
    return (config_dir / "lua" / "config" / "lazy.lua").is_file()


def install_lazyvim(target: Target) -> Path:
    paths = lazyvim_paths(target)
    config_dir = paths["config"]

    if lazyvim_already_installed(config_dir):
        LOG.info("LazyVim starter already present at %s; skipping clone.",
                 config_dir)
        return config_dir

    for p in paths.values():
        backup_path(p)
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Cloning LazyVim starter into %s", config_dir)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", LAZYVIM_STARTER_REPO, str(config_dir)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise InstallError(f"git clone of LazyVim starter failed: {e}") from e

    git_dir = config_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)
    return config_dir


# ---------------------------------------------------------------------------
# Telescope override
# ---------------------------------------------------------------------------

TELESCOPE_OVERRIDE_LUA = '''-- Generated by install_neovim.py
-- Show hidden files in Telescope's find_files and live_grep pickers.
-- (.git/ is still excluded; remove the --glob/!.git line below to include it.)
return {
  {
    "nvim-telescope/telescope.nvim",
    opts = {
      defaults = {
        vimgrep_arguments = {
          "rg",
          "--color=never",
          "--no-heading",
          "--with-filename",
          "--line-number",
          "--column",
          "--smart-case",
          "--hidden",
          "--glob=!.git/",
        },
      },
      pickers = {
        find_files = {
          hidden = true,
        },
      },
    },
  },
}
'''


def configure_telescope_hidden(config_dir: Path) -> Path:
    plugins_dir = config_dir / "lua" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    out = plugins_dir / "telescope.lua"
    if out.is_file():
        try:
            current = out.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == TELESCOPE_OVERRIDE_LUA:
            LOG.info("Telescope override already present at %s; no changes needed.",
                     out)
            return out
        LOG.info("Existing %s differs from generated content; overwriting.", out)
    out.write_text(TELESCOPE_OVERRIDE_LUA, encoding="utf-8")
    LOG.info("Wrote Telescope override to %s", out)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )


def print_followup(target: Target, bin_dir: Path, *, lazyvim_skipped: bool) -> None:
    print()
    print("=" * 60)
    print("Neovim install complete.")
    print("=" * 60)
    print(f"Binary directory: {bin_dir}")
    if target.os_name == "windows":
        print("Open a new terminal window so the updated PATH takes effect, then run:")
    else:
        print("Open a new shell or run `source ~/.zshrc`, then run:")
    print("    nvim --version")
    if not lazyvim_skipped:
        print()
        print("On first launch, LazyVim will bootstrap lazy.nvim and install plugins.")
        print("Telescope is configured to show hidden files (find_files & live_grep).")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install Neovim + LazyVim with Telescope hidden-files enabled.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")
    parser.add_argument("--skip-lazyvim", action="store_true",
                        help="Install only the Neovim binary; skip LazyVim and Telescope steps.")
    # Internal: set by the script itself when relaunching with UAC elevation
    # on Windows. Prevents an infinite elevation loop.
    parser.add_argument("--elevated-relaunch", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    setup_logging(args.verbose)

    # On Windows, request UAC elevation up front so we can install to
    # Program Files and write the system PATH. If the user declines the
    # UAC prompt, fall through and install user-local instead.
    if (platform.system().lower().startswith("win")
            and not is_admin_windows()
            and not args.elevated_relaunch):
        LOG.info("Requesting administrator privileges for system-wide install...")
        if relaunch_as_admin_windows():
            LOG.info("Elevated process launched. This window will exit.")
            return 0
        LOG.warning("Continuing without elevation; will install for the current user only.")

    require_git()

    target = detect_target()
    if target is None:
        LOG.warning("OS auto-detection failed (system=%s, machine=%s).",
                    platform.system(), platform.machine())
        target = prompt_target()
    LOG.info("Target: %s/%s", target.os_name, target.arch)

    try:
        release = http_get_json(NVIM_LATEST_API)
    except InstallError as e:
        die(str(e))

    target_tag = release.get("tag_name", "")
    LOG.info("Latest Neovim release: %s", target_tag or "(unknown)")

    existing = find_existing_install(target, target_tag)
    if existing is not None:
        install_dir, system_wide = existing
        LOG.info("Neovim %s is already installed at %s (%s); "
                 "skipping download and extraction.",
                 target_tag, install_dir,
                 "system-wide" if system_wide else "user-local")
        if (target.os_name == "windows" and not system_wide
                and is_admin_windows()):
            LOG.info("Note: existing install is user-local but the script is "
                     "running with admin rights. To migrate to %s, delete %s "
                     "and re-run.", system_install_dir(target), install_dir)
    else:
        try:
            asset_name, asset_url = find_release_asset(release, target.asset_patterns)
        except InstallError as e:
            die(str(e))

        with tempfile.TemporaryDirectory(prefix="nvim-install-") as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / asset_name
            try:
                http_download(asset_url, archive)
                extracted = extract_archive(archive, tmp_path / "extracted",
                                            target.archive_kind)
            except InstallError as e:
                die(str(e))

            try:
                install_dir, system_wide = place_neovim(extracted, target)
            except InstallError as e:
                die(str(e))

    LOG.info("Neovim is at %s (%s).",
             install_dir, "system-wide" if system_wide else "user-local")

    bin_dir = bin_dir_for(install_dir)
    try:
        if target.os_name == "windows":
            update_path_windows(bin_dir, system_wide=system_wide)
        else:
            update_path_unix(bin_dir)
    except InstallError as e:
        LOG.error(str(e))
        LOG.error("Neovim is installed at %s but PATH was not updated. "
                  "Add this directory to your PATH manually.", bin_dir)
        return 2

    try:
        verify_neovim(bin_dir, target)
    except InstallError as e:
        die(str(e))

    if args.skip_lazyvim:
        print_followup(target, bin_dir, lazyvim_skipped=True)
        return 0

    try:
        config_dir = install_lazyvim(target)
        configure_telescope_hidden(config_dir)
    except InstallError as e:
        LOG.error(str(e))
        LOG.error("Neovim is installed but LazyVim setup failed. "
                  "Re-run this script or install LazyVim manually.")
        return 3

    print_followup(target, bin_dir, lazyvim_skipped=False)
    return 0


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130
    finally:
        # When relaunched via UAC, this script runs in a new console window
        # that closes the moment the process exits. Pause so the user can
        # read the result.
        if "--elevated-relaunch" in sys.argv:
            try:
                input("\nPress Enter to close this window...")
            except EOFError:
                pass
    sys.exit(exit_code)
