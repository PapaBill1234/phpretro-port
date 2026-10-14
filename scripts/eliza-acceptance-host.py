#!/usr/bin/env python3
"""Isolated PRODUCTION full-Eliza acceptance host helper.

Owns one private tree, ``.builder/eliza-acceptance``.  Subcommands:

  check    read-only. Verify pin, patch, runtimes and installed canonical
           source; report ready vs blocked. Never writes anything.
  setup    build a fresh private overlay of the pinned source with the reviewed
           ``new-chat.patch`` applied, plus fixed isolated state. Gated.
  start    run the real host in the foreground with the exact reviewed argv and
           a filtered, fixed environment. Gated. No browser/model/account.
  stop     identity-safe stop of the recorded process group.
  status   report the recorded host and overlay provenance.

Hard boundaries (enforced, fail-closed):

  * The canonical install is a fixed trusted root, never a caller flag.  The
    helper only ever reads it.
  * All writes stay inside ``.builder/eliza-acceptance`` and every ancestor of
    a write target must be a real (non-symlink) directory.
  * ``setup`` never wipes a pre-existing overlay or state; it refuses an
    unknown/foreign overlay rather than replacing it.
  * The child environment is filtered: ``DATABASE_URL``/``POSTGRES_URL``,
    provider/cloud credentials and any other ``ELIZA_*`` variable are dropped
    and replaced with the fixed acceptance values.
  * ``stop`` only signals the recorded pid group after matching its command
    line; there is no global ``pkill``.

This module intentionally does not install packages, download runtimes, launch
a browser, call a model or provider, or read credentials.  Real startup and
browser proof remain a separate root step after review.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# --- reuse the pinned upstream helpers (single source of truth) -------------
_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("eliza_upstream", _HERE / "eliza-upstream.py")
upstream = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upstream)

UPSTREAM_PIN = upstream.UPSTREAM_PIN  # 28fef47c9d9d3acc7792be0fdaa3070020293cc5
SOURCE_REL = upstream.SOURCE_REL      # builder/eliza/upstream

# --- fixed, non-overridable acceptance identity -----------------------------
# The canonical installed checkout lives in this trusted root (submodule pin +
# private ``.builder/upstream`` runtimes).  There is deliberately no --root
# flag: production cannot be pointed at an arbitrary tree.
TRUSTED_ROOT = Path("/home/ubuntu/Documents/Codex/2026-10-11/al/outputs/phpretro-eliza")

ACCEPTANCE_REL = Path(".builder") / "eliza-acceptance"
PATCH_REL = Path("builder") / "eliza" / "patches" / "new-chat.patch"
PATCH_SHA256 = "caf755bed336361427796f1d33d088013c097f8dbc99c497cc0b27b52804ffb9"

ENTRY_REL = Path("packages") / "app" / "src" / "entry.ts"

# Default fixed loopback ports; bind is loopback only, allowed origins are the
# exact loopback origins (no wildcard, no null origin).
API_PORT = 18831
UI_PORT = 18832
BIND_ADDR = "127.0.0.1"
ALLOWED_ORIGINS = "http://%s:%d,http://%s:%d" % (BIND_ADDR, UI_PORT, BIND_ADDR, API_PORT)

LOG_LIMIT_BYTES = 4 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024  # refuse setup under 1 GiB free
DEFAULT_SETUP_BUDGET_S = 240

# Environment keys always dropped before the fixed values are applied.
DROP_EXACT = frozenset({
    "DATABASE_URL", "POSTGRES_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD",
    "PGDATABASE", "NODE_OPTIONS", "BUN_INSTALL",
})
# Credential/provider prefixes dropped wholesale (case-insensitive).
DROP_PREFIXES = (
    "OPENAI", "ANTHROPIC", "DEEPSEEK", "GEMINI", "GOOGLE_API", "AZURE",
    "AWS_", "CODEX", "CLAUDE_", "ELIZAOS_CLOUD", "ELIZA_CLOUD", "COHERE",
    "MISTRAL", "GROQ", "TOGETHER", "REPLICATE", "HF_", "HUGGINGFACE",
)


class AcceptanceError(Exception):
    """A fail-closed refusal; carries the process exit code."""

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


def logs_dir(root):
    return acceptance_root(root) / "logs"


def run_dir(root):
    return acceptance_root(root) / "run"


def config_path(root):
    return state_dir(root) / "eliza.json"


def pid_path(root):
    return run_dir(root) / "host.pid"


def provenance_path(root):
    return acceptance_root(root) / "overlay-provenance.json"


def canonical_source(root):
    return Path(root) / SOURCE_REL


# --- containment / boundary checks -----------------------------------------
def _resolved_under(path, base):
    """True when ``path`` resolves inside ``base`` (both fully resolved)."""
    try:
        p = Path(path).resolve(strict=False)
        b = Path(base).resolve(strict=False)
    except OSError:
        return False
    return p == b or b in p.parents


def assert_no_symlink_ancestors(path, base):
    """Every ancestor of ``path`` up to (and including) ``base`` is a real dir.

    A symlinked ancestor would let a write escape the owned tree, so refuse it.
    """
    path = Path(path)
    base = Path(base)
    if not _resolved_under(path, base):
        raise AcceptanceError("target %s escapes owned root %s" % (path, base))
    cur = path
    while True:
        if cur.exists() and cur.is_symlink():
            raise AcceptanceError("symlinked ancestor refused: %s" % cur)
        if cur == base or cur.parent == cur:
            break
        cur = cur.parent


def assert_trusted_root(root):
    root = Path(root)
    if not root.is_dir():
        raise AcceptanceError("trusted root missing: %s" % root)
    if (root / ".builder").is_symlink():
        raise AcceptanceError("trusted root .builder is a symlink; refusing")
    if (root / "builder" / "eliza").is_symlink():
        raise AcceptanceError("trusted builder/eliza is a symlink; refusing")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_patch(root):
    """Return the patch path only if its bytes match the reviewed digest."""
    patch = Path(root) / PATCH_REL
    if not patch.is_file():
        raise AcceptanceError("reviewed patch missing: %s" % PATCH_REL)
    if sha256_file(patch) != PATCH_SHA256:
        raise AcceptanceError("reviewed patch digest mismatch: %s" % PATCH_REL)
    return patch


def _git(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AcceptanceError("git %s failed in %s: %s"
                              % (" ".join(args), cwd, proc.stderr.strip() or proc.stdout.strip()))
    return proc


# --- environment filter -----------------------------------------------------
def _dropped(key):
    upper = key.upper()
    if upper in DROP_EXACT:
        return True
    if upper.startswith("ELIZA_"):
        return True  # all acceptance ELIZA_* values are fixed below
    for prefix in DROP_PREFIXES:
        if upper.startswith(prefix):
            return True
    return False


def build_env(root, base=None):
    """Fixed, filtered child environment for the isolated host."""
    source = dict(os.environ if base is None else base)
    env = {k: v for k, v in source.items() if not _dropped(k)}
    env.update({
        "ELIZA_STATE_DIR": str(state_dir(root)),
        "ELIZA_CONFIG_PATH": str(config_path(root)),
        "PGLITE_DATA_DIR": str(state_dir(root) / "db"),
        "ELIZA_API_BIND": BIND_ADDR,
        "ELIZA_API_PORT": str(API_PORT),
        "ELIZA_UI_PORT": str(UI_PORT),
        "ELIZA_ALLOWED_ORIGINS": ALLOWED_ORIGINS,
        "ELIZA_ALLOW_NULL_ORIGIN": "0",
        "ELIZA_NULL_ORIGIN": "disabled",
        # keep Bun/Node deterministic and offline; never inherit a DB/provider
        "BUN_INSTALL_CACHE_DIR": str(acceptance_root(root) / "cache"),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    return env


def start_argv(root, bun_bin=None):
    """The exact reviewed launch argv (entry.ts must be the overlay copy)."""
    entry = overlay_dir(root) / ENTRY_REL
    bun = str(bun_bin) if bun_bin else str(upstream.bun_bin(root))
    return [bun, "--no-env-file", "--no-install", "--conditions=eliza-source",
            str(entry), "start"]


# --- read-only check --------------------------------------------------------
def run_check(root=TRUSTED_ROOT):
    """Return a report dict; never writes. ``ok`` means every core step ready."""
    root = Path(root)
    report = {"root": str(root), "pin": UPSTREAM_PIN, "ready": False,
              "blockers": [], "warnings": [], "details": {}}
    try:
        assert_trusted_root(root)
    except AcceptanceError as exc:
        report["blockers"].append(str(exc))
        return report

    src = canonical_source(root)
    report["details"]["canonical_source"] = str(src)
    if not src.is_dir():
        report["blockers"].append("canonical source missing: %s" % src)
    else:
        head = _git(["rev-parse", "HEAD"], src, check=False)
        pin = head.stdout.strip()
        report["details"]["canonical_head"] = pin
        if pin != UPSTREAM_PIN:
            report["blockers"].append("canonical source not at pin %s (found %s)"
                                      % (UPSTREAM_PIN, pin or "?"))
        status = _git(["status", "--porcelain"], src, check=False)
        dirty = status.stdout.strip()
        report["details"]["canonical_dirty"] = bool(dirty)
        if dirty:
            report["blockers"].append("canonical source is not clean (operator changes present)")
        if not (src / ENTRY_REL).is_file():
            report["blockers"].append("entry point missing: %s" % ENTRY_REL)

    try:
        verify_patch(root)
        report["details"]["patch_sha256"] = PATCH_SHA256
    except AcceptanceError as exc:
        report["blockers"].append(str(exc))

    for kind in ("node", "bun"):
        binary = upstream.verified_runtime(root, kind)
        report["details"][kind] = str(binary) if binary else None
        if binary is None:
            report["blockers"].append("verified %s runtime unavailable in .builder/upstream" % kind)

    report["ready"] = not report["blockers"]
    return report


# --- overlay build ----------------------------------------------------------
def _hardlink_tree(source, overlay, deadline, skip=frozenset({"node_modules", ".git"})):
    for dirpath, dirnames, filenames in os.walk(source):
        if deadline and time.monotonic() > deadline:
            raise AcceptanceError("overlay build exceeded the shared deadline", code=2)
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel = Path(dirpath).relative_to(source)
        target_dir = overlay / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            dst = target_dir / name
            if dst.exists() or dst.is_symlink():
                continue
            try:
                os.link(Path(dirpath) / name, dst)
            except OSError:
                shutil.copy2(Path(dirpath) / name, dst)


def _link_node_modules(source_nm, overlay_nm, overlay):
    """Recreate node_modules so scoped workspace links resolve into the overlay.

    A wholesale node_modules symlink (or the canonical ``@elizaos/ui ->
    ../../packages/ui`` link) would resolve *the original unpatched* UI package,
    so ``@elizaos`` is materialized and its ``ui`` entry is re-pointed at the
    overlay's own ``packages/ui``.
    """
    if not source_nm.is_dir():
        raise AcceptanceError("canonical node_modules missing: %s" % source_nm)
    overlay_nm.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(source_nm), key=lambda e: e.name):
        if entry.name == "@elizaos":
            continue
        dst = overlay_nm / entry.name
        if dst.exists() or dst.is_symlink():
            continue
        os.symlink(entry.path, dst)
    src_scope = source_nm / "@elizaos"
    dst_scope = overlay_nm / "@elizaos"
    dst_scope.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(src_scope), key=lambda e: e.name):
        dst = dst_scope / entry.name
        if dst.exists() or dst.is_symlink():
            continue
        if entry.name == "ui":
            os.symlink(overlay / "packages" / "ui", dst)  # patched overlay UI
        else:
            os.symlink(entry.path, dst)


def _apply_patch(overlay, patch):
    _git(["apply", "--check", str(patch)], overlay)
    _git(["apply", str(patch)], overlay)
    # the applied diff must equal the reviewed patch exactly
    _git(["apply", "--check", "--reverse", str(patch)], overlay)


def _write_provenance(root, patch):
    data = {
        "pin": UPSTREAM_PIN,
        "patch": str(PATCH_REL),
        "patch_sha256": sha256_file(patch),
        "canonical_source": str(canonical_source(root)),
        "created": int(time.time()),
        "entry": str(ENTRY_REL),
    }
    path = provenance_path(root)
    path.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)
    return data


def overlay_provenance(root):
    try:
        return json.loads(provenance_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run_setup(root=TRUSTED_ROOT, deadline_s=DEFAULT_SETUP_BUDGET_S):
    """Build the private overlay + fixed state once; never wipe. Gated by CLI."""
    root = Path(root)
    report = run_check(root)
    if not report["ready"]:
        raise AcceptanceError("setup blocked: %s" % "; ".join(report["blockers"]))
    deadline = time.monotonic() + deadline_s if deadline_s else None

    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        raise AcceptanceError("low disk: %d bytes free (< %d)" % (free, MIN_FREE_BYTES), code=2)
    if upstream.verified_runtime(root, "bun") is None:
        raise AcceptanceError("bun runtime not verified")

    patch = verify_patch(root)
    overlay = overlay_dir(root)
    assert_no_symlink_ancestors(overlay, acceptance_root(root))
    made_overlay = False
    if overlay.exists():
        prov = overlay_provenance(root)
        if not prov or prov.get("pin") != UPSTREAM_PIN or prov.get("patch_sha256") != PATCH_SHA256:
            raise AcceptanceError(
                "existing overlay has unknown provenance; refusing to wipe %s" % overlay)
    else:
        (overlay / "..").resolve()
        overlay.mkdir(parents=True, exist_ok=False)
        made_overlay = True
        try:
            _hardlink_tree(canonical_source(root), overlay, deadline)
            _link_node_modules(canonical_source(root) / "node_modules",
                               overlay / "node_modules", overlay)
            _apply_patch(overlay, patch)
            _write_provenance(root, patch)
        except BaseException:
            if made_overlay and _resolved_under(overlay, acceptance_root(root)):
                shutil.rmtree(overlay, ignore_errors=True)  # only our fresh dir
            raise

    for directory in (state_dir(root), logs_dir(root), run_dir(root),
                      state_dir(root) / "db", state_dir(root) / "workspace"):
        assert_no_symlink_ancestors(directory, acceptance_root(root))
        directory.mkdir(parents=True, exist_ok=True)
    if not config_path(root).exists():
        write_config(root)

    return {"overlay": str(overlay), "state": str(state_dir(root)),
            "patch_sha256": PATCH_SHA256, "ready": True}


def acceptance_config(root):
    """Exact upstream-shaped config: pglite provider, isolated workspace."""
    return {
        "database": {"provider": "pglite", "dir": str(state_dir(root) / "db")},
        "agent": {"workspace": str(state_dir(root) / "workspace"),
                  "name": "acceptance-host"},
        "plugins": {
            "projectCompanion": {"enabled": True, "provider": "pglite"},
            "nativeSol": {"enabled": True, "generation": False},
        },
    }


def write_config(root=TRUSTED_ROOT):
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_config(root), indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


# --- start / stop / status --------------------------------------------------
def _read_pid(root):
    try:
        return int(pid_path(root).read_text().strip())
    except (OSError, ValueError):
        return None


def _same_host(pid, root):
    """True only if ``pid`` cmdline runs this overlay's entry (identity check)."""
    try:
        cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
    except OSError:
        return False
    text = " ".join(p.decode("utf-8", "replace") for p in cmdline if p)
    return str(overlay_dir(root) / ENTRY_REL) in text


