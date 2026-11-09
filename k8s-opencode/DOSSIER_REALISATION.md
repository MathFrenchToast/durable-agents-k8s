# Dossier de réalisation — Plateforme agentique OpenCode v2 sur K3s

**Version :** 0.1 — 6 novembre 2026  
**Cible :** laboratoire reproductible sur un hôte Ubuntu 26.04 / deux VM Ubuntu 24.04  
**Statut :** dossier et kit d'installation préparés, vérifiés statiquement ; exécution intégrale sur hyperviseur réel restant à valider.

## 1. Objet et résultats attendus

Construire un laboratoire représentant la couche d'exécution d'une future plateforme agentique DevOps : OpenCode v2 sert de *harness* (sessions, tools, skills, LLM), Kubernetes gère les Pods et leur cycle de vie, Longhorn persiste la base SQLite et rend son volume mobile entre nœuds. Le plan de contrôle métier sera développé dans une phase ultérieure en s'inspirant de KubeOpenCode ; Restate reste une option pour les attentes HITL et l'orchestration durable interservices.

**Résultat fonctionnel de la phase 1 :** (a) créer deux VM K3s, (b) installer Longhorn en deux réplicas, (c) lancer OpenCode sur un PVC dédié, (d) faire descendre le Deployment à zéro, (e) le redémarrer sur un autre nœud avec ses fichiers persistants.

**Ne sont pas promis dans cette phase :** reprise garantie au milieu d'un appel MCP, CRD/opérateur, scheduling natif OpenCode, sécurité multi-tenant complète, intégration Grafana et Kubernetes, HITL, auto-scaling KEDA, résilience à la perte physique de l'hôte.

## 2. Principes d'architecture

```text
Hôte Ubuntu 26.04 — KVM/libvirt (un seul domaine de panne physique)
  | réseau NAT 192.168.124.0/24
  +-- VM k3s-server  192.168.124.10 Ubuntu 24.04, 4 vCPU, 8 Gio
  |     +-- K3s control plane ET worker
  |     +-- disque virtio data 30 Gio /var/lib/longhorn
  +-- VM k3s-worker  192.168.124.11 Ubuntu 24.04, 4 vCPU, 8 Gio
        +-- K3s agent
        +-- disque virtio data 30 Gio /var/lib/longhorn

K3s
  |-- Longhorn CSI : replicas=2, filesystem ext4, mode RWOP
  |-- Namespace agent-lab
       |-- PVC 3 Gio longhorn /data
       |-- Deployment opencode-sre  replicas=0/1  strategy Recreate
            |-- image OpenCode v2 versionnée
            |-- OPENCODE_DB=/data/opencode.db
            |-- HOME + XDG dans /data
            |-- ConfigMap opencode.json v2 permissions deny by default
```

**Correspondance recherchée à terme :** un tenant contient plusieurs agents logiques ; un agent a une identité d'exécution, un espace persistant (PVC) et plusieurs sessions possibles dans une même base SQLite. Un runtime peut être absent (hibernation) ou actif (1 Pod écrivain). La session n'est pas un mécanisme de sécurité.

## 3. Choix et justifications

| Sujet | Choix POC | Justification / alternative |
|---|---|---|
| Hyperviseur | libvirt/QEMU/KVM | Disponible sur Ubuntu, automatisable sans service cloud |
| Provisionnement | Shell + virt-install + cloud-init | Découplé des versions du provider Terraform libvirt ; OpenTofu possible en v2 du labo |
| OS des VM | Ubuntu 24.04 | Réduit le nombre de nouveautés techniques par rapport à l'hôte 26.04 |
| Kubernetes | K3s serveur unique + worker | Test inter-nœuds sans simuler une HA du control plane |
| Stockage | Longhorn CSI 2 réplicas | Bloc mobile : ext4 monté à un seul endroit pour SQLite |
| Mode PVC | ReadWriteOncePod | Évite deux Pods simultanément sur le même PVC au niveau K8s/CSI si supporté |
| Harness | OpenCode v2 | Sessions, compétences, exécution non interactive, API |
| État | OPENCODE_DB et XDG sur PVC | Les fichiers de session survivent au Pod ; pas de NFS partagé |
| Contrôle | Scale Deployment 0/1 et scripts | Préfigure le futur reconciler sans coder l'opérateur dès le départ |
| Durable workflow | Aucun moteur ajouté en phase 1 | Mesurer d'abord les capacités réelles de reprise OpenCode v2 |

**Pourquoi ne pas utiliser une VM par Pod ?** Les VM jouent le rôle de nœuds K8s. Le Pod agent reste un workload K8s que l'ordonnanceur peut déplacer.

