# Dossier de réalisation — OpenCode v2 sur K3s / Longhorn (laboratoire reproductible)

**Statut : kit d'implémentation pour POC, non testé sur un hôte réel dans cet environnement.**

## But

Installer sur un hôte Ubuntu 26.04 deux VM Ubuntu 24.04 (K3s server + worker), un stockage Longhorn à deux réplicas, puis un Deployment OpenCode v2 à 0/1 réplique avec état SQLite sur PVC. Démontrer la mobilité contrôlée du Pod entre les deux VM et identifier les limites de la reprise durable. Le kit livre l'infrastructure **sans prétendre fournir déjà un opérateur**.

## Prérequis

- Hôte x86_64 Ubuntu 26.04 avec virtualisation Intel VT-x/AMD-V activée (`egrep -c '(vmx|svm)' /proc/cpuinfo`).
- Minimum conseillé : 8 vCPU logiques, 24 Gio RAM (32 recommandés), ~150 Gio disponibles SSD/NVMe.
- Connectivité Internet sortante pour Ubuntu cloud image, apt, K3s, Helm chart Longhorn et images OCI.
- Accès sudo ; `kubectl` et Helm 3 installés sur l'hôte ; paire de clés SSH ed25519 ; aucun secret dans le dépôt.
- Une plage NAT libvirt disponible (`192.168.124.0/24` par défaut). Le réseau K3s inter-VM doit autoriser 6443/TCP et la communication CNI (Flannel VXLAN UDP 8472, notamment).
- **Vérifier les versions** de `infra/lab.env` contre les matrices K3s/Longhorn et la disponibilité de l'image OpenCode avant exécution.

## Déploiement rapide (reproductible, confiné au dossier du projet)

Tous les artefacts générés (clés SSH éphémères, `known_hosts`, image officielle Ubuntu 24.04 LTS téléchargée, disques qcow2 des VMs, cache/config `helm` et `kubeconfig`) sont stockés dans `k8s-opencode/agent-platform-lab/.private/` afin de ne jamais polluer le `$HOME` de l'utilisateur :

```bash
cd k8s-opencode/agent-platform-lab
./up.sh
```

Pour tout nettoyer (destruction des VMs, du réseau `agentlab-net`, des clés SSH éphémères et du dossier `.private/`) :

```bash
./down.sh
# Ou pour conserver uniquement l'image de base ubuntu-base.img entre deux essais :
./down.sh --keep-image
```

## Console Web (« App Chapeau » FastAPI + Vue.js 3)

Une fois le lab démarré (`./up.sh`), lancez l'application web de supervision, d'instanciation (`0 ↔ 1`) et d'arbitrage HITL :

```bash
cd k8s-opencode/agent-platform-lab
./start-ui.sh
```

La console est accessible sur **http://localhost:8000** (le script crée un environnement virtuel isolé dans `.private/venv` et démarre automatiquement le relais TCP vers l'instance Ollama locale sur `192.168.124.1:11434`).

### Exécution étape par étape (identique à `./up.sh`)

```bash
cd k8s-opencode/agent-platform-lab
./infra/prepare-host.sh
./infra/provision.sh
./bootstrap/install-k3s.sh
./bootstrap/install-longhorn.sh
./bootstrap/deploy-agent.sh
./tests/wake.sh
./tests/check-db.sh
```

> `lab.env` n'est pas censé être versionné. Le script installe les invités Ubuntu 24.04 (Noble) volontairement, afin de limiter l'incertitude de compatibilité entre K3s/Longhorn et le noyau Ubuntu 26.04 hôte. Une validation d'Ubuntu 26.04 en VM constitue un test de compatibilité séparé.

## Test LLM optionnel

Créer une clé fournisseur via un mécanisme de secrets sécurisé ; pour un essai uniquement :

