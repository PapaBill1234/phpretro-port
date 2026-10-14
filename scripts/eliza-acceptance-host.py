#!/usr/bin/env python3
"""Isolated PRODUCTION full-Eliza acceptance host: thin adapter over the
reviewed dashboard supervisor (``scripts/eliza-dashboard.py``).

This does not reimplement supervision.  It registers one immutable
``ACCEPTANCE_PROFILE`` and delegates ``start``/``stop``/``status``/``check`` to
the reviewed ``eliza-dashboard`` helpers, so the OS lock, captured cutoff,
partial-startup group cleanup, PID start-tick signalling and private filesystem
are shared, not duplicated.  It owns only ``.builder/eliza-acceptance``.

No install, download, build, launch, browser, model, account or credential call
happens here; ``check`` is read-only and ``start`` is where the root later runs
the real host after review.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load("eliza_dashboard", _HERE / "eliza-dashboard.py")
upstream = _load("eliza_upstream", _HERE / "eliza-upstream.py")

DashboardError = dash.DashboardError
UPSTREAM_PIN = upstream.UPSTREAM_PIN
SOURCE_REL = upstream.SOURCE_REL

ROOT = Path(__file__).resolve().parents[1]  # fixed; no arbitrary CLI root
ACCEPTANCE_REL = Path(".builder") / "eliza-acceptance"
PATCH_REL = Path("builder") / "eliza" / "patches" / "new-chat.patch"
PATCH_SHA256 = "caf755bed336361427796f1d33d088013c097f8dbc99c497cc0b27b52804ffb9"
ENTRY_REL = Path("packages") / "app" / "src" / "entry.ts"

API_HOST = "127.0.0.1"
API_PORT = 18831
UI_PORT = 18832
ALLOWED_ORIGINS = "http://127.0.0.1:%d,http://127.0.0.1:%d" % (UI_PORT, API_PORT)

# The only paths the reviewed patch may change.  Any other overlay difference
# from the pinned canonical checkout is treated as an unknown change and refused.
EXPECTED_CHANGED = frozenset({
    "packages/ui/src/components/shell/ChatOverlay.tsx",
})
EXPECTED_ADDED = frozenset({
    "packages/ui/src/components/shell/__e2e__/new-chat.test.tsx",
    "packages/ui/src/components/shell/useNewChatAction.ts",
})

# Physical drop-ins: a REAL package directory under the fresh state's
# plugins/custom, never a symlinked directory (drop-in discovery skips symlink
# dirs).  Each drop-in carries its original package id and a small physical
# index that imports the fixed verified CANONICAL module in place via a file
# URL, so the package's own relative layout (e.g. native-model/transport.mjs
# resolving ``../../../scripts/codex-generate.py``, and the companion root
# layout) stays valid.  The whole module is never copied out of its layout.
DROPINS = {
    "@phpretro/plugin-project-companion": {
        "canonical": Path("builder") / "eliza" / "upstream-plugin",
        "entry": Path("src") / "index.js",
    },
    "@phpretro/eliza-native-model": {
        "canonical": Path("builder") / "eliza" / "native-model",
        "entry": Path("index.mjs"),
    },
}

_DROPIN_INDEX = (
    "// Physical drop-in: re-exports the verified canonical module in place so\n"
    "// the package's own relative layout (e.g. ../../../scripts) stays valid.\n"
    'export * from "%s";\n'
    'import * as canonical from "%s";\n'
    "export { canonical };\n"
    "// The pinned loader requires a capability array even for model-only plugins.\n"
    "export const plugin = { ...canonical.default, providers: canonical.default.providers ?? [] };\n"
    "export default plugin;\n"
)
DEFAULT_WINDOW_S = 3600
SETUP_BUDGET_S = 240

# Verified canonical runtimes (reused from the pinned upstream installer layout).
_NODE = ROOT / ".builder" / "upstream" / "runtimes" / "node-v24.15.0-linux-x64" / "bin" / "node"
_BUN = ROOT / ".builder" / "upstream" / "runtimes" / "bun" / "package" / "bin" / "bun"


class AcceptanceError(DashboardError):
    """A fail-closed refusal safe to print."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --- private layout ---------------------------------------------------------
