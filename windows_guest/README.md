# windows_guest — in-guest runner for Adobe After Effects / Premiere Pro attempts

Runs **inside** a fresh first-party EC2 Windows Server 2022 GPU instance (g4dn, AMI built by
`scripts/adobe_host/image/`), one instance per attempt. The control plane (`lumaris_api`) launches the
instance with the attempt's config in its user data, serves the attempt-token API, and terminates the
instance after `/done`. There is no host relay: the guest talks HTTPS to the API and moves files to/from
object storage through presigned URLs. The shared contract is `../adobe_contract.py` (PROTOCOL 2); a copy
ships beside the runner.

## What runs where

| Where | What |
|---|---|
| control plane | launches/terminates the instance, user data, attempt-token API (manifest, input_url, event, output_url, done), presigned storage URLs |
| guest, logon of the job user | `python runner.py` (autologon interactive session — Premiere needs a desktop) |
| guest, `AfterFX.com -noui -r ae_inspect.jsx` | AE inspection (trusted script; request/report files in the ctl dir) |
| guest, `aerender.exe` | AE render to the image's lossless output module |
| guest, Premiere Pro + UXP plugin `uxp/` | inspect / exportSequence through the loopback bridge `127.0.0.1:47811` |
| guest, `ffmpeg`/`ffprobe` | MKV delivery (`presets.py`), validation, full-decode check |
| guest, `nvcuda.dll` | VRAM scrub before `/done` (`vram_scrub.py`) |

Flow: `GET {base}/manifest` → re-validate against the contract → `GET {base}/input_url` → stream the bundle
from the presigned URL (size cap, sha256; render mode must match `input.sha256`) → `bundle.py` safe extract
to `<workspace>\bundle` → adapter → `POST {base}/output_url` → presigned `PUT` of the report JSON or the
final MKV → VRAM scrub → `POST {base}/done` (with `uploaded`). Hard deadline `manifest.deadline_s` and
no-progress limit `limits.no_progress_s` kill the whole child tree (Windows Job Object with
`KILL_ON_JOB_CLOSE`; POSIX process group in tests). Progress events are coalesced to ≤ 2/s. The token
is never logged (`common.redact`); presigned URLs are never logged. Mode `scrub` = VRAM scrub + done only.

## Config: EC2 user data over IMDSv2

The runner reads its config from the instance's user data, through IMDSv2 only, bypassing any proxy:
`PUT http://169.254.169.254/latest/api/token` (`X-aws-ec2-metadata-token-ttl-seconds: 300`), then
`GET http://169.254.169.254/latest/user-data` with `X-aws-ec2-metadata-token`. 2 s timeouts, retried for
up to 2 minutes because IMDS can lag right after boot. User data is JSON ≤ 4 KB:

```json
{"pb_relay": "https://api.example/adobe/guest/v1", "pb_token": "<64 lowercase hex>", "pb_attempt": "<[A-Za-z0-9._-]{1,80}>"}
```

`adobe_contract.check_user_data` enforces: `pb_relay` is `https://` (http only for `127.0.0.1`/`localhost`
and only with `--dev`), no userinfo, no query or fragment, path ends with `/adobe/guest/v1`. Anything else
exits before a single API call. `PB_RELAY`/`PB_TOKEN` env vars are honoured only with `--dev` (tests) and
skip IMDS; the IMDS base URL is overridable only as a function argument (`runner.discover(dev, imds_base)`).

## API (protocol 2)

All calls are relative to `pb_relay`, carry `Authorization: Bearer <pb_token>`, use JSON bodies, 30 s
timeouts, never follow redirects (any 3xx is an error) and retry with backoff only on 5xx / connection
errors. Storage transfers use a separate opener with **no** Authorization header.

