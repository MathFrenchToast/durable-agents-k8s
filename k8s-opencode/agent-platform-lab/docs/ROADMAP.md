# Étapes après le laboratoire

**Phase 0 — Infrastructure** : 2 VM, K3s, Longhorn, test montage et déplacement RWOP.

**Phase 1 — OpenCode v2** : démarrer une session, vérifier `OPENCODE_DB`, arrêter/redémarrer, puis tester le crash pendant `opencode run` avec traces des appels outils.

**Phase 2 — Control plane** : CRD `AgentRuntime` (tenantRef, identityRef, configRef, volumeClaim, desiredState, status), reconciler 0/1, idempotence, finalizers, exclusion et fencing sur changement de nœud ; introduire Lease Kubernetes (pas seul en cas de partition).

**Phase 3 — Audit** : MCP Grafana et Kubernetes en lecture seule, ServiceAccounts namespace-scoped, RBAC read-only, Secret via ESO, egress policies, audit logs, token redaction.

**Phase 4 — Événements** : CronJob ou schedules v2 selon durée de vie, Alertmanager -> récepteur validant/filtrant -> invocation idempotente, déduplication, quotas.

**Phase 5 — HITL** : adapter les événements permission/question OpenCode v2 à un état persistant externe, UI/Teams, expiration, authentification, reprise avec session ID ; évaluer Restate si attente durable utile.

**Phase 6 — Multitenant** : tenant namespace, credentials séparés, PVC et encryption, quotas, NetworkPolicies, RBAC/OPA, modèles et budgets par tenant, traces isolées.

## Schéma CRD envisagé (NON IMPLEMENTÉ)

```yaml
apiVersion: agents.example.io/v1alpha1
kind: AgentRuntime
metadata:
  name: sre-irium
spec:
  tenantRef: irium
  identityRef: sre-audit
  persistence:
    pvcName: agent-sre-irium
  runtime:
    image: ghcr.io/anomalyco/opencode:2.0.0
    idleTimeout: 30m
    desiredState: Sleeping
```
