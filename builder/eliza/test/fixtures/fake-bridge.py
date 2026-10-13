#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Controlled fake Codex bridge for tests. No model, login or network use.

Reports the environment actually propagated to a real Python child (mirroring
the real bridge, which forwards only its allowlisted child environment), so the
test proves companion-to-Python propagation of the selected CODEX_HOME and the
absence of any provider credential.
"""

import json
import os
import subprocess
import sys

CREDENTIAL_KEYS = ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")

probe = (
    "import json, os, sys;"
    "print(json.dumps({"
    "'CODEX_HOME': os.environ.get('CODEX_HOME'),"
    "'hasCredential': any(k in os.environ for k in %r)"
    "}))" % (list(CREDENTIAL_KEYS),)
)
child = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=dict(os.environ))
observed = json.loads(child.stdout)
print(json.dumps({
    "schema": "phpretro.codex.bridge.v1",
    "ok": True,
    "command": sys.argv[1] if len(sys.argv) > 1 else None,
    "received": observed,
    "childExit": child.returncode,
}))
