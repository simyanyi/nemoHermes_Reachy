# NemoHermes → Wireless Reachy

Make your existing NemoHermes agent report Kubernetes telemetry through Reachy's
speaker, head and antennas. Hermes continues to run inside NVIDIA OpenShell.
This project does **not** start a second Hermes agent.

```text
Kubernetes / Prometheus / existing read-only telemetry tools
                         ↓
                NemoHermes in OpenShell
                  reachy-output plugin
                         ↓ bounded HTTP API, bearer authentication
                 Host: reachy-bridge :8787
                         ↓ Reachy daemon HTTP API :8000
            Wireless Reachy: reachy-mini.local
                   speaker + head + antennas
```

Only the host resolves the robot's `.local` hostname. The sandbox connects to
`host.openshell.internal:8787`. No robot SDK, GPU, USB passthrough, multicast,
WebRTC or audio device is needed inside OpenShell. For this **output-only** demo,
the host uses Reachy's HTTP movement and audio-upload APIs instead of streaming
microphone/video. Telemetry collection stays in your existing Hermes tools;
the bridge has no Kubernetes credentials and cannot change your cluster.

## What is included

- `src/reachy_bridge`: authenticated host service, fixed gestures, speech, queue,
  stop, deduplication and firmware preflight.
- `hermes-plugin`: four Hermes tools, using only Python's standard library.
- `deploy`: OpenShell policy fragment and optional user systemd unit.
- `examples`: explicitly synthetic incident/recovery payloads and a demo prompt.
- `tests`: service, robot API, speech, policy and plugin integration tests.

The original `reachy-mini-hermes-agent` checkout is unchanged. This is a separate
implementation: no embedded `AIAgent`, developer-specific voice path, shared
Python robot object, or dependency on ElevenLabs/OmniVoice.

## 1. Prepare the host

Run from this directory on the same LAN as Reachy:

```bash
cd /home/admin/Documents/nemoHermes_Reachy
uv sync --locked --group dev
uv run reachy-bridge init
```

`init` creates `.secrets/bridge-token` with mode `0600`; it never prints or
overwrites the token. This host already has an ignored `.env` configured for
Kokoro Bella. On a new host, copy `.env.example` to `.env` and edit it there.
`.env` and `.secrets` are ignored by Git.

The code's default speech uses the host's existing `libespeak-ng` (or `libespeak`) and voice
data directly. No command-line executable, extra Python speech package, API key,
or GPU is needed. These libraries and voice data were already present on this
host. Synthesis runs in an isolated Python subprocess and does not play through
the host's speaker. If deploying elsewhere, preflight reports a missing library;
provide the library/voice data there or use the optional speech server below.

The default is a cheerful feminine variant (`en-us+f3`), rate 175, pitch 65 and
pitch range 75. Tune `REACHY_VOICE`, `REACHY_SPEECH_RATE`, `REACHY_SPEECH_PITCH`
and `REACHY_SPEECH_RANGE` in `.env`, then restart the bridge. It remains a
formant-synthesized voice. This host uses Kokoro Bella for a natural voice.
Preview with `reachy-bridge client report --file examples/voice-sample.json`.

**This host is configured for Kokoro Bella**, a natural neural female voice.
It reuses the cached `text-to-speech:latest` photo-booth image and cached voices,
with CPU inference and offline model loading. Start only this speech service:

```bash
docker compose -f deploy/compose.tts.yaml up -d
```

The ignored local `.env` selects `REACHY_TTS=openai`,
`REACHY_TTS_BASE_URL=http://127.0.0.1:8880/v1`, `REACHY_TTS_MODEL=kokoro`,
and `REACHY_TTS_VOICE=af_bella`. Other cached options exposed by this small server
are `af_heart`, `af_nova` and `af_sky`. Nothing is downloaded, and the rest of the
photo-booth stack is not started. The API binds only to host loopback; OpenShell
still needs access only to the output bridge, never to the speech server.
On another host this compose file requires the image/model cache to exist
already (`pull_policy: never`). The cached image lacks its optional pronunciation
fallback model, so uncommon names may be skipped; use familiar wording in spoken
reports.

For another natural voice, configure an existing OpenAI-compatible speech server
in `.env` (`REACHY_TTS=openai`, base URL, model and voice). It must support
`POST /audio/speech` with `response_format=wav` and return PCM WAV. An optional
provider key lives in `REACHY_TTS_KEY_FILE` on the host only. This project does
not download speech models. To use gestures only, set `REACHY_TTS=none`
and send alerts with `speak=false`.

## 2. Verify Reachy without moving it

```bash
uv run reachy-bridge preflight
```

This only reads daemon status, backend-gated motor status, media status and
`/openapi.json`. It checks the
HTTP API used by the bridge and exits nonzero if the backend/API/TTS is not ready.
For the full demo, also check `motor_mode: enabled` and `media_available: true`.
Enable/wake the robot through its dashboard before sending an alert:
`http://reachy-mini.local:8000`.

