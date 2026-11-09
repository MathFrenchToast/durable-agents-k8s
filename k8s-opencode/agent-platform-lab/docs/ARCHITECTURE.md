# Architecture / limites

```text
Ubuntu 26.04 host / KVM-libvirt
  +-- VM server Ubuntu 24.04 K3s (control plane + workloads)
  +-- VM worker Ubuntu 24.04 K3s (workloads)
            | Longhorn 2 replicas, volume CSI RWOP
            +-- Deployment OpenCode v2 replicas=0|1
                   | /data/opencode.db + home/state/cache on same PVC
```

## Frontières

- **Tenant** : namespace, ServiceAccount, secret, network policy et stockage distincts (à construire dans l'opérateur).
- **Instance d'Agent (`kind: DurableAgent`)** : **1 Instance d'Agent = 1 Pod dédié (`Deployment` `0|1`, `strategy: Recreate`) + 1 PVC Longhorn dédié (`ReadWriteOncePod`)** contenant `/data/opencode.db` (SQLite WAL) et `/data/workspace` (voir [ADR-001 dans `docs/ADR.md`](./ADR.md)).
- **Multi-instanciation d'un template d'agent** :
  - *Instances nommées* : N manifestes `DurableAgent` avec `metadata.name` distincts → N couples `(Deployment, PVC)` isolés.
  - *Pool indexé* : `StatefulSet` avec `volumeClaimTemplates` (`ReadWriteOncePod`) générant automatiquement `agent-0`, `agent-1` et leurs PVCs dédiés `storage-agent-0`, `storage-agent-1`.
- **Runtime** : Pod remplaçable, mis à l'échelle à `0` au repos (auto-hibernation après `idleTimeoutSeconds` ou mise en pause pendant un arbitrage humain `waiting_hitl`).
- **Session** : état OpenCode dans SQLite, pas une frontière de sécurité.

## Ce qui est implémenté ici

- Deux VM K3s (`agentlab-server` + `agentlab-worker`), stockage distribué CSI Longhorn (`agent-longhorn`, `ReadWriteOncePod`).
- Déclarations d'agents en YAML (`kind: DurableAgent` dans `agents/*.yaml`) compilées en `PVC` + `ConfigMap` (`opencode.json` + `AGENTS.md`) + `Deployment` + `ScaledObject` KEDA.
- Console Web multi-agents (« App Chapeau » dans `app_chapeau/main.py`) avec :
  - Auto-hibernation (`1 → 0`) après inactivité (`idleTimeoutSeconds`),
  - Workflow HITL en 2 phases (`opencode run --agent plan` en lecture seule + hibernation `0/1`, puis réveil/migration et reprise de la même session SQLite `--session <id>` avec `--agent build`),
  - Miroir hors-ligne (`.private/agent_cache/<agent_name>/`) permettant de consulter l'historique SQLite, prévisualiser et télécharger (`.zip`) l'arborescence `/data/workspace` même lorsque le Pod est hiberné à `0/1`.

## Ce qui n'est pas implémenté

Contrôleur CRD Agent, webhooks Alertmanager, schedules, HITL externe, MCP Grafana/K8s, sauvegardes, fencing applicatif, session-ID recovery automatisé, policies multi-tenants. Ne pas qualifier ce laboratoire de plateforme prête pour la production.

## Risques

1. Une seule machine physique : aucune tolérance à une panne de l'hyperviseur.
2. K3s single-server : arrêter la VM server rend l'API indisponible.
3. Longhorn à deux réplicas : test de mobilité, pas équivalent à trois domaines de panne.
4. RWOP limite les mounts simultanés mais ne constitue pas un *fencing* complet en cas de partition réseau ; superviser VolumeAttachment et Ownership.
5. SQLite doit rester sur un volume bloc ext4 monté par un écrivain ; ne pas partager la base via NFS.
6. `OPENCODE_DB` ne contient pas nécessairement TOUS les artefacts d'une session : le répertoire HOME/XDG et le workspace persistant sont à considérer.
7. Les permissions OpenCode protègent du modèle, pas contre tous les défauts du runtime ; RBAC, réseau et credentials restent requis.
8. Image Ubuntu téléchargée sans checksum immuable : verrouiller avec SHA256 pour reproductions déterministes.
9. Versions K3s et Longhorn choisies pour le dossier : vérifier la matrice de compatibilité et la disponibilité des tags avant déploiement.
