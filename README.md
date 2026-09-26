# Petabyte host agent — public source mirror

This repository is the **MIT-licensed source mirror** of the seller agent GPU owners install
to rent out their machines on [Petabyte](https://petabyte.market). You can inspect, modify,
build and redistribute Petabyte's agent code under [LICENSE](LICENSE).

The agent payload comes from the verified, signed bundle served at
`https://petabyte.market/agent.tar.gz`. This README, the license and the verification public
key are mirror metadata; they may be added or updated separately from that bundle.

- **Release:** `49cb87797c2e27be7ff1b8cced325c522d19acde`
- **Bundle SHA-256:** `503d91fe1464ed5c3836250483acaa2a8fd876dfd7888738fb97b0e9fa20e24d`

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
diff -r --exclude=.git --exclude=README.md --exclude=LICENSE --exclude=release_ed25519.pub lumaris_agent/ ./
```

Each release is a commit tagged `release-<sha>`, so the history shows what changed between
versions.

## Build and contribute

See [INSTALL.md](INSTALL.md) for host installation, [WINDOWS.md](WINDOWS.md) for the Windows
instructions and [BUILD.md](BUILD.md) for executable builds. For local source development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Read the installer and configure your own node credential before running the agent. Do not
commit credentials, signing keys or personal configuration. A modified build is not an
official signed Petabyte release. Release-signing private keys are never published here.

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

Petabyte-authored agent code in this mirror is licensed under the [MIT License](LICENSE).
Third-party dependencies retain their own licenses. This license does not apply to
Petabyte's private marketplace backend or separate desktop-app code.

The command-line client is also open source (MIT) at
[petabyte-cloud/petabyte-client](https://github.com/petabyte-cloud/petabyte-client).
Product updates are published in the [website changelog](https://petabyte.market/changelog).