During development on October 2, 2026, the robot resolved to `192.168.50.200`,
reported daemon version `1.9.0`, and exposed the required APIs. The daemon
summary continued to show `ready: false` after waking, while the backend-gated
motor endpoint returned `enabled` and live speech/gestures worked. The bridge
uses that direct endpoint for readiness and shows the summary for diagnostics.

If mDNS fails on the host, use `REACHY_ROBOT_URL=http://192.168.50.200:8000`
after confirming that address still belongs to your robot. Older firmware must
pass preflight; the bridge does not silently fall back to a host speaker.

## 3. Rehearse locally

On this host, stop the live `reachy-bridge.service` before starting a simulated
bridge in one terminal:

```bash
uv run reachy-bridge serve --dry-run
```

In another terminal, from the project directory:

```bash
uv run reachy-bridge client status
uv run reachy-bridge client report --file examples/critical.json
uv run reachy-bridge client event --event-id demo-checkout-critical-1
uv run reachy-bridge client report --file examples/recovered.json
uv run reachy-bridge client stop
```

Dry-run results explicitly say `simulated`; they do not contact the robot or TTS.
Stop this process before starting the live bridge. For live speech/gestures,
start `uv run reachy-bridge serve` without `--dry-run` and ensure
`REACHY_DRY_RUN=false` in `.env`.

The service defaults to loopback. On this host, the ignored `.env` binds
`172.18.0.1`, the current OpenShell Docker gateway interface. On another host,
set `REACHY_BIND` to an interface reachable from the sandbox, then restart the
bridge. Port 8787 must be reachable from the OpenShell
gateway network. Keep it off public networks; use a TLS reverse proxy if the
traffic crosses an untrusted network. Every output/status operation requires a
dedicated bridge token; unauthenticated `/healthz` returns only service health.

## 4. Install the Hermes plugin inside NemoHermes

The plugin is enabled in the current `my-hermes` sandbox. These commands repeat
the setup on a fresh sandbox.

Current NemoHermes uses `/sandbox/.hermes` for native Hermes state. Create the
plugin directory, upload the plugin's **contents**, and install only the dedicated
bridge credential (not any robot, Kubernetes or provider credential):

```bash
nemohermes my-hermes exec -- mkdir -p /sandbox/.hermes/plugins/reachy-output
openshell sandbox upload my-hermes ./hermes-plugin/plugin.yaml /sandbox/.hermes/plugins/reachy-output/plugin.yaml
openshell sandbox upload my-hermes ./hermes-plugin/__init__.py /sandbox/.hermes/plugins/reachy-output/__init__.py
openshell sandbox upload my-hermes ./hermes-plugin/settings.json /sandbox/.hermes/plugins/reachy-output/settings.json
openshell sandbox upload --no-git-ignore my-hermes .secrets/bridge-token /sandbox/.hermes/reachy-bridge-token
nemohermes my-hermes exec -- chmod 600 /sandbox/.hermes/reachy-bridge-token
nemohermes my-hermes exec -- hermes plugins list
```

Upload each file separately: uploading the whole directory creates a nested
`hermes-plugin` directory. Confirm the three files are directly inside
`/sandbox/.hermes/plugins/reachy-output`.
Enable the identifier reported by Hermes (normally `reachy-output`):

```bash
nemohermes my-hermes exec -- hermes plugins enable reachy-output
nemohermes my-hermes gateway restart
```

If your Hermes configuration restricts toolsets for the channel you use, add
`reachy` to that channel's existing enabled toolsets. Preserve its other toolsets.
Check in the actual Telegram/dashboard/CLI session that `reachy_status` is
available; loading a plugin does not necessarily override channel tool filters.

`hermes-plugin/settings.json` contains no secret. Its default is a dedicated
token file, not an environment variable. The scoped bridge token can only call
this output service. An operator-managed OpenShell credential binding can
instead inject the Authorization header: set `auth_mode` to `openshell` only
after configuring that binding. This repository does not create a provider or
assume an existing OpenShell secret is readable inside the sandbox.

Older NemoHermes releases with image-owned plugin enforcement need the plugin
baked into `/opt/hermes/plugins/reachy-output` using that release's custom-image
workflow. Do not weaken filesystem policy to work around plugin loading.

## 5. Permit only the bridge through OpenShell

Determine how the sandbox resolves the host, and the interpreter Hermes uses:

```bash
nemohermes my-hermes exec -- getent ahostsv4 host.openshell.internal
nemohermes my-hermes exec -- readlink -f /opt/hermes/.venv/bin/python
nemohermes my-hermes policy get > current-policy.yaml
```

Generate a merged policy, replacing the example IP with the address returned by
`getent`. This is the **host bridge address**, not Reachy's `192.168.50.200`:

```bash
uv run reachy-bridge policy --allowed-ip 172.18.0.1 --base current-policy.yaml > reachy-policy.yaml
```

