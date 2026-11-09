# Agents IA Durables sur Kubernetes

Ce dépôt compare deux approches pour héberger des agents IA sur Kubernetes : exécution de tâches longues, reprise après panne de Pod ou de nœud, mise en pause sur validation humaine (*Human-in-the-Loop*) avec retour à zéro ressource (`scale-to-zero`), et application de contrôle légère.

## Structure du dépôt

```text
.
├── durable-workflow/         # Approche 1 : Moteur d'exécution durable (Restate) + Agent codé via SDK
│   ├── README.md
│   ├── dossier.md
│   ├── v0-minimal/           # Implémentation minimale (Restate + KEDA)
│   └── v1-extensible/        # Architecture hexagonale (Restate/Dapr, LiteLLM, Phoenix, Argo CD)
│
└── k8s-opencode/             # Approche 2 : Primitives Kubernetes + Harness existant (OpenCode v2)
    ├── article-k8s-eaia-plateform.md
    ├── DOSSIER_REALISATION.md
    └── agent-platform-lab/   # Lab K3s multi-nœuds (KVM/libvirt), Longhorn, Pod OpenCode et Control Plane
```

## Comparatif des deux approches

| Critère | Approche 1 : Durable Execution + SDK ([`durable-workflow/`](./durable-workflow/)) | Approche 2 : Kubernetes + Harness ([`k8s-opencode/`](./k8s-opencode/)) |
| :--- | :--- | :--- |
| **Principe** | Journalisation d'événements (*Event Sourcing*) via [Restate](https://docs.restate.dev/) | Harnais d'agent existant ([OpenCode v2](https://opencode.ai/docs/)) dans un Pod dédié |
| **Code à produire** | Boucle agentique, outils et routage LLM codés en Python | Configuration déclarative (`opencode.json`, skills, MCP, permissions) + Control Plane léger |
| **Persistance** | Journal distribué Restate (état clé/valeur + historique RPC) | Base SQLite (`/data/opencode.db`) + workspace sur volume répliqué [Longhorn](https://longhorn.io/docs/) (`ReadWriteOncePod`) |
| **Reprise sur panne** | À l'étape près (`ctx.run`) par rejeu du journal | Au tour de conversation (`opencode run --continue`), fichiers conservés sur le PVC |
| **Attente humaine (HITL)** | Promesse durable (`awakeable`), worker libéré | Interception de l'événement `permission: ask`, snapshot et mise à l'échelle `replicas: 0` |
| **Empreinte au repos** | 0 Pod worker via [KEDA](https://keda.sh/) (moteur Restate actif) | 0 Pod agent (`replicas: 0`, PVC conservé) |

## Documentation et laboratoires

### 1. Approche Workflow Durable + SDK (`durable-workflow/`)
- Vue d'ensemble : [`durable-workflow/README.md`](./durable-workflow/README.md)
- Palier minimal : [`durable-workflow/v0-minimal/README.md`](./durable-workflow/v0-minimal/README.md) et [article associé](./durable-workflow/v0-minimal/ARTICLE_MEDIUM.md)
- Palier extensible : [`durable-workflow/v1-extensible/README.md`](./durable-workflow/v1-extensible/README.md) et [article associé](./durable-workflow/v1-extensible/ARTICLE_MEDIUM.md)

### 2. Approche Primitives Kubernetes + Harness (`k8s-opencode/`)
- Article : [`k8s-opencode/article-k8s-eaia-plateform.md`](./k8s-opencode/article-k8s-eaia-plateform.md)
- Dossier d'étude : [`k8s-opencode/DOSSIER_REALISATION.md`](./k8s-opencode/DOSSIER_REALISATION.md)
- Laboratoire reproductible et Control Plane : [`k8s-opencode/agent-platform-lab/README.md`](./k8s-opencode/agent-platform-lab/README.md) et [`docs/ADR.md`](./k8s-opencode/agent-platform-lab/docs/ADR.md)
