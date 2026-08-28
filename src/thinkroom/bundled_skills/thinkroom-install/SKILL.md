---
name: thinkroom-install
description: Install and verify Thinkroom skills
version: 0.1.0
author: CK, Martin (Hermes Agent)
license: MIT
platforms: [linux]
tags: [thinkroom, research, installation]
---

# Thinkroom install

Use this skill when installing, checking, or removing Thinkroom's managed Agent Skills bundle.

## Install

Choose the compatible agent's skill root and run:

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
- Existing unmanaged, missing-after-receipt, or modified managed files are `DIVERGED`.
- Never overwrite a `DIVERGED` target. Inspect and reconcile it outside the installer.
- Keep the generated `.thinkroom/skills-receipt-v1.json`; status and uninstall use it to verify ownership and hashes.

## Remove

Check status first, then remove only the exact managed projection:

```bash
thinkroom skills status --target <skill-root>
thinkroom skills uninstall --target <skill-root>
```

After any install or removal, start a fresh agent session or use the agent's supported skill reload command so its skill index reflects the filesystem.