Review the merged file. The helper retains unrelated rules, refuses to overwrite
an existing `reachy_output` entry and refuses a base containing
`[STRIPPED_BY_MIGRATION]`. Restore supported credential bindings before applying
any exported policy that contains those markers. The allowed executable must
match the installed Hermes interpreter; adjust the rule if it differs.

```bash
openshell policy set --policy reachy-policy.yaml --wait my-hermes
```

`deploy/openshell-policy.fragment.yaml` shows the exact rule. It allows only
GET health/status/event and POST report/stop to the bridge. The `/32` host
allowlist handles OpenShell's private-address guard. It does not grant direct
access to Reachy or the Kubernetes API. Keep the bridge on port 8787 unless you
also update the policy and plugin URL.

This host has the rule active as policy revision 2. It preserved Telegram and
the existing policy. The exported YAML files are ignored snapshots; re-export
and merge again after other policy changes or a sandbox rebuild.

If `host.openshell.internal` is unavailable in your topology, use a reachable
host LAN address consistently in `settings.json` and the policy (`--host`).
Do not use `localhost` from the sandbox to address the host.

Open `openshell term` to inspect denied requests. A gateway-port resolution
error from the NemoHermes host CLI must be resolved in that installation before
uploading/restarting/applying policy; it is not fixed by changing robot settings.

## 6. Run the infraops demo

To test Reachy before adding telemetry, open a direct chat with the Telegram bot
connected to `my-hermes` (or use the Hermes dashboard) and send:

> Use `reachy_status`. If Reachy is ready, call `reachy_report` with event ID
> `telegram-test-20261002-1`, severity `healthy`, source `telegram-test`,
> `speak=true`, `gesture=true`, and this exact summary: "Hello! This is Reachy
> speaking through NemoHermes with my Bella voice." Then check
> `reachy_event_status` for that ID until it completes. Do not inspect Kubernetes
> or perform other actions.

Use a new event ID for each new test. Reusing the same ID and payload is
idempotent and will not speak again. The Telegram bot is only the chat interface;
the Hermes plugin calls the authenticated host bridge, which sends audio and
gestures to wireless Reachy. The same tools work from the Hermes dashboard.

Kubernetes telemetry has not been configured yet. Once a read-only telemetry
source is provided, use the prompt in [examples/demo-prompt.md](examples/demo-prompt.md).

Use your existing read-only Kubernetes/Prometheus tools to establish the facts,
then call:

```json
{
  "event_id": "checkout-incident-42-critical-1",
  "severity": "critical",
  "summary": "Checkout has two restarting pods and an elevated error rate. I recommend checking the latest deployment and container logs. I have not changed the cluster.",
  "source": "demo-cluster/checkout",
  "speak": true,
  "gesture": true
}
```

Only use that wording when supported by actual observations. The example JSON
files explicitly label themselves as synthetic. Tools:

| Tool | Effect |
|---|---|
| `reachy_status` | Read backend readiness, media/TTS configuration, active job and queue |
| `reachy_report` | Queue a spoken summary and a small fixed severity gesture |
| `reachy_event_status` | Check the result using its event ID |
| `reachy_stop` | Cancel bridge output; does not disable motor torque |

`healthy` nods, `warning` tilts, `critical` turns left/right, `recovering` nods
gently, and `unknown` tilts the other way. Expressions use modest angles and
return to neutral before speech. The bridge never enables motors automatically.

An HTTP 202 / `queued` result is not delivery. Poll `reachy_event_status` after a
short wait. `completed` with `daemon_accepted` means Reachy accepted the commands
and the estimated playback interval passed; it is **not** sensor verification
that sound was audible or movement completed. Failures and simulation are explicit.

Reuse the same event ID **and identical payload** for retries. Changed incident
states need a new ID. Deduplication retains the last 256 events in memory and
resets on bridge restart. The single worker prevents overlapping bridge alerts,
queues at most eight, and drops queued items older than 120 seconds. Run one
service instance per robot. Stop other robot-control demos/apps before presenting;
this bridge cannot arbitrate with unrelated robot controllers.

## Optional host service and tests

On this host, `deploy/reachy-bridge.service` is linked and enabled as a user
systemd service. User lingering is enabled, so it survives logout. Its paths
are set for `/home/admin/Documents/nemoHermes_Reachy` and it reads the `.env`
in that directory. On another host, change the unit paths before linking.
Do not run a second manual bridge alongside the service.

```bash
uv run pytest -q
uv run ruff check src tests hermes-plugin
```

The tests use mock daemon transport and a simulated bridge; none moves hardware.
The plugin integration test starts an in-process HTTP test server and needs an
execution environment that permits its thread/event-loop wakeup sockets.

## References

- [Reachy daemon REST API](https://huggingface.co/docs/reachy_mini/en/API/rest-api)
- [Reachy media routes](https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/daemon/app/routers/media.py)
- [NemoHermes plugin lifecycle](https://docs.nvidia.com/nemoclaw/user-guide/hermes/manage-sandboxes/install-hermes-plugins)
- [OpenShell network policy](https://docs.nvidia.com/openshell/dev/how-it-works/policies/network-rules)
