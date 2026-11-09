# Labs Durable Execution — Restate & Architecture Hexagonale

Ce répertoire regroupe les spécifications et les deux implémentations de référence basées sur un moteur de *Durable Execution* (**Restate**), où la logique de l'agent est développée en Python à l'aide d'un SDK.

---

## Structure du Répertoire

```text
durable-workflow/
├── dossier.md                # Cahier des charges et spécifications techniques initiales
│
├── v0-minimal/               # Palier 1 : Pattern socle ("Zero-Waste Agent")
│   │                         # Objectif : Implémenter le cycle complet en < 150 lignes de code
│   │                         # - Couplage direct Restate VirtualObject (0 abstraction)
│   │                         # - Suspension HITL native (0% CPU pendant l'attente humaine)
│   │                         # - Autoscaling KEDA sur les métriques de file d'attente Restate
│   ├── agent_worker/         # Worker pool (agent_service.py en ~45 lignes)
│   ├── app_chapeau/          # Gateway & Dashboard Web inline (main.py en ~90 lignes)
│   ├── k8s/                  # Manifestes Kubernetes bruts (Restate, App, KEDA)
│   ├── docker-compose.yml    # Test local en 1 commande
│   ├── test-demo.sh          # Script de recette automatique
│   └── README.md             # Documentation du lab v0
│
└── v1-extensible/            # Palier 2 : Architecture Hexagonale (Ports & Adapters)
    │                         # Objectif : Découpler le code métier du moteur d'exécution
    │                         # - Ports & Adapters (ExecutionContext abstrait de Restate/Dapr)
    │                         # - Traçabilité OpenTelemetry & Arize Phoenix
    │                         # - Routeur LLM multi-fournisseurs (LiteLLM / fallbacks)
    │                         # - Catalogue déclaratif d'agents YAML & GitOps (Argo CD)
    ├── core/                 # Logique métier pure (indépendante du framework)
    ├── runtimes/             # Adaptateurs Restate et squelette Dapr
    ├── services/             # LLM Router & Observabilité Phoenix
    ├── gateway/              # Application Chapeau modulaire
    ├── gitops/               # Manifestes GitOps (Argo CD, Prometheus Stack, KEDA)
    ├── scripts/              # Scripts d'automatisation (k3d, build, démo)
    ├── tests/                # Tests unitaires isolés avec MockExecutionContext
    ├── docker-compose.yml    # Stack complète avec profil observabilité
    └── ARCHITECTURE.md       # Analyse détaillée des choix d'architecture
```

---

## Démarrage Rapide

### Tester la v0 (`v0-minimal`)

```bash
cd v0-minimal
docker compose up --build -d
./test-demo.sh
```

La console web est exposée sur `http://localhost:8000`.

### Tester la v1 (`v1-extensible`)

```bash
cd v1-extensible
docker compose up --build -d
./scripts/demo-recipe.sh
```

La console web est exposée sur `http://localhost:8000`, l'Ingress Restate sur `http://localhost:8080` et les métriques sur `http://localhost:9070`.