def acceptance_root(root):
    return Path(root) / ACCEPTANCE_REL


def overlay_dir(root):
    return acceptance_root(root) / "source"


def state_dir(root):
    return acceptance_root(root) / "state"


def config_path(root):
    return state_dir(root) / "runtime" / "eliza.json"


def cutoff_path(root):
    return acceptance_root(root) / "cutoff.json"


def provenance_path(root):
    return acceptance_root(root) / "overlay-provenance.json"


def canonical_source(root):
    return Path(root) / SOURCE_REL


def assert_private_path(root, target):
    """Reject escaping or symlinked acceptance paths before reads or mutations."""
    root, target = Path(root), Path(target)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise AcceptanceError("acceptance path escapes project")
    if relative.parts[:2] != ACCEPTANCE_REL.parts or ".." in relative.parts:
        raise AcceptanceError("acceptance path escapes owned tree")
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if current.is_symlink():
            raise AcceptanceError("symlink in acceptance path")
    return target


# --- source / profile -------------------------------------------------------
def _acceptance_source(root):
    return {
        "name": "eliza-acceptance-overlay",
        "managed": False,
        "path": overlay_dir(root),
        "pin": UPSTREAM_PIN,
        "bun": _BUN,
        "node": _NODE,
        "bun_version": "1.4.2",
        "node_version": "v24.15.0",
        "api_entry": ENTRY_REL,
        "vite": Path("node_modules") / "vite" / "bin" / "vite.js",
        "api_cwd": Path("."),
        "ui_cwd": Path("packages") / "app",
        # Registered validator: overlay vs canonical runtimes; never a caller value.
        "validate": lambda root: validate_overlay_source(root, launch_deadline(root)),
    }


def _verify_runtimes(root):
    for kind, expected in (("node", _NODE), ("bun", _BUN)):
        verified = upstream.verified_runtime(root, kind)
        if verified is None or Path(verified) != expected:
            raise AcceptanceError("registered %s runtime is not the verified canonical binary" % kind)
    return True


def _verify_canonical_source(root, deadline=None):
    _remaining(deadline, "canonical source")
    cutoff = None if deadline is None else (datetime.now(timezone.utc) + timedelta(seconds=deadline - time.monotonic())).isoformat()
    state = upstream.source_state(root, deadline=cutoff)
    _remaining(deadline, "canonical source")
    if not state.get("ok"):
        raise AcceptanceError("canonical source is not pinned and clean")
    return state


def launch_deadline(root):
    path = assert_private_path(root, cutoff_path(root))
    if not path.exists():
        return None
    stop = datetime.fromisoformat(str(json.loads(path.read_text())["stop_at"]))
    if stop.tzinfo is None:
        raise AcceptanceError("saved cutoff is not timezone-aware")
    deadline = time.monotonic() + stop.timestamp() - time.time() - 5
    _remaining(deadline, "launch prerequisites")
    return deadline


def launch_problems(root):
    deadline = launch_deadline(root)
    verify_overlay(root, deadline)
    if not _dropin_ready(root, deadline):
        return ["acceptance drop-ins not verified"]
    _remaining(deadline, "launch prerequisite completion")
    return []


def validate_overlay_source(root, deadline=None):
    root = Path(root)
    _verify_canonical_source(root, deadline)
    src = overlay_dir(root)
    if src.is_symlink() or not src.is_dir():
        raise AcceptanceError("acceptance overlay is unavailable")
    if not (src / ENTRY_REL).is_file():
        raise AcceptanceError("acceptance overlay is missing the api entry")
    if not (src / "node_modules" / "vite" / "bin" / "vite.js").is_file():
        raise AcceptanceError("acceptance overlay is missing the vite launcher")
    _remaining(deadline, "runtime prerequisites")
    _verify_runtimes(root)
    _remaining(deadline, "runtime prerequisites")
    verify_overlay(root, deadline)  # strict known-patch-only check
    return {"name": "eliza-acceptance-overlay", "pin": UPSTREAM_PIN, "readiness": True}


