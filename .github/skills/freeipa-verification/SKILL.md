---
name: freeipa-verification
description: Verification-first workflow for direct ipa CLI or ipalib Python API work against FreeIPA, outside the Ansible collection. Use this before running any ipa command or writing ipalib code whose availability or exact parameters you haven't confirmed against this specific server, and before any destructive FreeIPA operation.
---

# FreeIPA: verify against the live server, not memory

FreeIPA's CLI surface and Python API depend on which server plugins are
enabled (trust, DNS, vault, KRA, etc. are optional) and on the installed
version. Never assume a command or option exists — confirm it live.

## 1. Discover what's actually available on this server

```bash
ipa help topics
ipa help <topic>              # e.g. ipa help user
ipa <command> --help          # exact flags for one command
```

This reflects the live server's enabled plugins — more reliable than a
recalled generic FreeIPA install.

## 2. For direct Python API use, introspect the installed client

```bash
python3 -c "
from ipalib import api
api.bootstrap(context='cli')
api.finalize()
print(sorted(api.Command))
"
python3 -c "
from ipalib import api
api.bootstrap(context='cli'); api.finalize()
help(api.Command.user_add)
"
```

## 3. Check the version — option availability changed across releases

```bash
ipa --version
rpm -q freeipa-server freeipa-client 2>/dev/null || dpkg -l | grep freeipa
```

## No universal dry-run — snapshot instead

Most `ipa` commands have no `--check` mode. Snapshot state before changing
anything so you can produce a real diff afterward:

```bash
ipa user-show <uid> --all --raw > /tmp/before.txt   # or group-show, host-show, etc.
# ... make the change ...
ipa user-show <uid> --all --raw > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
```

For anything destructive (`*-del`, `*-remove-member`, `trust-del`,
`vault-*`), always snapshot first and get explicit user confirmation before
running — there's no built-in undo.

## Idempotency and error handling

- `*-add` commands fail loudly on a second run ("already exists") — that's
  expected. Check existence first (`ipa <object>-show`/`--find`) rather than
  relying on add-then-ignore-error, unless the command has a confirmed
  idempotent mode.
- `*-mod` commands are generally safe to re-run with the same values.
- Read the actual RPC error text (`already exists`, `no such entry`,
  `missing required parameter`) — it maps directly to a fix. Don't try a
  different flag before reading it.
- On replicated setups, allow a moment for propagation before concluding a
  change "didn't take"; verify against the server you wrote to.

If `ipa help`/`--help` doesn't clarify a behavior (e.g. option semantics
interacting with existing state), search for the specific FreeIPA version +
command rather than a generic query, and prefer upstream FreeIPA docs/issue
tracker over blog posts, since semantics changed across releases.
