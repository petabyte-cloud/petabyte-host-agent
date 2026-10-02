# One-line node onboarding

Turn any Ubuntu/Debian machine with a GPU into a Petabyte seller node:

```bash
export PETABYTE_API_URL=https://petabyte.market
export PETABYTE_API_KEY=pk_your_node_key
export PRICE_PER_HOUR=1.5  # optional
curl -fsSL https://petabyte.market/install.sh | \
  sudo --preserve-env=PETABYTE_API_URL,PETABYTE_API_KEY,PRICE_PER_HOUR bash
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

Optional env: `PETABYTE_KEEP_AWAKE=true|false` keeps the machine awake while the seller agent runs (default ON; `false` opts out; the 15-second prompt defaults to on), `UNITS` (identical rentable units, default 1), `MAX_HOURS` (24),
`GPU_MODEL`/`GPU_COUNT`/`VRAM_GB` (manual override when `nvidia-smi` isn't present).

Keep-awake only blocks idle system sleep while the agent is running; it does not keep the display on or override manual sleep/lid actions. On install, no response or Enter leaves it ON; only an explicit `n` (or `PETABYTE_KEEP_AWAKE=false`) turns it off, and that explicit choice is remembered.

Security: the agent's signing key lives only at `/etc/petabyte/agent_ed25519.key`
and is the same identity used for attestation and signed job results.