**Pourquoi 1 Instance d'Agent = 1 Pod dédié + 1 PVC `ReadWriteOncePod` (et non plusieurs instances co-localisées dans un même Pod) ?** (Voir [ADR-001 dans `agent-platform-lab/docs/ADR.md`](./agent-platform-lab/docs/ADR.md)) :
1. **Intégrité SQLite WAL & Isolation du Workspace** : OpenCode v2 persiste son journal de sessions dans `/data/opencode.db` (SQLite WAL) et travaille dans `/data/workspace`. Le mode `ReadWriteOncePod` (stable depuis Kubernetes v1.29) garantit au niveau CSI qu'un seul Pod sur tout le cluster peut monter le volume, éliminant tout risque de corruption SQLite, de *split-brain* ou de collision de fichiers entre deux agents.
2. **Granularité du Scale-to-Zero (`0 ↔ 1`) et Mobilité** : Un Pod dédié par instance d'agent permet d'hiberner (`replicas: 0`, **0 MiB RAM**) ou de migrer d'un nœud K3s à l'autre un agent inactif ou en attente d'arbitrage humain (`waiting_hitl`) sans interrompre les autres agents actifs.
3. **Multi-instanciation d'un template `kind: DurableAgent`** :
   - *Instances nommées par projet* : Déploiement de $N$ manifestes `DurableAgent` avec des `metadata.name` distincts (générant chacun un couple `Deployment` + `PVC` isolé).
   - *Pool indexé d'agents identiques* : Utilisation d'un `StatefulSet` Kubernetes avec `volumeClaimTemplates` (`accessModes: [ReadWriteOncePod]`, `storageClassName: agent-longhorn`), qui provisionne automatiquement des Pods isolés (`agent-0`, `agent-1`, …) dotés chacun de leur propre PVC Longhorn dédié (`storage-agent-0`, `storage-agent-1`, …).

## 4. Prérequis de la machine d'accueil

- CPU x86_64 avec support de virtualisation matérielle actif ; au moins 8 vCPU logiques disponibles.
- Mémoire : 24 Gio au minimum recommandé, 32 Gio idéalement.
- Espace libre SSD/NVMe : 150 Gio recommandés (2 disques système, 2 disques Longhorn, image de base, marge KVM et snapshots).
- Hôte Ubuntu 26.04 LTS avec sudo et sortie Internet ; bridge/NAT libvirt non conflictuel.
- Binaires `kubectl`, Helm 3 et clé publique SSH sur l'hôte.
- Accès à `cloud-images.ubuntu.com`, `get.k3s.io`, `charts.longhorn.io`, registres OCI requis.
- Aucune donnée de production ni token d'audit réel pendant le premier test.

**Ports :** depuis l'hôte vers les VM, SSH/TCP 22 et API K3s/TCP 6443 ; entre VM, trafic réseau CNI (Flannel VXLAN/UDP 8472 par défaut), et ports requis par Longhorn/iSCSI. Le réseau NAT libvirt n'est pas conçu pour exposer le laboratoire directement à Internet.

## 5. Mise en œuvre pas à pas

### Étape A — Préparer KVM/libvirt

```bash
cd agent-platform-lab
./infra/prepare-host.sh
```

Se déconnecter puis reconnecter pour appliquer les groupes `libvirt`/`kvm`. Vérifier `sudo virsh list --all`, `lsmod | grep kvm`, les ressources matérielles et l'absence de conflits avec le réseau `192.168.124.0/24`.

### Étape B — Configurer et créer les VM

```bash
cp infra/lab.env.example infra/lab.env
# Relire et adapter : IP, RAM, nombre de vCPU, clé SSH, URL image, versions.
./infra/provision.sh
```

L'installation télécharge une image officielle Ubuntu 24.04 ; fabrique deux disques système qcow2, deux disques de données, des fichiers cloud-init et un réseau libvirt NAT avec baux DHCP déterministes. Chaque invité monte `/dev/vdb` sur `/var/lib/longhorn`. **Ne pas changer les disques virtio sans adapter cette règle.**

Contrôles :

```bash
sudo virsh list --all
ssh ubuntu@192.168.124.10 'sudo cloud-init status --wait; lsblk; mount | grep longhorn'
ssh ubuntu@192.168.124.11 'sudo cloud-init status --wait; lsblk; mount | grep longhorn'
```

Pour reproduire à l'octet près, remplacer la référence `current` de l'image Ubuntu par une image datée, conserver son checksum SHA256 et verrouiller les versions des outils hôte. Le script livré est reproductible au sens procédural, **pas entièrement hermétique**.

### Étape C — Installer K3s

```bash
./bootstrap/install-k3s.sh
export KUBECONFIG="$PWD/.private/kubeconfig"
kubectl get nodes -o wide
```