| Call | Request | Response |
|---|---|---|
| `GET {base}/manifest` | — | guest manifest (`build_guest_manifest`); `input = {"size": int, "sha256": hex\|null}` |
| `GET {base}/input_url` | — | `{"url": "<presigned https GET>", "size": int}`; `size` must equal `manifest.input.size` |
| `POST {base}/event` | one `validate_event` object, ≤ `MAX_EVENT_BYTES` | 2xx |
| `POST {base}/output_url` | `{"kind": "output"\|"report", "bytes": int, "sha256": hex}` | `{"url": "<presigned https PUT>", "headers": {str: str}}` |
| presigned `PUT url` | the file, exactly the returned headers + `Content-Length`, streamed, never chunked | 2xx |
| `POST {base}/done` | `validate_done` object; when completed with an upload also `"uploaded": {"kind", "sha256", "bytes"}` | 2xx; retried (~4 min); first one wins server-side |

`report` is the inspection report JSON (≤ `MAX_REPORT_BYTES`), `output` the final MKV (≤
`limits.max_output_bytes`). Returned upload headers may not include `Authorization`, `Content-Length`,
`Transfer-Encoding` or `Host`. Failure causes the guest can report in `done` are `JOB_FAILURES` plus
`input_fetch_failed` / `result_upload_failed` (`GUEST_FAILURES`).

## Image layout

```
C:\ProgramData\Petabyte\
  guest-profile.json          trusted, written at image build, read-only to the job user
  runner\                     runner.py bundle.py ae_adapter.py ae_inspect.jsx premiere_adapter.py
                              presets.py vram_scrub.py common.py adobe_contract.py (copy)
  presets\pb_h264_aac_match.epr
D:\pbjob\                     workspace (NTFS, job user writable): bundle\ (extracted upload), ctl\ (request,
                              report, intermediates, final MKV) — both recreated per attempt
```

## guest-profile.json

```json
{"aerender": "C:\\Program Files\\Adobe\\Adobe After Effects 2026\\Support Files\\aerender.exe",
 "afterfx":  "C:\\Program Files\\Adobe\\Adobe After Effects 2026\\Support Files\\AfterFX.com",
 "premiere": "C:\\Program Files\\Adobe\\Adobe Premiere Pro 2026\\Adobe Premiere Pro.exe",
 "ffmpeg": "C:\\ProgramData\\Petabyte\\bin\\ffmpeg.exe", "ffprobe": "C:\\ProgramData\\Petabyte\\bin\\ffprobe.exe",
 "ae_version": "26.x", "premiere_version": "26.x", "workspace": "D:\\pbjob",
 "ae_templates": {"mkv_h264_aac": {"rs": "Best Settings", "om": "Petabyte Lossless", "ext": ".avi"}},
 "premiere_presets": {"mkv_h264_aac": "C:\\ProgramData\\Petabyte\\presets\\pb_h264_aac_match.epr"},
 "dynamic_link_qualified": false, "nvenc_qualified": false,
 "uxp_plugin_id": "market.petabyte.render",
 "uxp_plugin_data": "C:\\Users\\pbjob\\AppData\\Roaming\\Adobe\\UXP\\PluginsStorage\\PPRO\\26\\External\\market.petabyte.render\\PluginData"}
```

`ext` is the container the AE output module writes (default `.avi`). `uxp_plugin_data` is optional; without
it the adapter globs `%APPDATA%\Adobe\UXP\PluginsStorage\PPRO\*\*\<id>\PluginData`. `nvenc_qualified`
switches the encode to `h264_nvenc`. Tool paths are **never** taken from the manifest or the bundle.

## UXP plugin (Premiere)

`uxp/manifest.json` (manifest v5, host `premierepro` ≥ 25.6, network permission for
`http://127.0.0.1:47811` only, `localFileSystem: plugin`), `index.html`, `index.js`. Package with the UXP
Developer Tool ("Package" → `.ccx`), install at image build with
`"C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install petabyte-render.ccx`
(or load it through the Developer Tool for a dev image). Before Premiere launches, `premiere_adapter.py`
writes a per-run token to `PluginData\pb_token.txt`; the plugin sends it as `X-PB-Token` and polls
`GET /job` every 500 ms. Verbs: `inspect` (Project.open → getSequences → frame size, timebase ticks, end
time, audio-track clip presence, offline items) and `render` (optional in/out via `executeTransaction`,
`EncoderManager.exportSequence(seq, IMMEDIATELY, out, epr, exportFull)`, encoder events when
available). The export promise alone is never success: Python waits for the file to be stable (3 checks)
and ffprobes it against the inspection's expectations.