def acceptance_profile(root, window_s=DEFAULT_WINDOW_S):
    """The single immutable acceptance launch profile."""
    state = state_dir(root)
    return dash.LaunchProfile(
        name="eliza-acceptance",
        state_relative=ACCEPTANCE_REL,
        api_host=API_HOST,
        api_port=API_PORT,
        ui_port=UI_PORT,
        allowed_origins=ALLOWED_ORIGINS,
        allowed_hosts=API_HOST,
        source=_acceptance_source(root),
        run_path=overlay_dir(root),
        secrets=(),
        dotenv=None,
        compat=False,
        autonomy=False,
        routing=False,
        home=state / "home",
        window_seconds=window_s,
        gate=lambda r: acceptance_gate(r, window_s),
        load_secrets=lambda r, s: _acceptance_secrets(r),
        verify=lambda r, s: launch_problems(r),
    )


def _acceptance_secrets(root, deadline=None):
    profile = assert_private_path(root, state_dir(root) / "runtime")
    for directory in (profile, profile / "db", profile / "workspace", state_dir(root) / "home"):
        _remaining(deadline, "private directory")
        assert_private_path(root, directory)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    return {"profile": profile}


# --- bounded cutoff (captured once, never extended) -------------------------
def acceptance_gate(root, window_s=DEFAULT_WINDOW_S):
    root = Path(root)
    canonical = dash.require_window(root)
    canonical_stop = datetime.fromisoformat(str(canonical["stop_at"]))
    if canonical_stop.tzinfo is None:
        raise AcceptanceError("canonical cutoff is not timezone-aware")
    path = assert_private_path(root, cutoff_path(root))
    if path.exists():
        data = json.loads(path.read_text())
        stop_at = datetime.fromisoformat(str(data["stop_at"]))
        if stop_at.tzinfo is None:
            raise AcceptanceError("saved cutoff is not timezone-aware")
        stop_at = min(stop_at, canonical_stop, datetime.now(timezone.utc) + timedelta(seconds=window_s))
    else:
        stop_at = min(canonical_stop, datetime.now(timezone.utc) + timedelta(seconds=window_s))
    if datetime.now(timezone.utc) >= stop_at:
        raise AcceptanceError("acceptance cutoff reached; no launch")
    if not path.exists() or data["stop_at"] != stop_at.isoformat():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stop_at": stop_at.isoformat()}) + "\n")
        os.chmod(path, 0o600)
    return {"stop_at": stop_at.isoformat(), "allowed": True}


def _remaining(deadline, stage):
    if deadline is not None and time.monotonic() >= deadline:
        raise AcceptanceError("acceptance %s exceeded the shared deadline" % stage, code=2)


def sha256_file(path, deadline=None):
    _remaining(deadline, "hash")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            _remaining(deadline, "hash")
            digest.update(chunk)
    return digest.hexdigest()


def verify_patch(root, deadline=None):
    patch = Path(root) / PATCH_REL
    if not patch.is_file() or sha256_file(patch, deadline) != PATCH_SHA256:
        raise AcceptanceError("reviewed patch missing or digest mismatch")
    return patch


# --- overlay build (regular copies; no shared source inodes) ----------------
def _tracked_paths(source, deadline=None):
    _remaining(deadline, "git inventory")
    timeout = min(30, deadline - time.monotonic()) if deadline is not None else 30
    out = upstream._git(["ls-files", "--stage", "-z"], cwd=source, timeout=timeout) if hasattr(upstream, "_git") else None
    if out is None:
        import subprocess
        out = subprocess.run(["git", "-C", str(source), "ls-files", "--stage", "-z"],
                             capture_output=True, text=True, check=True, timeout=timeout)
    paths = []
    for row in out.stdout.split("\0"):
        if not row:
            continue
        metadata, rel = row.split("\t", 1)
        mode = metadata.split()[0]
        if mode == "160000":  # optional native-inference submodule, not initialized here
            continue
        if mode not in ("100644", "100755", "120000"):
            raise AcceptanceError("unsupported canonical Git entry")
        paths.append(rel)
    _remaining(deadline, "git inventory completion")
    return paths