def run_start(root=TRUSTED_ROOT):
    """Foreground launch of the real host under the fixed filtered environment."""
    root = Path(root)
    report = run_check(root)
    if not report["ready"]:
        raise AcceptanceError("start blocked: %s" % "; ".join(report["blockers"]))
    entry = overlay_dir(root) / ENTRY_REL
    if not entry.is_file():
        raise AcceptanceError("setup required first: %s missing" % entry)
    bun = upstream.verified_runtime(root, "bun")
    if bun is None:
        raise AcceptanceError("bun runtime not verified")
    argv = start_argv(root, bun)
    env = build_env(root)
    existing = _read_pid(root)
    if existing and _same_host(existing, root):
        raise AcceptanceError("host already running (pid %d)" % existing)

    run_dir(root).mkdir(parents=True, exist_ok=True)
    log = logs_dir(root) / "host.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(argv, cwd=str(entry.parent.parent.parent.parent),
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True, text=True)
    pid_path(root).write_text("%d\n" % proc.pid)
    os.chmod(pid_path(root), 0o600)
    try:
        with open(log, "a", encoding="utf-8", errors="replace") as handle:
            for line in proc.stdout:
                handle.write(line)
        return proc.wait()
    finally:
        pid_path(root).unlink(missing_ok=True)


