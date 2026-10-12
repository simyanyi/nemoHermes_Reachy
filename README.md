# NemoHermes + Reachy infra-ops assistant

This setup checks a local single-node k3s cluster every 10 seconds. Reachy
speaks the initial status and any subsequent health change automatically after
NemoHermes reaches Ready. Telegram audio messages feed a Hermes conversation,
and Reachy speaks the answer.

## Give the monitor read access to k3s

The host's k3s service is already running. The monitor reads the user-owned
`/home/admin/.kube/config` file:

```bash
kubectl --kubeconfig /home/admin/.kube/config get nodes -o wide
```

The kubeconfig contains cluster credentials; keep it out of source control.
The monitor retries every 10 seconds and announces changes. The optional example workload is
in `/home/admin/Documents/k3s-demo.yaml`. k3s currently runs from its systemd
service with the default server settings.

## Demo workload

`k3s-demo.yaml` deploys three small HTTP replicas and a ClusterIP service in
`reachy-demo`. All three run on the current single node; adding Kubernetes
nodes requires another machine or VM. To reapply or inspect the workload:

```bash
kubectl --kubeconfig /home/admin/.kube/config apply -f /home/admin/Documents/k3s-demo.yaml
kubectl --kubeconfig /home/admin/.kube/config -n reachy-demo get deploy,pods,svc -o wide
```

The passive Reachy monitor announces Ready node, replica, and pod counts, recent
pod restarts, and warning events on startup and when they change. Healthy and
problem reports include small head and antenna poses when the motors are enabled.
`reachy_status` exposes current details for spoken requests.

## Change what Reachy monitors

Edit `/home/admin/Documents/reachy-telemetry.yaml`. Its `kubeconfig` points to
the Kubernetes cluster; `namespace` and `deployment` choose the workload.
Change `source` to `docker` to monitor local containers. `interval_seconds`
controls polling, `speak_every_check` controls repeated announcements, and
`gestures` controls telemetry poses. After an edit, run:

```bash
systemctl --user restart reachy-monitor.service
```

The separate `/home/admin/Documents/k3s-demo.yaml` defines the demo workload
and its replica count. Edit and reapply that manifest to change the workload
itself. Other telemetry systems need a source adapter in the Reachy bridge.

## Run

```bash
cd /home/admin/Documents/nemoHermes_Reachy
docker compose -f deploy/compose.tts.yaml up -d
systemctl --user daemon-reload
systemctl --user enable --now nemohermes-lifecycle.service
systemctl --user restart nemohermes-lifecycle.service
```

Send a Telegram voice message or audio file to use the
`telegram-reachy-audio` pipeline. Hermes transcribes it with its local
faster-whisper model, echoes the transcript in Telegram, and answers the
request in Telegram. The Reachy plugin recognizes the transcribed audio turn
and queues the same final answer for Reachy to speak, even when Hermes does
not select the skill.
The model is installed in the sandbox under `/sandbox/.hermes/models/`, and
`stt.provider: local` with `stt.echo_transcripts: true` is configured in
`/sandbox/.hermes/config.yaml`. The Reachy helper is installed as
`/sandbox/.hermes/plugins/reachy-output/speak_reply.py`.

The separate Reachy microphone service is disabled for this Telegram audio
path. To use the older live microphone mode, enable `reachy-voice.service`
and send `/start-reachy`; send `/stop-reachy` to turn listening off. Telegram
text messages retain their own conversation history. Voice transcription runs
locally with faster-whisper; Kokoro synthesizes Reachy's spoken replies.
The `/start-reachy` greeting uses a short pre-generated Kokoro WAV in
`.runtime/reachy-start-greeting.wav`, so activation does not wait for speech
synthesis. If the file is removed, the bridge falls back to live synthesis.

The Telegram slash skills use the narrowly scoped sandbox-to-host policy in
`deploy/reachy-bridge-policy.yaml`. It is applied to the current `my-hermes`
sandbox; if the host bridge address changes, update its pinned address and
reapply the policy before using the commands.

## Check

```bash
cd /home/admin/Documents/nemoHermes_Reachy
.venv/bin/reachy-bridge preflight
.venv/bin/reachy-bridge client status
systemctl --user status reachy-bridge.service reachy-monitor.service --no-pager
journalctl --user -u reachy-monitor.service -n 50 --no-pager
```

To update the existing `.venv` after a code or dependency change:

```bash
uv sync --locked --python 3.12 --group dev --group voice
systemctl --user restart reachy-bridge.service reachy-monitor.service reachy-voice.service
```

## Local models

| Role | Model | Runs in |
|---|---|---|
| Conversation and skill/tool selection | NVIDIA Nemotron 3 Nano Omni 30B Reasoning | NemoHermes via local vLLM |
| Reachy microphone transcription | faster-whisper `base.en` | Host CPU, local model files |
| Reachy speech | Kokoro `af_bella` | Local TTS container |

The STT package is in `.venv/lib/python3.12/site-packages`; its model is in
`.models/faster-whisper-base.en`. Kokoro runs in `nemohermes-reachy-tts`.
