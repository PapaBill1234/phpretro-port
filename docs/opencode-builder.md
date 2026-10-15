# OpenCode 2 bootstrap

Current continuation: [migration plan](opencode-migration-plan.md) and
[gap/acceptance checklist](opencode-migration-checklist.md). The plan is proposed,
not architecture-locked; runtime enforcement and live acceptance remain open.

OpenCode 2.0.29 is pinned from the official `@opencode/cli-linux-x64` npm
package. The bootstrap checks the published SHA-512 integrity value, validates
the package identity and contents, and extracts only the native CLI binary
into `.builder/opencode-runtime/v2.0.29/`. It does not run npm, postinstall
scripts, or a model request, and it does not install or replace a global CLI.

Install and start either of the two isolated account profiles:

```sh
python3 scripts/opencode-builder.py install
python3 scripts/opencode-builder.py launch --profile primary
python3 scripts/opencode-builder.py launch --profile secondary
```

Each profile has its own private `HOME` and XDG config, data, state, and cache
directories. `--standalone` gives each launch a profile-local server as well.
Desktop browser variables are preserved so native OAuth can open a browser;
API-key and Codex-token variables are removed from the child environment. To
connect an account, run `/connect` in that profile, choose OpenAI and use the
available ChatGPT sign-in flow. Then run `/models` and select the model the
account can actually use. Repeat separately in the other profile for the
second account. No login, account switch, model selection, or generation was
performed during this bootstrap. The launcher does not set a guessed Sol model
ID, change accounts, or silently substitute models.

OpenCode 2 includes a native DeepSeek provider for direct API access. It can be
connected from either account profile through `/connect`; no custom provider
adapter or credential is staged by this change. Model availability and account
access remain unverified until an owner connects the provider and checks
`/models`. The launcher does not dispatch a DeepSeek agent.

The profile launcher does not enforce off-peak timing, a shared four-worker
limit, or a spend ceiling. Those remain project policies without qualified
OpenCode 2 request-boundary enforcement. CMS/theme/game-port work remains
paused during the migration.

## Verified OpenCode sources

- [OpenCode 2 documentation and install options](https://opencode.ai/v2/docs/)
- [V2 providers and native provider setup](https://opencode.ai/v2/docs/providers/)
- [V2 configuration locations and providers config](https://opencode.ai/v2/docs/config/)
- [V1-to-V2 migration guide](https://opencode.ai/v2/docs/migrate-v1/)
- [Official OpenCode CLI package](https://www.npmjs.com/package/@opencode/cli)
- [Official pinned Linux x64 package](https://www.npmjs.com/package/@opencode/cli-linux-x64/v/2.0.29)