def run_stop(root=TRUSTED_ROOT, grace_s=20):
    """Identity-safe stop of the recorded group; never a global pkill."""
    root = Path(root)
    pid = _read_pid(root)
    if pid is None:
        return {"stopped": False, "reason": "no recorded host"}
    if not _same_host(pid, root):
        pid_path(root).unlink(missing_ok=True)
        raise AcceptanceError("recorded pid %d is not this overlay's host; refusing to signal"
                              % pid)
    pgid = os.getpgid(pid)
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    else:
        os.killpg(pgid, signal.SIGKILL)
    pid_path(root).unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def run_status(root=TRUSTED_ROOT):
    root = Path(root)
    pid = _read_pid(root)
    live = bool(pid) and _same_host(pid, root)
    if pid and not live:
        pid_path(root).unlink(missing_ok=True)
    return {
        "root": str(root),
        "overlay": str(overlay_dir(root)),
        "overlay_provenance": overlay_provenance(root),
        "state": str(state_dir(root)),
        "pid": pid if live else None,
        "running": live,
        "api": "%s:%d" % (BIND_ADDR, API_PORT),
        "ui": "%s:%d" % (BIND_ADDR, UI_PORT),
    }


# --- CLI --------------------------------------------------------------------
def _render(report):
    print("trusted root: %s" % report.get("root"))
    print("pin:          %s" % report.get("pin"))
    for key, value in sorted(report.get("details", {}).items()):
        print("  %-18s %s" % (key, value))
    print("ready:        %s" % ("yes" if report.get("ready") else "NO"))
    for item in report.get("blockers", []):
        print("  BLOCKED: %s" % item)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="check",
                        choices=["check", "setup", "start", "stop", "status"])
    parser.add_argument("--yes", action="store_true",
                        help="confirm a gated write/foreground action (setup/start)")
    parser.add_argument("--deadline", type=int, default=DEFAULT_SETUP_BUDGET_S,
                        help="setup shared deadline in seconds")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            report = run_check()
            _render(report)
            return 0 if report["ready"] else 1
        if args.command in ("setup", "start") and not args.yes:
            raise AcceptanceError("%s is gated; pass --yes" % args.command)
        if args.command == "setup":
            print(json.dumps(run_setup(deadline_s=args.deadline), indent=2))
            return 0
        if args.command == "start":
            return run_start()
        if args.command == "stop":
            print(json.dumps(run_stop(), indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(run_status(), indent=2))
            return 0
    except AcceptanceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return exc.code
    return 2


if __name__ == "__main__":
    sys.exit(main())
