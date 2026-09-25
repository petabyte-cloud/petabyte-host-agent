# Petabyte host agent — public source mirror

This repository is a **read-only mirror** of the software GPU owners install to rent out their
machines on [Petabyte](https://petabyte.market). Every file here is the content of the exact
signed bundle that host machines download from `https://petabyte.market/agent.tar.gz`.

- **Release:** `bf823d693df0a79932918e62973dc2c671979441`
- **Bundle SHA-256:** `f7a74d2d9af50c2f3865b14e35b6e1e72090a95c4791a91aed171ad29cde14dc`

Hosts never install or update from this repository. The installer pins our release public key
(`release_ed25519.pub`, also in this repo), and the updater (`update.sh`) refuses any bundle whose
signature does not verify against it. So this mirror lets anyone read the code, but cannot change
what runs on a host.

## Check that this is really what hosts run

```bash
curl -O https://petabyte.market/agent.tar.gz
curl -O https://petabyte.market/agent.tar.gz.sig
# 1. the bundle is signed by Petabyte's release key
openssl pkeyutl -verify -pubin -inkey release_ed25519.pub -rawin -in agent.tar.gz -sigfile agent.tar.gz.sig
# 2. it is the bundle this commit was made from (a newer release changes the hash — see the git log)
sha256sum agent.tar.gz
# 3. the files are identical to this repository
tar -xzf agent.tar.gz
diff -r --exclude=.git --exclude=README.md --exclude=release_ed25519.pub lumaris_agent/ ./
```

Each release is a commit tagged `release-<sha>`, so the history shows what changed between
versions.

## Where to start reading

- `install.sh` / `install.ps1` — what the installer does to your machine
- `main.py`, `task_fetcher.py` — the agent loop and how jobs run in isolated containers
- `network_policy.py`, `egress_vpn.py` — the network limits on buyer workloads
- `update.sh` — signed-only auto-update

## Reporting a problem

Security issues: security@petabyte.market (see
[/.well-known/security.txt](https://petabyte.market/.well-known/security.txt)).
Other questions: info@petabyte.market. Issues and pull requests are not monitored here.

## License

Source available for inspection. Copyright © 2026 Petabyte Cloud, Inc. All rights reserved.
No license to copy, modify or redistribute is granted beyond what GitHub's terms allow for
viewing and forking public repositories. The `petabyte` command-line client is separately
open source (MIT) at [petabyte-cloud/petabyte-client](https://github.com/petabyte-cloud/petabyte-client)
and on [PyPI](https://pypi.org/project/petabyte-client/).
