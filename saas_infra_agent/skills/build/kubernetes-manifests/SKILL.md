---
name: kubernetes-manifests
description: Kubernetes manifest patterns for the Build agent — Deployment, Service, Ingress, HPA, ConfigMap/Secret wiring, and resource requests/limits. Use when the deployment target in the architecture plan is Kubernetes (EKS/GKE/AKS or generic).
---

# Kubernetes Manifests (Build Agent Output Contract)

## File Layout

```
k8s/
├── namespace.yaml
├── deployment.yaml
├── service.yaml
├── ingress.yaml        # only if the plan exposes HTTP publicly
├── hpa.yaml            # only if the plan requires autoscaling
├── configmap.yaml      # non-secret config
└── secret.example.yaml # placeholder keys only — real values applied out-of-band
```

## Deployment Pattern

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  namespace: my-app
  labels: {app: my-app}
spec:
  replicas: 2
  selector:
    matchLabels: {app: my-app}
  template:
    metadata:
      labels: {app: my-app}
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: app
          image: my-app:0.1.0          # pinned tag, never :latest
          ports: [{containerPort: 8000}]
          envFrom:
            - configMapRef: {name: app-config}
            - secretRef: {name: app-secrets}
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {cpu: "1", memory: 512Mi}
          readinessProbe:
            httpGet: {path: /health, port: 8000}
            initialDelaySeconds: 5
          livenessProbe:
            httpGet: {path: /health, port: 8000}
            initialDelaySeconds: 15
```

## Rules

- **Every container** has resource requests + limits, readiness + liveness
  probes, and a pinned image tag. No exceptions — HPA and scheduling depend on it.
- **Config vs secrets**: plain config in a ConfigMap; secrets referenced by name
  only, with a `secret.example.yaml` showing the expected keys and placeholder
  values. Never write real secret values into manifests.
- **Replicas and HPA bounds come from the plan** (user counts / latency targets);
  state the mapping assumption in the summary if the plan gives no numbers.
- HPA targets ~70% CPU utilization unless the plan says otherwise:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: {type: Utilization, averageUtilization: 70}
```

- Stateful components (DB, vector store) that the plan puts in managed cloud
  services stay in Terraform — do NOT generate StatefulSets for them unless the
  plan explicitly self-hosts.
- Label everything with `app` (and `environment` when multi-env) so Services,
  NetworkPolicies, and monitoring selectors line up.

## Related Skills

- `dockerfile` — the image these manifests deploy
- `terraform-scaffold` — cluster + managed services provisioning (e.g. via `eks` skill)
- `eks` — AWS-specific cluster setup
