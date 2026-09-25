# One-line node onboarding

Turn any Ubuntu/Debian machine with a GPU into a Petabyte seller node:

```bash
PETABYTE_API_URL=https://petabyte.market \
PETABYTE_API_KEY=pk_your_node_key \
PRICE_PER_HOUR=1.5 \
bash <(curl -fsSL https://petabyte.market/install.sh)
```

What it does (≈30s after deps):
1. Installs Docker (the job sandbox runtime), Python, venv.
2. Fetches the agent into `/opt/petabyte-agent`.
3. `provision.py`: detects CPU/RAM and GPU (`nvidia-smi`, or `GPU_MODEL=` override),
   registers the spec, **attests it with a fresh Ed25519 key**, mints a 90-day API
   key, and writes `/etc/petabyte/agent.env` (chmod 600).
4. Installs + starts the `petabyte-agent` systemd service (heartbeat + job loop).

The node is then attested, online, and bookable. Verify:
```bash
systemctl status petabyte-agent
journalctl -u petabyte-agent -f
```

Optional env: `UNITS` (identical rentable units, default 1), `MAX_HOURS` (24),
`GPU_MODEL`/`GPU_COUNT`/`VRAM_GB` (manual override when `nvidia-smi` isn't present).

Security: the agent's signing key lives only at `/etc/petabyte/agent_ed25519.key`
and is the same identity used for attestation and signed job results.
