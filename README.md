# Agents IA Durables sur Kubernetes — Comparatif de Deux Architectures

Ce dépôt rassemble les laboratoires et le code source évaluant deux architectures d'exécution d'agents IA sur Kubernetes. L'objectif commun aux deux approches consiste à exécuter des tâches longues, tolérer les pannes de nœuds ou de Pods, suspendre l'activité lors d'une validation humaine (*Human-in-the-Loop*, HITL) sans consommer de ressources inutiles, et conserver une couche de pilotage légère au-dessus des agents.

---

## Vue d'ensemble du dépôt

```text
.
├── durable-workflow/         # Approche 1 : Moteur d'exécution durable (Restate) + Agent codé via SDK
│   ├── dossier.md            # Spécifications initiales
│   ├── v0-minimal/           # Implémentation directe sur Restate VirtualObject + KEDA
│   └── v1-extensible/        # Implémentation hexagonale (Ports & Adapters, LiteLLM, Phoenix, Argo CD)
│
└── k8s-opencode/             # Approche 2 : Primitives Kubernetes + Harness existant (OpenCode v2)
    ├── article-k8s-eaia-plateform.md
    ├── DOSSIER_REALISATION.md
    └── agent-platform-lab/   # Lab K3s multi-nœuds (KVM/libvirt), stockage répliqué Longhorn et Pod OpenCode
```

| Critère | Approche 1 : Durable Execution + SDK (`durable-workflow/`) | Approche 2 : Kubernetes + Harness (`k8s-opencode/`) |
| :--- | :--- | :--- |
| **Principe d'exécution** | Journalisation d'événements (*Event Sourcing*) via Restate | Processus agentique prêt à l'emploi (**OpenCode v2**) dans un Pod dédié |
| **Implémentation de l'agent** | Écriture intégrale de la boucle agentique, des outils et du routage LLM en Python | Configuration déclarative (`opencode.json`, skills, MCP, permissions) |
| **Persistance de l'état** | Journal distribué Restate (clé/valeur + historique d'appels RPC) | Base SQLite (`OPENCODE_DB`) + workspace sur volume bloc répliqué **Longhorn** (`RWOP`) |
| **Granularité de reprise sur crash** | À l'étape près (`ctx.run`), sans ré-exécuter les appels déjà journalisés | Au tour de conversation (`opencode run --continue`), avec fichiers conservés sur le PVC |
| **Attente humaine (HITL)** | Promesse durable (`awakeable`) libérant le worker Python (0 % CPU/RAM) | Interception d'événement permission/question par l'App Chapeau et mise à l'échelle `replicas: 0` |
| **Coût mémoire par agent actif** | Faible par invocation (workers mutualisés sans état local) | ~166 MiB RSS par Pod `opencode serve` actif ; 0 MiB au repos (`replicas: 0`) |

---

## Approche 1 — Exécution durable et boucle agentique codée via SDK (`durable-workflow/`)

La première approche repose sur un moteur de *Durable Execution* (**Restate**). L'agent est écrit comme un service ou un *Virtual Object* dont chaque étape à effet de bord (appel LLM, exécution d'outil, appel API) est encapsulée dans une primitive de journalisation (`ctx.run`).

### Fonctionnement et compromis techniques

1. **Reprise déterministe par rejeu du journal :** lorsqu'un Pod worker tombe au milieu d'un workflow en cinq étapes, Restate relance l'exécution sur un autre worker et réinjecte les résultats des étapes déjà franchies depuis son journal sans rappeler le LLM ni les API externes.
2. **Suspension HITL sans occupation mémoire :** lorsqu'une action requiert un arbitrage humain, le worker crée un `awakeable` Restate, transmet l'identifiant de réveil à l'application chapeau, puis clôture sa requête HTTP. Tant que l'opérateur n'a pas validé l'action, aucun processus Python ne tourne pour cette instance. KEDA ajuste le nombre de workers selon la file d'attente Restate.
3. **Coût d'ingénierie applicative :** ce modèle impose de développer et maintenir soi-même la mécanique interne de l'agent via un SDK. La gestion de la fenêtre de contexte, la compaction de l'historique, l'exécution sécurisée de commandes shell, l'édition de fichiers, l'intégration des serveurs MCP et la boucle d'auto-correction sur erreur de test doivent être codées dans l'application.

