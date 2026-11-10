# Documentation Technique — Stack du Lab & Interactions Control Plane

## 1. Stack technique utilisée dans le lab

La stack du laboratoire se divise en deux étages : ce qui s'exécute **sur la machine hôte Linux** et ce qui s'exécute **dans le cluster Kubernetes**.

### A. Sur l'hôte Linux (Ubuntu)
- **KVM / libvirt (`qemu:///system`)** : héberge les deux machines virtuelles Ubuntu 24.04 LTS (`agentlab-server` et `agentlab-worker`, 4 Go de RAM chacune) connectées sur un réseau NAT isolé (`192.168.124.0/24`).
- **Control Plane « App Chapeau » (`app_chapeau/main.py`)** : serveur **Python 3 / FastAPI (`uvicorn`)** côté backend et **Vue 3 (via CDN, sans étape de build)** côté frontend. Il tourne sur l'hôte afin de pouvoir piloter à la fois le cluster Kubernetes, les VMs libvirt (pour les tests de panne de nœud) et ouvrir un proxy TCP local (`192.168.124.1:11434` → `127.0.0.1:11434`) vers le serveur **Ollama** de l'hôte.
- **Confinement dans `.private/`** : les disques `.qcow2`, les clés SSH éphémères, le fichier `kubeconfig`, le cache Helm, le `venv` Python et les snapshots des agents (`.private/agent_cache/<agent>/`) sont intégralement confinés dans `k8s-opencode/agent-platform-lab/.private/`.

### B. Dans les 2 VMs (le cluster Kubernetes)
- **K3s (`v1.31.6+k3s1`)** : Control Plane Kubernetes sur `agentlab-server` (`192.168.124.101`) et nœud worker sur `agentlab-worker` (`192.168.124.102`).
- **Longhorn CSI (`v1.8.0`)** : moteur de stockage bloc distribué, configuré avec une `StorageClass` `agent-longhorn` (`numberOfReplicas: 2`, `fsType: ext4`, `reclaimPolicy: Retain`) qui réplique chaque volume en temps réel entre les disques des deux VMs via `open-iscsi`.
- **Pods d'agents (`ghcr.io/anomalyco/opencode:2.0.0`)** : chaque agent isolé dispose dans le namespace `agent-lab` d'un `Deployment` (`strategy: Recreate`), d'un `ConfigMap` (`opencode.json` + `AGENTS.md`) et d'un `PersistentVolumeClaim` en mode **`ReadWriteOncePod`** monté sur `/data` (contenant `/data/workspace` et la base SQLite WAL `/data/opencode.db`).

---

## 2. Interactions entre l'App Chapeau, Kubernetes et l'hyperviseur

L'App Chapeau utilise le client **`kubectl`** (configuré avec `KUBECONFIG=.private/kubeconfig`) pour dialoguer avec l'API Server K3s (`https://192.168.124.101:6443`), et **`virsh`** (via les scripts `pause.sh` et `resume.sh`) pour piloter les VMs KVM :

| Action | Cible | Commande ou appel exécuté par `app_chapeau/main.py` |
| :--- | :--- | :--- |
| **Rafraîchir l'état (`GET /api/state`)** | API K8s + CRDs Longhorn + `virsh` | `kubectl -n agent-lab get deploy,pvc,pods -o json` + `kubectl -n longhorn-system get volumes.longhorn.io -o json` + `virsh -c qemu:///system domstate` |
| **Instancier un agent (`POST /api/agents/apply`)** | API K8s | Traduit le YAML `kind: DurableAgent` en 3 manifestes (`ConfigMap`, `PVC`, `Deployment` à `replicas: 0`) et exécute `kubectl apply -f -`. |
| **Réveiller / Hiberner (`⚡ Wake` / `💤 Hibernate`)** | API K8s | `kubectl -n agent-lab scale deploy/<agent> --replicas=1` (ou `0`) suivi de `kubectl -n agent-lab rollout status deploy/<agent>`. |
| **Exécuter une tâche / événement (`POST /api/agents/{name}/message`)** | Pod K8s (flux `exec`) | Si `replicas == 0`, réveille d'abord le Pod (`0 → 1`), puis lance `kubectl -n agent-lab exec <pod> -- opencode run --standalone --auto --format json ...` et parse le flux **NDJSON** ligne par ligne. |
| **Capturer le Snapshot (`WORKING → IDLE` et avant `1 → 0`)** | Pod K8s (`exec`) | `kubectl -n agent-lab exec <pod> -- sh -c ...` : interroge `/data/opencode.db` (sessions, messages, événements) et copie les fichiers de `/data/workspace` vers `.private/agent_cache/<agent>/`. |
| **`🔀 Migrate Node (A → B)`** | API K8s | `kubectl cordon <ancien_noeud>` → `kubectl -n agent-lab delete pod <pod>` → `kubectl rollout status` (le Pod redémarre sur l'autre VM et Longhorn rattache le volume bloc) → `kubectl uncordon <ancien_noeud>`. |
| **`⏹️ Stop Nœud Worker (-4 Go)` / `▶️ Start Nœud Worker`** | Hyperviseur KVM hôte | Exécute `./pause.sh --worker-only` (`virsh shutdown agentlab-worker`) ou `./resume.sh` (`virsh start agentlab-worker`) sur l'hôte Linux. |
| **`🌙 Pause Lab` / `☀️ Réveiller VMs`** | Hyperviseur KVM hôte | Exécute `./pause.sh` ou `./resume.sh` pour éteindre ou rallumer les deux VMs K3s sur l'hôte Linux. |