def _link_node_modules(source_nm, overlay_nm, overlay, canonical, deadline=None):
    overlay_nm.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(source_nm), key=lambda e: e.name):
        _remaining(deadline, "dependency links")
        if entry.name == "@elizaos":
            continue
        if entry.name == "@phpretro":
            continue
        dst = overlay_nm / entry.name
        if not dst.exists() and not dst.is_symlink():
            os.symlink(entry.path, dst)
    scope = overlay_nm / "@elizaos"
    scope.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(source_nm / "@elizaos"), key=lambda e: e.name):
        _remaining(deadline, "workspace links")
        dst = scope / entry.name
        if dst.exists() or dst.is_symlink():
            continue
        if entry.name == "ui":
            os.symlink(overlay / "packages" / "ui", dst)  # patched overlay UI
        else:
            os.symlink(entry.path, dst)
    # Drop-ins are NOT linked into node_modules: discovery scans the real
    # plugins/custom package directories created by install_dropins().


def build_overlay(root, deadline=None):
    root = Path(root)
    _verify_canonical_source(root, deadline)
    source = canonical_source(root)
    overlay = assert_private_path(root, overlay_dir(root))
    if overlay.exists():
        if not _overlay_provenance_matches(root):
            raise AcceptanceError("existing overlay has unknown provenance; refusing to overwrite")
        verify_overlay(root, deadline)
        return overlay
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.mkdir(mode=0o700)
    made = True
    try:
        _remaining(deadline, "copy")
        for rel in _tracked_paths(source, deadline):
            _remaining(deadline, "copy")
            dst = overlay / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            original = source / rel
            if original.is_symlink():
                link = os.readlink(original)
                if os.path.isabs(link) or not _under(original.resolve(), source):
                    raise AcceptanceError("canonical symlink escapes source")
                os.symlink(link, dst)  # preserve only pinned internal aliases
            else:
                shutil.copy2(original, dst)  # regular copy, no shared inode
        _remaining(deadline, "node_modules")
        _link_node_modules(source / "node_modules", overlay / "node_modules", overlay, root, deadline)
        _remaining(deadline, "patch")
        patch = verify_patch(root, deadline)
        _apply_patch(overlay, patch, deadline)
        _remaining(deadline, "provenance")
        _write_provenance(root, patch, deadline)
        verify_overlay(root, deadline)
    except BaseException:
        if made and overlay.is_dir() and _under(overlay, acceptance_root(root)):
            shutil.rmtree(overlay, ignore_errors=True)
        raise
    return overlay