Le dossier [`durable-workflow/`](file:///home/mathieu/dev/projects/FRENCHT/durable-agents-k8s/durable-workflow/README.md) décline cette architecture en deux paliers :
* [`v0-minimal/`](file:///home/mathieu/dev/projects/FRENCHT/durable-agents-k8s/durable-workflow/v0-minimal/README.md) : implémentation directe sur le SDK Restate en moins de 150 lignes de code avec une application chapeau FastAPI.
* [`v1-extensible/`](file:///home/mathieu/dev/projects/FRENCHT/durable-agents-k8s/durable-workflow/v1-extensible/README.md) : architecture hexagonale (*Ports & Adapters*) isolant le code métier du moteur d'exécution (Restate ou Dapr), couplée à LiteLLM, OpenTelemetry / Arize Phoenix et Argo CD.

---

## Approche 2 — Kubernetes, stockage répliqué et Harness existant (`k8s-opencode/`)

La seconde approche délègue toute la mécanique interne de l'agent à un *harness* existant (**OpenCode v2**) exécuté dans un Pod Kubernetes dédié, et confie la gestion du cycle de vie ainsi que la mobilité de l'état aux primitives d'infrastructure Kubernetes et **Longhorn**.

### Fonctionnement et compromis techniques

1. **Réutilisation d'un harness complet :** OpenCode v2 fournit nativement la gestion des sessions, les outils de lecture/écriture/édition de code, l'exécution shell, le support MCP, la compaction de contexte, le système de permissions (`allow`, `ask`, `deny`) et un serveur HTTP/SSE headless (`opencode serve`). Il n'y a plus de boucle agentique à développer en Python.
2. **État persistant et mobile entre nœuds K8s :** chaque agent logique possède un `PersistentVolumeClaim` en mode `ReadWriteOncePod` sur une `StorageClass` Longhorn répliquée (`numberOfReplicas: 2`, système de fichiers `ext4`). La base SQLite des sessions (`OPENCODE_DB=/data/opencode.db`), les répertoires XDG et le répertoire de travail (`/data/workspace`) résident sur ce volume.
3. **Hibernation (`replicas: 0`) et migration inter-nœuds :** le `Deployment` de l'agent utilise la stratégie `Recreate` pour garantir un unique écrivain sur la base SQLite. À l'arrêt (`replicas: 0`), l'agent ne consomme ni CPU ni RAM. Au réveil sur un autre nœud K3s (en ~18 s de bout en bout lors d'une migration contrôlée), Longhorn rattache le volume bloc au nouveau Pod et l'agent reprend sa session là où elle s'était arrêtée (`opencode run --continue`).
4. **Rôle de l'application légère au-dessus (« App Chapeau ») :** au lieu d'exécuter la logique de l'agent, l'application de contrôle se limite à trois fonctions :
   * **Instanciation et mise à l'échelle (`0 ↔ 1`)** du Pod agent lors de la réception d'une tâche ou d'une alerte.
   * **Suivi léger** de l'état du Pod et des sessions via l'API HTTP/SSE d'OpenCode.
   * **Arbitrage humain (HITL)** en captant les demandes de permission (`ask`) émises par OpenCode pour les présenter à l'opérateur avant de débloquer la session.

Le laboratoire reproductible (2 VMs K3s sous KVM/libvirt, Longhorn CSI, Pod OpenCode v2 et scripts de test de résilience et de performance) est documenté dans [`k8s-opencode/DOSSIER_REALISATION.md`](file:///home/mathieu/dev/projects/FRENCHT/durable-agents-k8s/k8s-opencode/DOSSIER_REALISATION.md) et [`k8s-opencode/agent-platform-lab/README.md`](file:///home/mathieu/dev/projects/FRENCHT/durable-agents-k8s/k8s-opencode/agent-platform-lab/README.md).

---

## Références Officielles

* **Restate Documentation :** [https://docs.restate.dev/](https://docs.restate.dev/)
* **OpenCode v2 Documentation & CLI :** [https://opencode.ai/docs/](https://opencode.ai/docs/)
* **OpenCode Permissions :** [https://opencode.ai/docs/permissions/](https://opencode.ai/docs/permissions/)
* **Longhorn Distributed Block Storage :** [https://longhorn.io/docs/](https://longhorn.io/docs/)
* **K3s Lightweight Kubernetes :** [https://docs.k3s.io/](https://docs.k3s.io/)
* **KubeOpenCode :** [https://github.com/kubeopencode/kubeopencode](https://github.com/kubeopencode/kubeopencode)

---

## Glossaire

* **CSI (*Container Storage Interface*)** : standard Kubernetes permettant d'attacher et monter des volumes de stockage (ici Longhorn) sur les Pods.
* **Harness** : moteur d'exécution d'agent fournissant la boucle d'interaction avec le LLM, les outils locaux (`bash`, `write`, `edit`), la gestion de session et le contrôle de permissions.
* **HITL (*Human-in-the-Loop*)** : étape d'un workflow où l'agent suspend son action dans l'attente d'une validation ou d'une correction par un opérateur humain.
* **KEDA (*Kubernetes Event-driven Autoscaling*)** : composant Kubernetes ajustant le nombre de réplicas d'un déploiement (de 0 à N) à partir de métriques d'événements ou de files d'attente.
* **MCP (*Model Context Protocol*)** : protocole standardisant la connexion d'outils et de sources de contexte externes à un agent IA.
* **PVC (*PersistentVolumeClaim*)** : requête de volume persistant attachée à un Pod Kubernetes.
* **RWOP (*ReadWriteOncePod*)** : mode d'accès Kubernetes garantissant qu'un volume persistant est monté en lecture/écriture par un seul Pod sur l'ensemble du cluster.
* **WAL (*Write-Ahead Logging*)** : mode de journalisation SQLite offrant l'atomicité des transactions sur un système de fichiers local (`ext4`).
