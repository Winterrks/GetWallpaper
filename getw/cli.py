"""getw — command line entry point for GetWallpaper."""

import argparse
import os
import shutil
import subprocess
import sys

from . import __version__, we_source


def _steamcmd():
    return shutil.which("steamcmd") or shutil.which("steamcmd.exe")


def _install_hint():
    if sys.platform == "darwin":
        return "  macOS:   brew install steamcmd"
    if os.name == "nt":
        return ("  Windows: download SteamCMD from "
                "https://developer.valvesoftware.com/wiki/SteamCMD "
                "and add it to PATH")
    return ("  Linux:   install SteamCMD (distro package or "
            "https://developer.valvesoftware.com/wiki/SteamCMD)")


def _run_login(account):
    exe = _steamcmd()
    if not exe:
        print("steamcmd not found. Install it first:")
        print(_install_hint())
        return 1
    print("Running: steamcmd +login %s +quit" % account)
    print("Enter your Steam password / Guard code when prompted.")
    return subprocess.run([exe, "+login", account, "+quit"]).returncode


def _cmd_start(args):
    from . import webui
    webui.start(port=args.port, open_browser=not args.no_browser)
    return 0


def _cmd_setup(args):
    from . import webui

    print("GetWallpaper setup")
    print("  data dir:        %s" % webui.STATE_DIR)
    print("  download folder: %s" % webui.DOWNLOAD_ROOT)
    answer = input("New download folder [Enter to keep]: ").strip()
    if answer:
        try:
            print("  saved: %s" % webui.set_download_root(answer))
        except OSError as exc:
            print("  could not use that folder: %s" % exc)

    exe = _steamcmd()
    print()
    if not exe:
        print("SteamCMD: not installed (needed only for Wallpaper Engine downloads)")
        print(_install_hint())
        print("iWallpaper needs no Steam account.")
        return 0

    print("SteamCMD: %s" % exe)
    account = we_source._steam_account()
    print("Steam account: %s" % (account or "not detected"))
    if account:
        print("Cached session detected. If downloads fail, run: getw login")
        return 0
    if input("Log in to Steam now? [y/N]: ").strip().lower().startswith("y"):
        name = input("Steam account name: ").strip()
        if name:
            return _run_login(name)
    return 0


def _cmd_login(args):
    account = args.account or we_source._steam_account()
    if not account:
        account = input("Steam account name: ").strip()
    if not account:
        print("No account given.")
        return 1
    return _run_login(account)


def _dir_size(path):
    total = 0
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if os.path.isfile(full):
                total += os.path.getsize(full)
    except OSError:
        pass
    return total


def _cmd_status(args):
    from . import webui

    print("GetWallpaper %s" % __version__)
    print("  data dir:         %s" % webui.STATE_DIR)
    print("  download folder:  %s" % webui.DOWNLOAD_ROOT)
    print("  iWallpaper files: %d downloaded" % len(webui.INDEX))
    print("  Workshop files:   %d downloaded" % len(webui.WE_INDEX))
    print("  iWallpaper thumbs: %.1f MB" % (_dir_size(webui.THUMB_DIR) / 1e6))
    print("  Workshop thumbs:   %.1f MB" % (_dir_size(we_source.THUMB_DIR) / 1e6))
    exe = _steamcmd()
    print("  steamcmd:         %s" % (exe or "not installed"))
    print("  steam account:    %s" % (
        we_source._steam_account() if exe else "n/a (steamcmd missing)"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="getw", description="GetWallpaper — live wallpaper gallery")
    parser.add_argument("--version", action="version",
                        version="getw " + __version__)
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="run the web UI")
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--no-browser", action="store_true")
    start.set_defaults(func=_cmd_start)

    setup = sub.add_parser("setup", help="choose the download folder, check SteamCMD")
    setup.set_defaults(func=_cmd_setup)

    login = sub.add_parser("login", help="log SteamCMD in (needed for Workshop downloads)")
    login.add_argument("--account", help="Steam account name")
    login.set_defaults(func=_cmd_login)

    status = sub.add_parser("status", help="show configuration and caches")
    status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