Le script installe le serveur sur la première VM et attache la seconde avec le token K3s. Le kubeconfig est conservé dans `.private/kubeconfig` (mode 0600, non versionné).

**Critère :** deux nœuds `Ready`. Ne pas arrêter le serveur unique durant les tests de panne du worker.

### Étape D — Installer Longhorn

```bash
./bootstrap/install-longhorn.sh
kubectl -n longhorn-system get pods
kubectl get storageclass agent-longhorn
```

Le chart est déployé avec deux réplicas par volume et le chemin `/var/lib/longhorn`. Vérifier les prérequis iSCSI, la disponibilité du CSI, la santé des deux nœuds Longhorn et la conformité versionnelle K3s/Longhorn.

### Étape E — Installer OpenCode v2

```bash
./bootstrap/deploy-agent.sh
kubectl -n agent-lab get deploy,pvc
./tests/wake.sh
./tests/check-db.sh
```

Le manifeste définit `replicas: 0` initialement, `strategy: Recreate`, `ReadWriteOncePod`, exécution non root, restrictions Linux et permissions OpenCode v2 *deny-by-default*. Le conteneur demeure disponible grâce à `sleep infinity` afin de pouvoir lancer `opencode` en `kubectl exec` pour les premiers essais. **Ce n'est pas encore un serveur HTTP OpenCode opérationnel ou un contrôleur.**

La base ne sera pas forcément créée avant une première session ; `opencode debug paths db` affiche son chemin sans démarrer le serveur.

### Étape F — Créer une session puis tester la mobilité

Configurer un secret fournisseur LLM de test (la marche à suivre est dans `README.md`), choisir un modèle supporté puis :

```bash
export OPENCODE_MODEL='anthropic/REPLACE_WITH_SUPPORTED_MODEL'
./tests/initialize-session.sh
./tests/hibernate.sh
./tests/wake.sh
./tests/migrate.sh
```

`migrate.sh` cordonne le nœud de départ, arrête le Pod, redémarre celui-ci sur un autre nœud et contrôle le stockage. Le cordon est retiré automatiquement en fin de script. **Migration propre ≠ reprise après crash**.

## 6. Plan de tests et critères de recette

| Test | Procédure | Acceptation | Preuve à conserver |
|---|---|---|---|
| T01 Bootstrap | `kubectl get nodes` | 2 × Ready | sortie nodes, versions |
| T02 Stockage | PVC `Bound` | 2 réplicas Longhorn sains | vue Longhorn, PV/PVC |
| T03 Base | run session, `check-db.sh` | `OPENCODE_DB` existe | logs et fichier SQLite |
| T04 Hibernation | `hibernate.sh` | 0 Pod, PVC intact | événements, mounts |
| T05 Réveil | `wake.sh` | Pod Running, état conservé | UID, chemin base |
| T06 Déplacement | `migrate.sh` | nouveau nœud, même PVC | node source/destination |
| T07 Crash pendant LLM | kill Pod pendant run | mesure : perte ou reprise ? | flux d'événements, historique |
| T08 Crash pendant MCP | outil artificiel à effet traçable | nombre d'appels/duplicats documenté | identifiants par appel |
| T09 Panne worker | stopper la VM worker (pas server) | mesurer délai de recovery | CSI VolumeAttachment, logs |
| T10 Concurrent startup | tenter deux runtimes même PVC | refus du second, pas deux écrivains | événements admission/CSI |

**Pour T07/T08 :** consigner `sessionID`, `executionID` (si accessible), timestamps, appels LLM et appels MCP ; ne conclure à la durabilité interne que sur observation. OpenCode peut préserver une conversation sans reprendre automatiquement une action en cours. La non-duplication d'outils externes n'est pas garantie par la seule base SQLite.

## 7. Stratégie de sécurité et multitenancy future

1. Un namespace et un ServiceAccount par tenant ou niveau de confiance, avec RBAC limité et interdiction d'exécutions privilégiées.
2. Credentials par agent et de courte durée ; secrets gérés par ESO plutôt que fichiers en clair dans un ConfigMap ou un Git.
3. Stockage isolé par agent, chiffrement au repos selon moteur CSI, sauvegardes/restauration éprouvées.
4. NetworkPolicies pour limiter l'egress de l'agent aux fournisseurs LLM, MCP et API explicitement autorisées.
5. Audit des appels aux outils, des changements de droits, des décisions humaines et des déplacements de runtime.
6. Concurrence : un seul propriétaire de la base SQLite à la fois. RWOP et Deployment Recreate sont nécessaires mais pas une preuve de fencing complet lors d'une partition réseau.
7. Gestion des sessions anciennes : retirer les credentials au réveil si les droits de l'identité ont changé.
8. Pour un environnement mutualisé permanent, n'héberger dans un processus OpenCode commun que des tâches appartenant au **même domaine de confiance**.

