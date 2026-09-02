---
name: thinkroom-install
description: Install and verify Thinkroom skills
license: MIT
metadata:
  version: "0.2.6"
  author: "CK, Martin (Hermes Agent)"
  platforms: "linux"
  tags: "thinkroom, research, installation"
---

# Thinkroom install

Use this skill when installing, checking, or removing Thinkroom's managed Agent Skills bundle.

## Install

Use the host profile when available:

```bash
# Codex App / CLI / IDE
thinkroom skills install --profile codex
thinkroom skills status --profile codex

# Hermes Agent default profile; pair with `hermes --profile default ...`
unset HERMES_HOME
thinkroom skills install --profile hermes
thinkroom skills status --profile hermes
```

Codex resolves to `$HOME/.agents/skills`. Hermes resolves to `$HERMES_HOME/skills`, defaulting to
`~/.hermes/skills`. For a named Hermes profile, set the non-empty absolute
`HERMES_HOME="$HOME/.hermes/profiles/<profile-name>"` for every Thinkroom command and use
`hermes --profile <profile-name> ...` for MCP registration and verification. Never rely on sticky
profile state. For another Agent Skills-compatible host, use exactly one explicit target:

```bash
thinkroom skills install --target <skill-root>
thinkroom skills status --target <skill-root>
```

The managed projection contains:

- `thinkroom-trigger`
- `thinkroom-operate`
- `thinkroom-install`

## Safety contract

- Installation is idempotent: managed files that still match are classified `EXACT`.
- New managed files are classified `ADD`.
- Exact files owned by the allowlisted pre-profile v0.2 receipt are classified `UPDATE` when the
  current bundle replaces them. The installer migrates only that known receipt and exact payload set.
- Existing unmanaged, missing-after-receipt, or modified managed files are `DIVERGED`.
- Never overwrite a `DIVERGED` target. Inspect and reconcile it outside the installer.
- Keep the generated `.thinkroom/skills-receipt-v1.json`; status and uninstall use it to verify ownership and hashes.

## Remove

Check status first, then remove only the exact managed projection:

```bash
thinkroom skills status --target <skill-root>
thinkroom skills uninstall --target <skill-root>
```

The equivalent profile commands are supported for Codex and Hermes. Do not pass `--profile` and
`--target` together.

After any install or removal, start a fresh agent session or use the agent's supported skill reload command so its skill index reflects the filesystem.
