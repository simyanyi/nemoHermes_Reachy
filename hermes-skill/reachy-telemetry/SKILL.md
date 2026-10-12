---
name: reachy-telemetry
category: communication
description: Report observed Kubernetes or Docker health through Reachy's speaker.
---

# Reachy telemetry

Use this skill when the user asks Reachy for cluster health or a spoken telemetry report.

The host `reachy-monitor.service` starts with NemoHermes, checks the source configured in `/home/admin/Documents/reachy-telemetry.yaml` every 10 seconds, speaks its initial status, and announces health changes through the Reachy bridge. Reports include node, replica, pod, restart, and warning event counts for Kubernetes. Healthy and problem reports use small Reachy poses when motors are enabled. The monitor is the source of automatic reports; do not start another polling loop from a Hermes conversation.

For an on-demand report, call `reachy_status` and use its `kubernetes` or `docker` section, matching the configured source. State only observed status. If telemetry is unavailable, say that plainly; do not claim the source is healthy. Keep speech brief and avoid raw logs, credentials, or speculative causes.

If the user asks how to change the monitored cluster or workload, point them to `/home/admin/Documents/reachy-telemetry.yaml`: change `kubeconfig` for another Kubernetes cluster and `namespace`/`deployment` for another workload, then run `systemctl --user restart reachy-monitor.service`. Set `source: docker` to monitor local containers. The workload manifest `/home/admin/Documents/k3s-demo.yaml` controls the current demo deployment and its replica count.

When a separate Reachy announcement is requested, call `reachy_report` with a stable `event_id`, a severity matching the observation, and a summary of at most 600 characters. Check `reachy_event_status` with the same ID. A queued or completed event confirms bridge processing; verify robot delivery separately if needed.
