# Plateforme Agentique Durable sur Kubernetes — Guide Multi-Articles

Ce dépôt contient le code source complet servant de base à une série d'articles techniques (Medium) sur l'architecture des agents d'intelligence artificielle durables sur Kubernetes.

---

## Deux Versions, Deux Objectifs Éditoriaux

```text
eaiap/
├── dossier.md                # Cahier des charges et spécifications techniques initiales
│
├── v0-minimal/               # 🎯 ARTICLE 1 : Le Pattern Socle ("Zero-Waste Agent")
│   │                         # Objectif : Comprendre le concept en < 150 lignes de code
│   │                         # - Direct Restate VirtualObject (0 abstraction)
│   │                         # - Suspension HITL native (0% CPU pendant l'attente humaine)
│   │                         # - Autoscaling KEDA sur les métriques de file d'attente Restate
│   ├── agent_worker/         # Worker pool (agent_service.py en ~45 lignes)
│   ├── app_chapeau/          # Gateway & Dashboard Web inline (main.py en ~90 lignes)
│   ├── k8s/                  # Manifestes Kubernetes bruts (Restate, App, KEDA)
│   ├── docker-compose.yml    # Test local en 1 commande
│   ├── test-demo.sh          # Script de recette automatique
│   └── README.md             # Trame rédigée pour le premier article Medium
│
└── v1-extensible/            # 🚀 ARTICLE 2 : L'Architecture Hexagonale de Production
    │                         # Objectif : Industrialiser et éliminer tout vendor lock-in
    │                         # - Ports & Adapters (ExecutionContext abstrait de Restate/Dapr)
    │                         # - Traçabilité OpenTelemetry & Arize Phoenix
    │                         # - Routeur LLM multi-fournisseurs (LiteLLM / fallbacks)
    │                         # - Catalogue déclaratif d'agents YAML & GitOps complet
    ├── core/                 # Logique métier pure (indépendante du framework)
    ├── runtimes/             # Adaptateurs Restate et squelette Dapr
    ├── services/             # LLM Router & Observabilité Phoenix
    ├── gateway/              # Application Chapeau modulaire
    ├── gitops/               # Manifestes GitOps (Argo CD, Prometheus Stack, KEDA)
    ├── scripts/              # Scripts d'automatisation (k3d, build, démo)
    ├── tests/                # Tests unitaires isolés avec MockExecutionContext
    ├── docker-compose.yml    # Stack complète avec profil observabilité
    └── ARCHITECTURE.md       # Analyse approfondie des choix d'architecture
```

---

## Démarrage Rapide

### Tester la v0 (Article 1)
```bash
cd v0-minimal
docker compose up --build -d
./test-demo.sh
```
Console web disponible sur **http://localhost:8000**.

### Tester la v1 (Article 2)
```bash
cd v1-extensible
docker compose up --build -d
./scripts/demo-recipe.sh
```
Console web disponible sur **http://localhost:8000**, Restate Ingress sur **http://localhost:8080**, et métriques sur **http://localhost:9070**.