## Autologon notes

The image build below is scripted as an EC2 AMI in `scripts/adobe_host/image/` (`pb-adobe-render.pkr.json`,
`provision.ps1`, `build_ami.sh`, pinned `versions.json`; runbook step 5 in `docs/ADOBE_RENDERING.md`). It
stores the `pbjob` autologon password as the LSA `DefaultPassword` secret (not the registry value) and
installs the UXP plugin as `pbjob` through a one-shot scheduled task; both are unproven until a real build.

The runner must run in an **interactive** desktop session (Session 0 services cannot drive Premiere).
Image build: local non-admin user `pbjob`, autologon (`Sysinternals Autologon` or
`HKLM\...\Winlogon\AutoAdminLogon`/`DefaultUserName`/`DefaultPassword`), and a logon task
(`schtasks /Create /SC ONLOGON /RU pbjob /IT`) running `pythonw runner.py`. Disable lock screen /
screensaver / sleep, Windows Update, Defender real-time scanning of `D:\pbjob`, and first-run dialogs for
both apps (sign the Adobe account in at image build; the instance's egress is limited to the API and
storage). The instance is terminated after every attempt, so nothing in the guest needs to be cleaned
beyond the VRAM scrub.

## Qualification gates (only a real Windows + Adobe install can prove these)

1. **AE inspect** — `AfterFX.com -noui -r ae_inspect.jsx` runs unattended, `$.getenv("PB_CTL")`
   reaches the script, `app.open` with `beginSuppressDialogs` never blocks on a dialog,
   `usedFonts`/`isSubstitute`, `app.effects`, `workingSpace` behave as assumed on the pinned version.
2. **AE render** — `aerender` argv (`-RStemplate`/`-OMtemplate` names, `-output` extension for the
   lossless OM, `-s/-e` numbering vs. `displayStartFrame`), `PROGRESS:` line format and the error strings
   `classify_error_line` keys on; comp names with quotes survive aerender's own argument parsing.
3. **UXP autoload** — the plugin's `entrypoints.setup → plugin.create` fires at Premiere start without a
   user opening the panel; if not, the image must open the panel in the saved workspace.
4. **exportSequence semantics** — what the promise resolves to, whether `EVENT_RENDER_*` events exist on
   `ppro.EncoderManager`/`EventManager.addGlobalEventListener`, in/out range through
   `createSetInPointAction`/`TickTime.createWithTicks`, `OpenProjectOptions` flags suppress all dialogs.
5. **User-data delivery on the real AMI** — IMDSv2 answers the job user's `PUT /latest/api/token` +
   `GET /latest/user-data` inside the autologon session (hop limit, no proxy, EC2Launch v2 not consuming
   or rewriting the JSON), within the 2-minute boot wait.
6. **CUDA scrub** — `nvcuda.dll` loads for the job user on the g4dn GPU; the amount allocatable vs. total
   VRAM; time to memset/verify on the qualified card (chunk size tuning).
7. **Audio heuristics** — AE `layer.hasAudio && audioEnabled`, Premiere audio-track clip presence match
   what the apps actually render (silent outputs are tolerated, missing-when-expected is a failure).
8. **Dynamic Link / MOGRT** detection in `.prproj` XML on real projects (tag names, `.aep` paths).

Everything in `windows_guest_test.py` (bundle attacks, argv, parsing, presets validation, user-data
validation, IMDSv2 flow against a fake IMDS, runner end-to-end against a fake API + fake presigned storage,
process-tree kill) runs on Linux and Windows without Adobe.
