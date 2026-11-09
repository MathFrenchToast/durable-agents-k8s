# Architecture d'une Plateforme Agentique d'Entreprise : Découplage Hexagonal, Observabilité Phoenix et Routage Multi-LLM

## *Comment concevoir un runtime d'agents Cloud Native : éliminer le lock-in orchestrateur (Restate/Dapr), auditer les chaînes de pensée avec OpenTelemetry et maîtriser les coûts d'inférence.*

---

![Production Architecture](https://raw.githubusercontent.com/open-telemetry/opentelemetry.io/main/iconography/3D/PNG/02_OpenTelemetry_3D_Hero.png)
*(Observabilité distribuée et architecture découplée pour les flottes d'agents d'entreprise)*

---

### 1. De l'Expérimentation à la Production : Les 3 Défis d'Ingénierie

Dans notre [premier article](file:///home/mathieu/dev/eaiap/v0-minimal/ARTICLE_MEDIUM.md), nous avons validé le pattern fondamental de l'**Exécution Durable (*Durable Execution*)** sur Kubernetes : transformer des agents asynchrones à longue durée de vie en objets virtuels capables d'hiberner à 0% de CPU pendant les attentes de validation humaine (*Human-in-the-Loop*), tout en garantissant une reprise exacte après crash sans ré-exécuter les appels LLM passés.

Cependant, faire tourner un POC en direct sur le SDK d'un framework ne suffit pas pour un déploiement d'entreprise à grande échelle. Trois écueils majeurs apparaissent immédiatement :

1. **Le couplage fort au moteur d'orchestration (*Framework Lock-in*) :** Importer directement `import restate` au sein de la logique métier de l'agent lie définitivement votre propriété intellectuelle à un produit tiers. Si votre équipe d'infrastructure standardise sur **Dapr (Distributed Application Runtime)**, Temporal ou un orchestrateur interne, l'ensemble des agents doit être réécrit.
2. **L'opacité opérationnelle des systèmes stochastiques :** Superviser des microservices classiques repose sur des métriques déterministes (code HTTP 200/500, CPU, RAM). Les agents d'IA, eux, échouent silencieusement : hallucinations, dérives de raisonnement, boucles d'outils infinies, explosion du nombre de tokens. Sans **tracing sémantique distribué**, le débogage en production est impossible.
3. **Le coût et la disponibilité des modèles de langage :** Coder en dur l'appel à une API tierce unique (OpenAI ou Anthropic) expose à des pannes réseaux, des blocages de quotas (*rate limits*) et des coûts disproportionnés. Il est impératif d'aiguiller chaque tâche vers le modèle optimal (ex: analyse de code pointue vs résumé de texte léger) avec bascule automatique sur un modèle de secours.

Pour adresser ces exigences, nous avons conçu la version **v1-extensible** de notre plateforme, bâtie sur l'**Architecture Hexagonale (Ports & Adapters)**, une instrumentation **Arize Phoenix / OpenTelemetry**, un **Routeur de LLM déclaratif**, et un déploiement GitOps souverain avec **Argo CD**.

---

## 2. Le Découplage par l'Architecture Hexagonale (Ports & Adapters)

Le principe directeur est strict : **l'agent d'IA est un composant de domaine pur. Il ignore totalement l'existence de Kubernetes, de Restate, de Dapr ou de FastAPI.**

```text
                           ┌────────────────────────┐
                           │   Console Web (HITL)   │
                           └───────────┬────────────┘
                                       │ HTTP / REST
                                       ▼
                       ┌────────────────────────────────┐
                       │    APPLICATION CHAPEAU         │
                       │    (FastAPI / Gateway)         │
                       └───────┬────────────────┬───────┘
                               │                │
             (1) Dispatch Run  │                │ (2) Token Awakeable
                               ▼                ▼
      ┌─────────────────────────────────────────────────────────────┐
      │                MOTEUR D'EXÉCUTION DURABLE                   │
      │      [Restate (implémenté)  |  Dapr (stub prêt)]             │
      └──────────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼
        ┌─────────────────────────────────────────────────────────┐
        │                WORKER POOL AGENTS                       │
        │                                                         │
        │   ┌─────────────────────────────────────────────────┐   │
        │   │  Adaptateur Runtime (RestateContextAdapter)     │   │
        │   └────────────────────────┬────────────────────────┘   │
        │                            │ Implémente ExecutionContext │
        │                            ▼                            │
        │   ┌─────────────────────────────────────────────────┐   │
        │   │     AGENT MÉTIER PUR (Domain Core)              │   │
        │   │     - github_issue_resolver.py                  │   │
        │   │     - 0 import Restate / 0 import Dapr          │   │
        │   └────────────┬───────────────────────┬────────────┘   │
        │                │                       │                │
        │                ▼                       ▼                │
        │   ┌───────────────────────┐ ┌───────────────────────┐   │
        │   │   LLM Router Adapter  │ │ Observabilité Phoenix │   │
        │   │   (LiteLLM / Mock)    │ │ (OpenTelemetry OTLP)  │   │
        │   └───────────────────────┘ └───────────────────────┘   │
        └─────────────────────────────────────────────────────────┘
```

### Le Port : L'interface `ExecutionContext` (`core/interfaces/runtime.py`)

Nous isolons les besoins d'un agent durable derrière une interface Python abstraite :

```python
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Tuple, TypeVar

T = TypeVar("T")

class ExecutionContext(ABC):
    @abstractmethod
    def key(self) -> str:
        """Identifiant unique de partitionnement (instance ID)."""
        pass

    @abstractmethod
    async def run(self, step_name: str, action: Callable[..., Any]) -> T:
        """Étape checkpointée idempotente : rejouée sans calcul si déjà journalisée."""
        pass

    @abstractmethod
    def awakeable(self) -> Tuple[str, Awaitable[Any]]:
        """Génère un token de suspension externe et une promesse asynchrone (HITL)."""
        pass

    @abstractmethod
    async def get_state(self, key: str) -> Any: ...
    @abstractmethod
    async def set_state(self, key: str, value: Any) -> None: ...
```

### Le Domaine Métier : `GitHubIssueResolverAgent` (`core/agents/github_issue_resolver.py`)

L'agent consomme uniquement des contrats : son `ExecutionContext`, son `BaseLLMRouter` et son `BaseTracer`.

```python
class GitHubIssueResolverAgent:
    def __init__(
        self,
        llm_router: Optional[BaseLLMRouter] = None,
        tracer: Optional[BaseTracer] = None,
        notify_gateway_fn: Optional[Callable[[str, str, str], Any]] = None,
    ):
        self.llm_router = llm_router
        self.tracer = tracer or NoOpTracer()
        self.notify_gateway_fn = notify_gateway_fn

    async def resolve(self, ctx: ExecutionContext, payload: dict) -> dict:
        instance_id = ctx.key()
        repo = payload.get("target_repo")
        issue_id = str(payload.get("issue_id"))

        async with self.tracer.start_span("resolve_workflow", attributes={"repo": repo}):
            # 1. Étape d'analyse durable (idempotente)
            async def run_analysis():
                return await self.execute_issue_analysis(repo, issue_id)

            solution = await ctx.run("analyze", run_analysis)

            # 2. Point de suspension Human-in-the-Loop
            token, promise = ctx.awakeable()
            
            if self.notify_gateway_fn:
                await ctx.run("notify_ui", lambda: self.notify_gateway_fn(
                    instance_id, f"Patch proposé : {solution}", token
                ))

            # HIBERNATION COMPLETE
            decision = await promise

            # 3. Action finale à effet de bord (Création de PR)
            if decision.get("approved") is True:
                pr = await ctx.run("create_pr", lambda: self._create_pr(repo, issue_id))
                return {"status": "completed", "action": "PR_CREATED", "details": pr}

            return {"status": "aborted", "action": "REJECTED"}
```

### L'Adaptabilité : Restate vs Dapr

* **Adaptateur Restate (`runtimes/restate/adapter.py`) :** Enveloppe le `ObjectContext` de Restate. Il garantit que les lambdas ou fonctions asynchrones passées à `run()` sont correctement inspectées et sérialisées dans le journal Restate.
* **Adaptateur Dapr (`runtimes/dapr/adapter_stub.py`) :** Implémente le même contrat sur l'API **Dapr Workflows** (`call_activity` pour les étapes durables, `wait_for_external_event` pour les awakeables). Basculer d'un orchestrateur à l'autre ne requiert aucune modification du code de l'agent.

### L'Impact sur la Vitesse de Développement : Des Tests Unitaires en 8 Millisecondes

Dans [`tests/test_agent_core.py`](file:///home/mathieu/dev/eaiap/v1-extensible/tests/test_agent_core.py), un `MockExecutionContext` en mémoire simule la journalisation d'étapes et la résolution d'awakeables. L'ensemble de la logique métier de l'agent (analyse, persistance, arbitrage, création de PR) est testé unitairement sans instancier le moindre conteneur ni cluster Kubernetes.

---

## 3. Observabilité Sémantique avec Arize Phoenix et OpenTelemetry

Surveiller des agents nécessite de capturer la structure hiérarchique de leur exécution : quel prompt a mené à quel appel d'outil, avec quelle latence et quelle consommation de tokens ?

Nous avons standardisé la télémétrie sur **OpenTelemetry (OTel)** et connecté la plateforme à **Arize Phoenix**, outil d'évaluation et de traçabilité spécialisé pour les LLMs :

```text
┌────────────────────────────────────────────────────────┐
│                   ARIZE PHOENIX (UI :6006)             │
│   - Traces de raisonnement (Spans hiérarchiques)       │
│   - Consommation de tokens (Prompt vs Completion)      │
│   - Évaluation d'hallucinations & latences d'outils    │
└───────────────────────────▲────────────────────────────┘
                            │ OTLP HTTP (Collector :6006/v1/traces)
┌───────────────────────────┴────────────────────────────┐
│      COUCHE DE TÉLÉMÉTRIE MODULAIRE (services/observability) │
│   - services/observability/phoenix.py                  │
│   - services/observability/tracer.py (ModularTracer)   │
└───────────────────────────▲────────────────────────────┘
                            │
┌───────────────────────────┴────────────────────────────┐
│                    AGENT WORKERS                       │
│    async with tracer.start_span("code_analysis"): ...  │
└────────────────────────────────────────────────────────┘
```

### Le Pattern du Tracer Modulaire (`services/observability/`)

1. **Initialisation découplée (`phoenix.py`) :** Le composant vérifie la présence de `PHOENIX_COLLECTOR_ENDPOINT`. S'il est configuré, un `TracerProvider` OTel avec un exportateur OTLP HTTP par lot (*BatchSpanProcessor*) est instancié.
2. **Mode Dégradé Transparent :** Si Phoenix n'est pas déployé (environnement de test ou local minimal), le traceur retombe gracieusement sur un **Logging Structuré**, sans exception ni dépendance bloquante.
3. **Déploiement K8s Déclaratif :** Un manifeste unique ([`gitops/observability-phoenix.yaml`](file:///home/mathieu/dev/eaiap/v1-extensible/gitops/observability-phoenix.yaml)) permet de déployer l'interface et le collecteur Phoenix au sein du namespace `agent-system` en une commande.

---

## 4. Routage Dynamique de Modèles (LLM Router)

Pour éviter le couplage dur à un modèle ou à un fournisseur particulier, nous introduisons un composant de routage déclaratif dans [`services/llm/`](file:///home/mathieu/dev/eaiap/v1-extensible/services/llm/).

### Déclaration de la Politique de Routage (`services/llm/config.yaml`) :

```yaml
version: "1.0"
default_model: "gpt-4o-mini"

# Allocation spécialisée par nature de tâche
routing_rules:
  code_analysis: "claude-3-5-sonnet-20241022"  # Précision de code maximale
  summarization: "gpt-4o-mini"                 # Économie de tokens
  decision: "gpt-4o-mini"

# Chaîne de repli automatique en cas d'erreur API ou de dépassement de quota
fallback_chain:
  - "claude-3-5-sonnet-20241022"
  - "gpt-4o-mini"
  - "mock"                                     # Repli déterministe pour tests/offline
```

### Le Routeur Pluggable (`services/llm/router.py`) :
* Implémente l'interface `BaseLLMRouter` (`route_and_generate`).
* Aiguille automatiquement la requête selon le `task_type` spécifié par l'agent.
* En cas d'erreur HTTP 429 (*Rate Limit Exceeded*) ou d'indisponibilité d'un provider, le routeur parcourt la `fallback_chain` pour assurer la continuité de service.
* Intègre un mode simulateur déterministe (`MockLLMRouter`) calculant tokens et latences réalistes sans nécessiter de clés d'API externes en phase de démo ou de tests.

---

## 5. Passerelle Déclarative et Exploitation GitOps

L'Application Chapeau ([`gateway/`](file:///home/mathieu/dev/eaiap/v1-extensible/gateway/)) a été découplée pour fonctionner comme un point d'entrée d'entreprise :

1. **Catalogue Déclaratif (`catalog/` & `CatalogService`) :**
   Les agents disponibles ne sont pas fixés dans le code de la passerelle. Chaque agent dispose d'un manifeste YAML définissant ses paramètres, ses types et ses contraintes :
   ```yaml
   id: "github-issue-resolver"
   name: "Résolveur de Tickets GitHub"
   service_name: "GitHubIssueResolver"
   handler: "resolve"
   parameters:
     - name: "target_repo"
       type: "string"
       required: true
     - name: "issue_id"
       type: "string"
       required: true
   ```
   L'interface web génère dynamiquement ses formulaires de lancement à partir de ce catalogue, permettant d'ajouter de nouveaux agents sans redéployer la passerelle.
2. **Abstraction du Registre HITL (`BaseHITLStore`) :**
   La mémoire des suspensions est isolée derrière une interface, permettant de basculer d'une table mémoire locale (dev) à un cache distribué Redis ou PostgreSQL (production multi-répliques).
3. **Déploiement GitOps avec Argo CD (`gitops/argocd-root-app.yaml`) :**
   Tous les composants de la plateforme (Restate, ServiceMonitor Prometheus, Workers scalés par KEDA, Passerelle, Observabilité Phoenix) sont déclarés sous forme de manifestes GitOps synchronisés en continu par Argo CD.

---

## Synthèse Comparative : Prototype v0 vs Plateforme Industrielle v1

| Critère d'Ingénierie | Implémentation v0 (POC Direct) | Implémentation v1 (Production Hexagonale) |
| :--- | :--- | :--- |
| **Couplage Orchestrateur** | Direct (SDK Restate importé dans l'agent) | **Totalement découplé via `ExecutionContext`** |
| **Interchangeabilité Moteur**| Restate exclusivement | **Restate & Dapr interchangeables sans retouche de l'agent** |
| **Observabilité** | Logs texte sur console stdout | **Spans distribués OpenTelemetry + Arize Phoenix** |
| **Gestion des Modèles LLM**| Réponse statique simulée | **Routeur déclaratif YAML avec fallbacks multi-fournisseurs** |
| **Catalogue d'Agents** | Formulaire web codé en dur | **Découverte dynamique via manifestes YAML (`catalog/`)** |
| **Stratégie de Test** | Tests d'intégration sur conteneurs | **Tests unitaires purs en 8 ms via `MockExecutionContext`** |
| **Déploiement K8s** | Manifestes `kubectl apply` basiques | **GitOps déclaratif Argo CD avec KEDA ScaledObject** |

---

## Conclusion : Bâtir des Systèmes Agentiques Durables

La transition du chatbot conversationnel à la plateforme d'agents autonomes exige une refonte des patterns d'architecture Cloud Native :

* **L'Exécution Durable** apporte la sobriété en ressources (0% CPU en attente) et la tolérance absolue aux pannes d'infrastructure.
* **L'Architecture Hexagonale** protège vos investissements logiciels contre le lock-in technologique et autorise des tests unitaires ultrarapides.
* **L'Observabilité OpenTelemetry / Phoenix** et le **Routage de LLM** apportent l'auditabilité et le contrôle des coûts indispensables en entreprise.

---

*L'ensemble du code source de cette plateforme industrielle (core, runtimes, services, gitops, tests unitaires et recettes d'intégration) est disponible dans le répertoire `v1-extensible/` de ce dépôt.*
