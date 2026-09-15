# Plateforme Agentique Durable sur Kubernetes (v0.1 MVP)

> **Environnement léger d'exécution d'agents IA basé sur l'Exécution Durable (Restate), la mise à l'échelle élastique (K3s + KEDA) et une console d'interaction humaine centralisée (FastAPI HITL).**

Ce dépôt contient l'ensemble du code, des manifestes GitOps, de la configuration et des scripts nécessaires pour déployer et démontrer la plateforme sur un environnement cible, sans aucune installation préalable requise sur cette machine de développement.

---

## 1. Principes Clés et Séparation des Préoccupations (SoC)

Le système est conçu selon une architecture modulaire en couches (Hexagonale / Ports & Adapters) afin de permettre des extensions futures directes :

* **`core/` (Cœur Métier & Domaines) :**
  Logique de l'agent (`GitHubIssueResolverAgent`) totalement agnostique du framework d'exécution. L'agent interagit uniquement avec des interfaces abstraites (`ExecutionContext`, `BaseLLMRouter`, `BaseTracer`).
* **`runtimes/` (Moteurs d'Exécution Durables) :**
  * **Restate** : Adaptateur `RestateContextAdapter` et worker pool `VirtualObject`.
  * **Dapr** : Adaptateur stub `DaprContextAdapter` prêt pour une implémentation alternative.
* **`services/llm/` (Routeur de Modèles) :**
  Abstraction `BaseLLMRouter` avec simulateur déterministe pour la démo hors-ligne et connecteur multi-modèles (LiteLLM / OpenAI / Anthropic / Ollama) configurable via YAML.
* **`services/observability/` (Traçabilité & Évaluation) :**
  Pont OpenTelemetry / Arize Phoenix prêt à l'emploi (`setup_phoenix_tracing`), avec repli sur logs structurés en mode léger.
* **`gateway/` (Application Chapeau) :**
  Service unifié FastAPI avec catalogue dynamique (`catalog/`), proxy de dispatching asynchrone et console web Human-in-the-Loop réactive.

Consulter [ARCHITECTURE.md](file:///home/mathieu/dev/eaiap/ARCHITECTURE.md) pour les détails conceptuels.

---

## 2. Structure du Projet

```text
.
├── dossier.md                    # Cahier des charges et guide d'implémentation
├── ARCHITECTURE.md               # Guide d'architecture et séparation des préoccupations
├── README.md                     # Documentation générale et guide d'exécution
├── docker-compose.yml            # Déploiement local sans Kubernetes
├── .env.example                  # Variables d'environnement de référence
│
├── catalog/                      # Manifestes déclaratifs des agents
│   └── github-issue-resolver.yaml
│
├── core/                         # Logique métier pure et interfaces (Ports)
│   ├── models.py                 # Modèles Pydantic communs (Manifest, HITL, Result)
│   ├── interfaces/
│   │   ├── runtime.py            # Contrat ExecutionContext (run, awakeable, state)
│   │   ├── llm.py                # Contrat BaseLLMRouter
│   │   └── telemetry.py          # Contrat BaseTracer & BaseSpan
│   └── agents/
│       └── github_issue_resolver.py  # Agent modèle résolveur de tickets
│
├── runtimes/                     # Adaptateurs d'exécution (Adapters)
│   ├── restate/                  # Implémentation Restate
│   │   ├── adapter.py            # RestateContextAdapter (ObjectContext -> ExecutionContext)
│   │   ├── worker.py             # Service VirtualObject & Uvicorn
│   │   ├── entrypoint.sh         # Auto-enregistrement auprès de Restate Admin
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── dapr/                     # Extension Dapr future
│       ├── README.md             # Guide d'intégration Dapr Workflows/Actors
│       └── adapter_stub.py       # DaprContextAdapter stub
│
├── services/
│   ├── llm/                      # Routage LLM
│   │   ├── router.py             # MockLLMRouter & ConfigurableLLMRouter
│   │   └── config.yaml           # Stratégie de routage et chaînes de repli
│   └── observability/            # Télémétrie Arize Phoenix
│       ├── phoenix.py            # Initialisation OTel / Phoenix
│       └── tracer.py             # ModularTracer (OTel Span + Logging Span)
│
├── gateway/                      # Application Chapeau (FastAPI & HITL Console)
│   ├── main.py                   # Point d'entrée FastAPI & Dashboard HTML
│   ├── config.py                 # Configuration Pydantic
│   ├── api/
│   │   ├── catalog.py            # GET /api/catalog
│   │   ├── agents.py             # POST /api/agents/launch
│   │   └── hitl.py               # POST /api/hitl/notify, /pending, /resolve
│   ├── services/
│   │   ├── catalog_service.py    # Découverte YAML
│   │   ├── hitl_store.py         # Registre d'état des suspensions (InMemory / Redis)
│   │   └── orchestrator_client.py# Client de commande Restate / Dapr
│   ├── templates/
│   │   └── index.html            # Interface web dynamique & réactive
│   ├── Dockerfile
│   └── requirements.txt
│
├── gitops/                       # Manifestes Kubernetes (Argo CD)
│   ├── namespace.yaml            # Namespace 'agent-system'
│   ├── restate.yaml              # Serveur Restate (Ingress + Admin)
│   ├── restate-monitor.yaml      # ServiceMonitor Prometheus
│   ├── app-chapeau.yaml          # Passerelle et console HITL
│   ├── agent-workers.yaml        # Worker Pool Deployment + KEDA ScaledObject
│   ├── observability-phoenix.yaml# Déploiement Arize Phoenix
│   └── argocd-root-app.yaml      # Application racine Argo CD
│
├── scripts/                      # Scripts d'automatisation
│   ├── 01-setup-k3d.sh           # Création du cluster k3d local
│   ├── 02-install-prereqs.sh     # Installation Argo CD, Prometheus, KEDA
│   ├── 03-build-images.sh        # Build Docker & import dans k3d
│   ├── 04-deploy-gitops.sh       # Déploiement des manifestes
│   └── demo-recipe.sh            # Cahier de recette automatisé (Section 6)
│
└── tests/                        # Tests unitaires indépendants de l'infrastructure
    ├── test_agent_core.py        # Test unitaire de l'agent via MockExecutionContext
    └── test_gateway.py           # Test unitaire du catalogue et du store HITL
```

---

## 3. Guide de Déploiement sur l'Environnement Cible

Deux modes de déploiement sont préparés :

### Mode A : Déploiement Kubernetes Complet (k3d + Helm + GitOps)

Ce mode correspond fidèlement au dossier d'architecture technique. Exécuter dans l'ordre les scripts fournis dans `scripts/` :

```bash
# 1. Création du cluster k3d avec exposition des ports
./scripts/01-setup-k3d.sh

# 2. Installation d'Argo CD, Prometheus Stack et KEDA
./scripts/02-install-prereqs.sh

# 3. Construction des images Docker et import dans k3d
./scripts/03-build-images.sh

# 4. Déploiement des composants GitOps et vérification du démarrage
./scripts/04-deploy-gitops.sh
```

Une fois déployé :
* **Console Web & Dashboard HITL :** [http://localhost:8000](http://localhost:8000)
* **Restate Server Ingress :** [http://localhost:8080](http://localhost:8080)
* **Restate Admin / Métriques Prometheus :** [http://localhost:9070](http://localhost:9070)

---

### Mode B : Déploiement Léger Docker Compose (Sans Kubernetes)

Pour une démonstration rapide ou un environnement sans cluster Kubernetes :

```bash
# Lancement de la stack de base (Restate + App Chapeau + Worker)
docker compose up --build -d

# Pour inclure également l'interface Arize Phoenix (port 6006)
ENABLE_PHOENIX=true docker compose --profile observability up --build -d
```

---

## 4. Déroulé de la Démonstration (Cahier de Recette)

Vous pouvez exécuter le scénario complet via le script automatique :

```bash
./scripts/demo-recipe.sh
```

Ou réaliser la démonstration interactive étape par étape :

### Étape 1 : Vérification des Composants
Ouvrir [http://localhost:8000](http://localhost:8000). Le statut en haut à droite indique *« Restate Ingress Connecté »*.
Côté cluster :
```bash
kubectl get pods -n agent-system
```
*Attendu :* 1 pod `restate`, 1 pod `app-chapeau`, et 1 pod `agent-workers` en état `Running`.

### Étape 2 : Lancement Simultané de Deux Instances
Dans l'interface web (ou via API REST) :
1. Instance 1 : ID `instance-alpha`, dépôt `mon-orga/backend`, issue `101` -> Cliquer sur **Démarrer le Cycle**.
2. Instance 2 : ID `instance-beta`, dépôt `mon-orga/frontend`, issue `202` -> Cliquer sur **Démarrer le Cycle**.

### Étape 3 : Constat de l'Hibernation (0% CPU)
Dans la section **Arbitrage Humain (HITL)** de la console, deux cartes jaunes apparaissent immédiatement avec l'analyse proposée par l'agent.
* Mesure d'empreinte CPU :
  ```bash
  kubectl top pods -n agent-system
  ```
* *Attendu :* La consommation du pod worker retombe au niveau plancher (~1-2m CPU). Aucun thread n'est bloqué. Les agents sont suspendus au niveau de la promesse `awakeable` dans Restate.

### Étape 4 : Preuve de Tolérance aux Pannes (Crash Recovery)
Tuer le worker pendant que les deux agents sont en hibernation :
```bash
kubectl delete pod -l app=agent-workers -n agent-system
```
Kubernetes redémarre un nouveau pod worker en quelques secondes. Le nouveau pod s'auto-enregistre auprès de Restate. Les deux instances restent fidèlement en attente sans perte de contexte ni redémarrage de l'étape d'analyse !

### Étape 5 : Réveil Sélectif et Résolution
1. Sur `instance-alpha` : saisir *« Approuvé pour merge rapide »* et cliquer sur **✓ Approuver & Créer PR**.
   * Seule l'instance Alpha se réveille, valide la création de PR et disparaît de la file d'attente.
   * `instance-beta` reste en hibernation.
2. Sur `instance-beta` : cliquer sur **✗ Rejeter**.
   * L'instance Beta termine son cycle avec statut `aborted`.

---

## 5. Extensions Futures

### 1. Observabilité Avancée avec Arize Phoenix
1. Déployer Phoenix dans le cluster :
   ```bash
   kubectl apply -f gitops/observability-phoenix.yaml
   ```
2. Définir les variables d'environnement sur le worker (`PHOENIX_COLLECTOR_ENDPOINT=http://phoenix.agent-system.svc.cluster.local:6006/v1/traces` et `ENABLE_PHOENIX=true`).
3. Accéder à l'interface Phoenix sur `http://localhost:6006` pour inspecter la trace complète de chaque étape, la consommation de tokens et les évaluations d'agents.

### 2. Routage LLM Multi-Fournisseurs
Modifier [services/llm/config.yaml](file:///home/mathieu/dev/eaiap/services/llm/config.yaml) pour router des tâches spécifiques vers des modèles spécialisés (ex: Claude 3.5 Sonnet pour l'analyse de code, GPT-4o-mini pour la synthèse, ou un modèle open source local Ollama/vLLM).

### 3. Support de Dapr
Le connecteur [runtimes/dapr/adapter_stub.py](file:///home/mathieu/dev/eaiap/runtimes/dapr/adapter_stub.py) fournit le squelette de conversion vers Dapr Workflows et Virtual Actors sans impacter le code métier situé dans `core/agents/`.