## 8. Futur control plane (phase non livrée)

**API envisagée :** une CRD `AgentRuntime` contenant identité, tenant, image et politique d'exécution, référence PVC, config de skills/outils, idle timeout et état souhaité. Son contrôleur (reconciler) garantit l'unicité du runtime et réconcilie les événements `Wake`, `Hibernate` et `Recover`.

**Transitions d'états :** `Sleeping → Starting → Ready → Busy → Idle → Sleeping`, complétées par `Recovering` et `Failed`. Les actions externes deviennent idempotentes par clé d'invocation et déduplication.

**Reprise :** le contrôleur doit pouvoir retrouver la session, lancer OpenCode sur le PVC, distinguer reprise de session et redémarrage de tâche, tracer les appels potentiellement répétés. Avant d'accepter un nouveau propriétaire d'une base, il doit établir que l'ancien n'a plus la possibilité d'écrire. Un simple Lease Kubernetes peut insuffisamment protéger les partitions réseau.

**Triggers :** morning review via CronJob (au départ), alertes via Alertmanager → Webhook validé → file durable ou tâche idempotente. Le scheduling natif OpenCode v2 peut être étudié pour les agents permanents, mais un Pod hiberné ne peut pas faire tourner lui-même son cron.

**HITL :** réception d'une question/permission dans un bus d'événements, stockage hors du Pod, notification Teams ou Web UI, authentification du décideur, délai d'expiration, réveil, et reprise de la session. Restate reste un candidat pour le workflow externe, sans réimplémenter la boucle OpenCode.

## 9. Runbook et dépannage

- **Pod Pending :** inspecter `kubectl describe pod` et événements ; vérifier PVC et disponibilité du nœud.
- **Volume Multi-Attach :** attendre le détachement propre, inspecter `VolumeAttachment`, l'état Longhorn et l'ancien Pod. Ne pas forcer la libération sans preuve de fencing.
- **Longhorn dégradé :** vérifier `open-iscsi`, `iscsid`, `/var/lib/longhorn`, nombre de réplicas, espace disque et connectivité inter-VM.
- **SQLite absent :** déclencher une session avec un provider configuré ; `opencode debug paths db` n'initialise pas la base.
- **OpenCode refuse l'image ou la commande :** vérifier image/tag v2, architecture du binaire, shell disponible, `opencode --help` dans le conteneur.
- **Timeout K3s :** serveur joignable sur TCP/6443, token, TLS SAN, journaux `journalctl -u k3s` / `k3s-agent`.
- **Crash Hyperviseur :** les deux VM disparaissent simultanément, ce test n'est pas représentatif d'une HA multisite.

## 10. Points ouverts / décisions à prendre

- Valider l'image OpenCode v2 choisie sur un nœud x86_64 et son comportement non-root ; adapter le manifeste si l'image ne fournit pas `sh`.
- Figer et tester les versions exactes K3s, Longhorn, Ubuntu guest et installer avec une somme SHA256 vérifiée.
- Confirmer le support `ReadWriteOncePod` par le pilote CSI Longhorn retenu ; sinon revenir à RWO avec un verrou robuste et *fencing*.
- Vérifier quels autres chemins OpenCode doivent être sur le PVC (workspace, secrets locaux, snapshots et caches).
- Déterminer la stratégie de sauvegarde SQLite (backup cohérent, pas copie à chaud du seul fichier DB avec WAL actif).
- Choisir l'interface du control plane : API HTTP, CRDs Kubernetes ou les deux.
- Définir critères quantitatifs : temps de wake, temps de migration, coût RAM, latence LLM, fréquence d'échec et budgets par tenant.

## 11. Références officielles

- OpenCode v2 : https://opencode.ai/v2/docs/
- OpenCode v2 CLI / `OPENCODE_DB` : https://opencode.ai/v2/docs/cli/
- Permissions v2 : https://opencode.ai/v2/docs/permissions/
- K3s requirements : https://docs.k3s.io/installation/requirements
- K3s quick start : https://docs.k3s.io/quick-start
- Longhorn : https://longhorn.io/docs/
- Longhorn install : https://longhorn.io/docs/latest/deploy/install/
- KubeOpenCode (inspiration) : https://github.com/kubeopencode/kubeopencode
- SQLite WAL : https://sqlite.org/wal.html

---

**Critère de fin de phase 1 :** les tests T01 à T06 sont reproductibles sur un hôte vierge ; T07 à T10 ont un protocole, des traces et des conclusions documentées, sans promettre de reprise « exactly once ». Le passage à l'opérateur et aux credentials d'audit n'est engagé qu'après validation des fondations.