def _apply_patch(overlay, patch, deadline=None):
    import subprocess
    for args in (["--check"], [], ["--check", "--reverse"]):
        _remaining(deadline, "patch")
        timeout = min(30, deadline - time.monotonic()) if deadline is not None else 30
        proc = subprocess.run(["git", "apply", *args, str(patch)], cwd=str(overlay),
                              capture_output=True, text=True, timeout=timeout,
                              env={"PATH": "/usr/bin:/bin", "GIT_CEILING_DIRECTORIES": str(overlay.parent.resolve()),
                                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        if proc.returncode != 0:
            raise AcceptanceError("overlay patch step failed: %s" % proc.stderr.strip())


def _under(path, base):
    try:
        p, b = Path(path).resolve(), Path(base).resolve()
    except OSError:
        return False
    return p == b or b in p.parents


def verify_overlay(root, deadline=None):
    """Strict provenance: only the reviewed paths may differ; reverse-apply alone
    is not sufficient, so every tracked file is compared by hash."""
    root = Path(root)
    source = canonical_source(root)
    overlay = assert_private_path(root, overlay_dir(root))
    tracked = set(_tracked_paths(source, deadline))
    changed, added = set(), set()
    source_links = {rel for rel in tracked if (source / rel).is_symlink()}
    for rel in tracked:
        _remaining(deadline, "overlay verification")
        o = overlay / rel
        if rel in source_links:
            if not o.is_symlink() or os.readlink(o) != os.readlink(source / rel) or not _under(o.resolve(), overlay):
                raise AcceptanceError("overlay alias differs from pinned internal source link")
            continue
        if o.is_symlink():
            raise AcceptanceError("symlinked tracked overlay file: " + rel)
        if not o.is_file():
            changed.add(rel)
            continue
        if sha256_file(o, deadline) != sha256_file(source / rel, deadline):
            changed.add(rel)
    # Unknown additions: any overlay file (outside node_modules) that is not a
    # tracked canonical path.  Reverse-apply alone cannot exclude these.
    for dirpath, dirnames, filenames in os.walk(overlay):
        _remaining(deadline, "overlay inventory")
        for name in dirnames:
            directory = Path(dirpath) / name
            rel = str(directory.relative_to(overlay))
            if directory.is_symlink() and rel not in source_links:
                raise AcceptanceError("unexpected overlay symlink directory")
        if Path(dirpath) == overlay:
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for name in filenames:
            rel = str(Path(dirpath).relative_to(overlay) / name)
            if rel not in tracked:
                added.add(rel)
    unknown = (changed - EXPECTED_CHANGED) | (added - EXPECTED_ADDED)
    if unknown:
        raise AcceptanceError("unknown overlay change(s): %s" % ", ".join(sorted(unknown)))
    if changed != EXPECTED_CHANGED or added != EXPECTED_ADDED:
        raise AcceptanceError("overlay does not contain exactly the reviewed patch diff")
    for rel, expected in expected_patch_bytes(root, deadline).items():
        _remaining(deadline, "patch byte verification")
        if (overlay / rel).is_symlink() or (overlay / rel).read_bytes() != expected:
            raise AcceptanceError("overlay output differs from reviewed patch: " + rel)
    # the overlay UI must be the patched copy, not the canonical workspace link
    ui = overlay / "node_modules" / "@elizaos" / "ui"
    if not ui.is_symlink() or ui.resolve() != (overlay / "packages" / "ui").resolve():
        raise AcceptanceError("@elizaos/ui does not resolve to the overlay copy")
    if not (state_dir(root) / "db").parent.is_dir():
        pass
    return True


def expected_patch_bytes(root, deadline=None):
    """Reconstruct reviewed outputs from canonical bytes, independent of Git cwd."""
    outputs = {}
    patch = verify_patch(root, deadline).read_text()
    for section in patch.split("diff --git ")[1:]:
        _remaining(deadline, "patch reconstruction")
        rel = section.splitlines()[0].split(" b/", 1)[1]
        if rel not in EXPECTED_CHANGED | EXPECTED_ADDED or rel in outputs:
            raise AcceptanceError("unexpected reviewed patch path")
        base = canonical_source(root) / rel
        rows = base.read_text().splitlines(keepends=True) if rel in EXPECTED_CHANGED else []
        offset = 0
        for hunk in re.finditer(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n((?:[ +\-][^\n]*\n)*)", section, re.M):
            body = hunk.group(2).splitlines(keepends=True)
            old = [line[1:] for line in body if line[0] in " -"]
            new = [line[1:] for line in body if line[0] in " +"]
            start = max(0, int(hunk.group(1)) - 1) + offset
            if rows[start:start + len(old)] != old:
                raise AcceptanceError("reviewed patch context differs from canonical source")
            rows[start:start + len(old)] = new
            offset += len(new) - len(old)
        outputs[rel] = "".join(rows).encode()
    if set(outputs) != EXPECTED_CHANGED | EXPECTED_ADDED:
        raise AcceptanceError("reviewed patch scope differs from expected files")
    return outputs


def _is_git(path):
    return (Path(path) / ".git").exists()


def _overlay_ok(root):
    try:
        return verify_overlay(root)
    except AcceptanceError:
        return False
    except Exception:
        return False


def _write_provenance(root, patch, deadline=None):
    _remaining(deadline, "provenance")
    path = assert_private_path(root, provenance_path(root))
    data = {
        "pin": UPSTREAM_PIN,
        "patch": str(PATCH_REL),
        "patch_sha256": sha256_file(patch, deadline),
        "changed": sorted(EXPECTED_CHANGED),
        "entry": str(ENTRY_REL),
        "dropins": dropin_provenance(root, deadline),
    }
    _remaining(deadline, "provenance write")
    path.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)


def _overlay_provenance_matches(root):
    try:
        data = json.loads(assert_private_path(root, provenance_path(root)).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("pin") == UPSTREAM_PIN and data.get("patch_sha256") == PATCH_SHA256


# --- physical drop-ins (real dirs; canonical import; verified) ---------------
def plugins_custom_dir(root):
    return state_dir(root) / "runtime" / "plugins" / "custom"


def dropin_dir(root, package):
    return plugins_custom_dir(root) / package.split("/")[-1]


def canonical_entry(root, package):
    spec = DROPINS[package]
    return Path(root) / spec["canonical"] / spec["entry"]


def install_dropins(root, deadline=None):
    """Create REAL drop-in package dirs (never symlinks) under the fresh state."""
    _remaining(deadline, "drop-in installation")
    root = Path(root)
    custom = assert_private_path(root, plugins_custom_dir(root))
    custom.mkdir(parents=True, exist_ok=True)
    os.chmod(custom, 0o700)
    created = []
    for package, spec in DROPINS.items():
        _remaining(deadline, "drop-in installation")
        target = assert_private_path(root, dropin_dir(root, package))
        if target.is_symlink():
            raise AcceptanceError("drop-in directory must not be a symlink: %s" % target)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        assert_private_path(root, target / "package.json")
        assert_private_path(root, target / "index.mjs")
        (target / "package.json").write_text(json.dumps({
            "name": package,
            "type": "module",
            "main": "./index.mjs",
            "exports": {".": {"import": "./index.mjs", "default": "./index.mjs"}},
        }, indent=2) + "\n")
        url = canonical_entry(root, package).resolve().as_uri()
        (target / "index.mjs").write_text(_DROPIN_INDEX % (url, url))
        created.append(str(target))
    return created


def dropin_provenance(root, deadline=None):
    """Content-hash provenance binding each drop-in to its canonical entry."""
    root = Path(root)
    data = {}
    for package in DROPINS:
        entry = canonical_entry(root, package)
        if not entry.is_file():
            raise AcceptanceError("canonical drop-in entry missing: %s" % package)
        data[package] = {"canonical": str(entry), "entry_sha256": sha256_file(entry, deadline)}
    return data


def _dropin_ready(root, deadline=None):
    _remaining(deadline, "drop-in verification")
    """Readiness: real dir, original package id, canonical file-URL import."""
    root = Path(root)
    prov = _overlay_data(root).get("dropins", {})
    for package in DROPINS:
        _remaining(deadline, "drop-in verification")
        target = assert_private_path(root, dropin_dir(root, package))
        pkg_json = assert_private_path(root, target / "package.json")
        index = assert_private_path(root, target / "index.mjs")
        if target.is_symlink() or not pkg_json.is_file() or not index.is_file():
            return False
        try:
            if json.loads(pkg_json.read_text()).get("name") != package:
                return False
        except (OSError, json.JSONDecodeError):
            return False
        url = canonical_entry(root, package).resolve().as_uri()
        if index.is_symlink() or index.read_text() != _DROPIN_INDEX % (url, url):
            return False
        recorded = prov.get(package, {}).get("entry_sha256")
        if recorded != sha256_file(canonical_entry(root, package), deadline):
            return False
    return True


def _overlay_data(root):
    try:
        return json.loads(assert_private_path(root, provenance_path(root)).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# --- config (verified schema only; no invented fields) ----------------------
def acceptance_config(root):
    """``plugins.entries`` per the pinned zod schema
    (packages/agent/src/config/zod-schema.ts): values allow only ``enabled`` and
    ``config``.  Provider routing and the generation guard are the plugin's, not
    config fields, so none are fabricated here."""
    return {"plugins": {"entries": {name: {"enabled": True} for name in DROPINS}},
            "agents": {"defaults": {"workspace": str(state_dir(root) / "runtime" / "workspace")}}}


def write_config(root, deadline=None):
    _remaining(deadline, "config write")
    path = assert_private_path(root, config_path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_config(root), indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


# --- commands ---------------------------------------------------------------
def cmd_check(root, _argv=None):
    import io
    root = Path(root)
    prereq = {"ready": False, "problems": []}
    buf = io.StringIO()
    stdout = sys.stdout
    try:
        sys.stdout = buf
        dash.cmd_check(root, acceptance_profile(root).source, acceptance_profile(root))
    finally:
        sys.stdout = stdout
    try:
        prereq = json.loads(buf.getvalue())
    except ValueError:
        prereq = {"ok": False, "problems": ["prerequisite check produced no report"]}
    overlay_ready = _overlay_ok(root)
    try:
        dropins_ready = _dropin_ready(root)
    except Exception:
        dropins_ready = False
    report = {
        "command": "check",
        "prerequisites": {"ok": bool(prereq.get("ok")), "problems": prereq.get("problems", [])},
        "overlay_ready": overlay_ready,
        "dropins_ready": dropins_ready,
        "host": "pending",       # real configured host not launched here
        "browser": "pending",    # browser/owner transport proof pending root
    }
    print(json.dumps(report, indent=2))
    return 0 if (report["prerequisites"]["ok"] and overlay_ready and dropins_ready) else 1


def operation_budget(args, cap):
    seconds = getattr(args, "deadline", None)
    if seconds is None:
        return cap
    if seconds <= 0:
        raise AcceptanceError("operation deadline must be positive")
    return min(seconds, cap)


def cmd_setup(root, _argv=None):
    budget = operation_budget(_argv, SETUP_BUDGET_S)
    gate = acceptance_gate(root)
    seconds = datetime.fromisoformat(gate["stop_at"]).timestamp() - time.time() - 5
    if seconds <= 0:
        raise AcceptanceError("acceptance setup has no cleanup budget")
    deadline = time.monotonic() + min(budget, seconds)
    build_overlay(root, deadline)
    _acceptance_secrets(root, deadline)
    install_dropins(root, deadline)
    write_config(root, deadline)
    _remaining(deadline, "setup completion")
    print(json.dumps({"overlay": str(overlay_dir(root)),
                      "plugins": [str(dropin_dir(root, p)) for p in DROPINS],
                      "patch_sha256": PATCH_SHA256, "ready": True}, indent=2))
    return 0


def cmd_start(root, _argv=None):
    profile = acceptance_profile(root, operation_budget(_argv, DEFAULT_WINDOW_S))
    return dash.cmd_start(Path(root), profile.source, profile)


def cmd_stop(root, _argv=None):
    return dash.cmd_stop(Path(root), acceptance_profile(root))


def cmd_status(root, _argv=None):
    return dash.cmd_status(Path(root), acceptance_profile(root))


COMMANDS = {"check": cmd_check, "setup": cmd_setup, "start": cmd_start,
            "stop": cmd_stop, "status": cmd_status}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="check", choices=list(COMMANDS))
    parser.add_argument("--yes", action="store_true", help="confirm a gated action")
    parser.add_argument("--deadline", type=int, default=None, help="maximum operation seconds (setup capped at 240, start at 3600)")
    args = parser.parse_args(argv)
    if args.command in ("setup", "start") and not args.yes:
        print("error: %s is gated; pass --yes" % args.command, file=sys.stderr)
        return 1
    try:
        return COMMANDS[args.command](ROOT, args)
    except subprocess.TimeoutExpired:
        print("error: acceptance subprocess exceeded the shared deadline", file=sys.stderr)
        return 2
    except DashboardError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return getattr(exc, "code", 1) if isinstance(exc, AcceptanceError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