```bash
export KUBECONFIG="$PWD/.private/kubeconfig"
read -rs ANTHROPIC_API_KEY
kubectl -n agent-lab create secret generic opencode-provider   --from-literal="anthropic-api-key=$ANTHROPIC_API_KEY"
unset ANTHROPIC_API_KEY
# redémarrer le Pod pour charger le Secret ajouté après le démarrage
./tests/hibernate.sh && ./tests/wake.sh
export OPENCODE_MODEL='anthropic/REPLACE_WITH_SUPPORTED_MODEL'
./tests/initialize-session.sh
```

Le nom exact du modèle n'est pas figé : choisir un modèle réellement disponible pour le provider. Ne pas saisir de vrais identifiants dans les arguments des commandes sur un environnement partagé ; utiliser un fichier Secret protégé ou ESO à terme.

## Test de mobilité

```bash
./tests/hibernate.sh
./tests/wake.sh
./tests/migrate.sh
```

Le script `migrate.sh` cordonne le nœud courant, arrête proprement le Pod puis le relance sur l'autre nœud. Il contrôle le chemin et la présence de la base mais **pas** une reprise transparente à mi-exécution. Un test de crash est décrit dans `tests/README.md`.

## Commandes de diagnostic

```bash
export KUBECONFIG="$PWD/.private/kubeconfig"
kubectl get nodes -o wide
kubectl -n longhorn-system get pods
kubectl -n agent-lab get pvc,pods -o wide
kubectl -n agent-lab describe pvc opencode-sre-storage
kubectl -n agent-lab get events --sort-by=.lastTimestamp
kubectl -n agent-lab get volumeattachments.storage.k8s.io
```

## Arrêt et nettoyage

```bash
./tests/hibernate.sh
# ATTENTION : supprimer le namespace et le PVC entraîne des risques sur les données.
# Nettoyage hyperviseur après sauvegarde et arrêt des VM :
sudo virsh destroy agentlab-worker  # uniquement si active
sudo virsh undefine agentlab-worker --nvram
sudo virsh destroy agentlab-server
sudo virsh undefine agentlab-server --nvram
# Détruire manuellement uniquement les disques de ce LAB après contrôle.
```

La StorageClass est en `Retain` : supprimer un PVC ne supprime pas automatiquement les données. Prévoir une procédure contrôlée de purge ou récupération des PV. Ne pas utiliser ces commandes sur des VMs non dédiées au labo.

## Livrables

- `../article-k8s-eaia-plateform.md` : article technique complet (approche Enterprise AI Agent, séparation des besoins invariants et des choix du lab, cycle de vie en 8 étapes, API d'événements et REX).
- `infra/` : préparation hôte, VMs libvirt NAT + cloud-init, variables.
- `bootstrap/` : installation K3s, Longhorn et du runtime.
- `platform/` : namespace, StorageClass, PVC, ConfigMap, Deployment OpenCode v2.
- `agents/` : templates déclaratifs d'agents (`kind: DurableAgent`).
- `app_chapeau/` : console web multi-agents (instanciation YAML, machine à états 4 étapes, snapshots `/data/workspace` + SQLite, simulateur d'événements API et guide interactif).
- `tests/` : réveil, hibernation, migration et contrôle basique SQLite.
- `docs/ADR.md` : Architecture Decision Records (**ADR-001** Isolation 1 Agent = 1 Pod + 1 PVC RWOP, **ADR-002** Machine à états & Snapshots, **ADR-003** Découplage Définition / Réveil événementiel).
- `docs/ARCHITECTURE.md` : décisions, limites et risques.
- `docs/ROADMAP.md` : futur control plane, événements et HITL.

## Sources

- OpenCode v2 : https://opencode.ai/v2/docs/ et https://opencode.ai/v2/docs/cli/
- Permissions v2 : https://opencode.ai/v2/docs/permissions/
- K3s : https://docs.k3s.io/installation/requirements
- Longhorn : https://longhorn.io/docs/ et https://longhorn.io/docs/latest/deploy/install/
- KubeOpenCode (inspiration) : https://github.com/kubeopencode/kubeopencode
- Ambient Code Platform (référence Claude Code + Kubernetes) : https://github.com/ambient-code/platform
