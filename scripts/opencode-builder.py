#!/usr/bin/env python3
"""Install and launch pinned OpenCode 2.x with isolated account profiles."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.0.29"
PACKAGE_URL = f"https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-{VERSION}.tgz"
PACKAGE_SRI = "sha512-pdG/T4uBxSC3hjUTOrTlx2pXlZl2VqABlb0AgDlZ8VO/q1pUUOYvooWmN5VeSi+EP8cKGVUsj0GUmMDeB2ympA=="
PROFILES = ("primary", "secondary")


class BootstrapError(Exception):
    pass


def profile_root(profile: str, state_root: Path | None = None) -> Path:
    if profile not in PROFILES:
        raise BootstrapError("Choose primary or secondary profile.")
    return (state_root or ROOT / ".builder/opencode-profiles") / profile


def profile_environment(profile: str, base_env: dict[str, str] | None = None,
                        state_root: Path | None = None) -> dict[str, str]:
    root = profile_root(profile, state_root).absolute()
    home = root / "home"
    paths = (root, home, root / "xdg/config", root / "xdg/data",
             root / "xdg/state", root / "xdg/cache", root / "tmp")
    for path in paths:
        for parent in (path, *path.parents):
            if parent.exists() and parent.is_symlink():
                raise BootstrapError("Profile path must not traverse a symlink.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise BootstrapError("Profile path must be owned and non-symlink.")
        os.chmod(path, 0o700)

    source = os.environ if base_env is None else base_env
    env: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "TERM", "COLORTERM", "NO_COLOR",
                 "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                 "XDG_RUNTIME_DIR"):
        if source.get(name):
            env[name] = source[name]
    env.update({
        "HOME": str(home),
        "USER": source.get("USER", str(os.getuid())),
        "LOGNAME": source.get("LOGNAME", str(os.getuid())),
        "SHELL": "/bin/sh",
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "xdg/config"),
        "XDG_DATA_HOME": str(root / "xdg/data"),
        "XDG_STATE_HOME": str(root / "xdg/state"),
        "XDG_CACHE_HOME": str(root / "xdg/cache"),
    })
    return env


def runtime_root(explicit: str | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else ROOT / ".builder/opencode-runtime"


def verify_binary(runtime: Path) -> Path:
    directory = runtime / f"v{VERSION}"
    binary, receipt = directory / "opencode", directory / "install.json"
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise BootstrapError(f"OpenCode v{VERSION} is not installed in the private runtime.")
    if receipt.is_symlink() or not receipt.is_file():
        raise BootstrapError("OpenCode install receipt is missing.")
    try:
        metadata = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BootstrapError("OpenCode install receipt is invalid.") from None
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    expected = {"version": VERSION, "package_sri": PACKAGE_SRI, "binary_sha256": binary_sha256}
    if metadata != expected:
        raise BootstrapError("OpenCode install receipt does not match the binary.")
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20)
    if result.returncode or VERSION not in result.stdout:
        raise BootstrapError("Private OpenCode binary did not report its pinned version.")
    return binary


def install(runtime: Path) -> Path:
    target = runtime / f"v{VERSION}"
    if target.exists() or target.is_symlink():
        raise BootstrapError("Pinned runtime already exists; refusing to overwrite it.")
    request = urllib.request.Request(PACKAGE_URL, headers={"User-Agent": "project-opencode-bootstrap"})
    with urllib.request.urlopen(request, timeout=90) as response:
        archive_data = response.read(110_000_001)
    if len(archive_data) > 110_000_000:
        raise BootstrapError("OpenCode package exceeded the download size limit.")
    actual_sri = "sha512-" + base64.b64encode(hashlib.sha512(archive_data).digest()).decode("ascii")
    if actual_sri != PACKAGE_SRI:
        raise BootstrapError("Official OpenCode npm package failed its pinned SHA-512 integrity check.")
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
        members = archive.getmembers()
        files = {m.name: m for m in members if m.isfile()}
        expected_names = {"package/package.json", "package/bin/opencode"}
        if set(files) != expected_names or len(members) != 2:
            raise BootstrapError("OpenCode package has unexpected files.")
        if files["package/bin/opencode"].size > 250_000_000:
            raise BootstrapError("OpenCode binary exceeded the extraction size limit.")
        package_stream = archive.extractfile(files["package/package.json"])
        binary_stream = archive.extractfile(files["package/bin/opencode"])
        if package_stream is None or binary_stream is None:
            raise BootstrapError("OpenCode package is incomplete.")
        package = json.loads(package_stream.read(10_000).decode("utf-8"))
        if package.get("name") != "@opencode/cli-linux-x64" or package.get("version") != VERSION:
            raise BootstrapError("OpenCode package identity did not match the pin.")
        binary_data = binary_stream.read(250_000_001)
        if len(binary_data) != files["package/bin/opencode"].size:
            raise BootstrapError("OpenCode binary size did not match the package metadata.")

    runtime = runtime.absolute()
    for parent in (runtime, *runtime.parents):
        if parent.exists() and parent.is_symlink():
            raise BootstrapError("Runtime path must not traverse a symlink.")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != os.getuid():
        raise BootstrapError("Runtime path must be an owned, non-symlink directory.")
    os.chmod(runtime, 0o700)
    target.mkdir(mode=0o700)
    binary = target / "opencode"
    fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "wb") as output:
        output.write(binary_data)
        output.flush()
        os.fsync(output.fileno())
    receipt = {"version": VERSION, "package_sri": PACKAGE_SRI,
               "binary_sha256": hashlib.sha256(binary_data).hexdigest()}
    fd = os.open(target / "install.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(receipt, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return verify_binary(runtime)


def launch(profile: str, runtime: Path) -> int:
    binary = verify_binary(runtime)
    env = profile_environment(profile)
    # A profile-local server keeps background server state and auth isolated too.
    return subprocess.call([str(binary), "--standalone"], cwd=ROOT, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "launch"))
    parser.add_argument("--profile", choices=PROFILES, default="primary")
    parser.add_argument("--runtime-root", help="Defaults to <repo>/.builder/opencode-runtime")
    args = parser.parse_args()
    runtime = runtime_root(args.runtime_root)
    try:
        if args.command == "install":
            binary = install(runtime)
            print(json.dumps({"version": VERSION, "binary": str(binary), "package_sri": PACKAGE_SRI}))
            return 0
        return launch(args.profile, runtime)
    except (BootstrapError, OSError, tarfile.TarError, ValueError) as error:
        print(f"OpenCode bootstrap blocked: {error}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
