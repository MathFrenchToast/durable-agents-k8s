# Architecture et Séparation des Préoccupations (SoC)

Ce document détaille les principes architecturaux mis en œuvre dans ce projet pour garantir un découplage strict entre la logique métier des agents, le moteur d'exécution durable, le routage des LLM et la couche d'observabilité.

---

## 1. Vue d'Ensemble : Architecture Hexagonale (Ports & Adapters)

L'architecture isole complètement la logique agentique de toute dépendance technologique sous-jacente :

```
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
       │   │     - Aucun import Restate ou Dapr              │   │
       │   └────────────┬───────────────────────┬────────────┘   │
       │                │                       │                │
       │                ▼                       ▼                │
       │   ┌───────────────────────┐ ┌───────────────────────┐   │
       │   │   LLM Router Adapter  │ │ Observabilité Phoenix │   │
       │   │   (LiteLLM / Mock)    │ │ (OpenTelemetry OTLP)  │   │
       │   └───────────────────────┘ └───────────────────────┘   │
       └─────────────────────────────────────────────────────────┘
```

---

## 2. Découplage des Runtimes Durables (`core/interfaces/runtime.py`)

### Le Contrat `ExecutionContext`
L'agent manipule uniquement l'interface abstraite :

```python
class ExecutionContext(ABC):
    @abstractmethod
    def key(self) -> str: ...

    @abstractmethod
    async def run(self, step_name: str, action: Callable[..., Awaitable[T]]) -> T: ...

    @abstractmethod
    def awakeable(self) -> Tuple[str, Awaitable[Any]]: ...
```

### Bénéfices :
1. **Remplacement sans refactorisation :**
   - Aujourd'hui : [RestateContextAdapter](file:///home/mathieu/dev/eaiap/runtimes/restate/adapter.py) traduit `run()` et `awakeable()` vers le SDK Restate.
   - Demain : [DaprContextAdapter](file:///home/mathieu/dev/eaiap/runtimes/dapr/adapter_stub.py) traduit ces appels vers Dapr Workflows (`call_activity`, `wait_for_external_event`) sans toucher à une seule ligne de l'agent.
2. **Testabilité unitaire à 100% :**
   - Comme démontré dans [tests/test_agent_core.py](file:///home/mathieu/dev/eaiap/tests/test_agent_core.py), un agent peut être testé avec un `MockExecutionContext` en mémoire en quelques millisecondes, sans démarrer de serveur Restate ni de cluster Kubernetes.

---

## 3. Extension Observabilité : Arize Phoenix (`services/observability/`)

La traçabilité des exécutions d'agents (prompts, latences, tokens, suspensions HITL) s'appuie sur le standard **OpenTelemetry** :

- **Abstraction :** [BaseTracer](file:///home/mathieu/dev/eaiap/core/interfaces/telemetry.py) et [BaseSpan](file:///home/mathieu/dev/eaiap/core/interfaces/telemetry.py).
- **Adaptateur modulaire :** [ModularTracer](file:///home/mathieu/dev/eaiap/services/observability/tracer.py) émet des logs structurés si OTel n'est pas présent, ou transmet à l'exporteur OTLP si configuré.
- **Intégration Phoenix :** [services/observability/phoenix.py](file:///home/mathieu/dev/eaiap/services/observability/phoenix.py) se connecte directement à l'instance Arize Phoenix via `PHOENIX_COLLECTOR_ENDPOINT` (ex: `http://phoenix.agent-system.svc.cluster.local:6006/v1/traces`).
- **Déploiement K8s prêt à l'emploi :** [gitops/observability-phoenix.yaml](file:///home/mathieu/dev/eaiap/gitops/observability-phoenix.yaml) déploie le serveur Phoenix en un clic.

---

## 4. Extension Router LLM (`services/llm/`)

Le modèle d'inférence est abstrait via [BaseLLMRouter](file:///home/mathieu/dev/eaiap/core/interfaces/llm.py) :

- **Mode Démo Léger (MVP actuel) :** [MockLLMRouter](file:///home/mathieu/dev/eaiap/services/llm/router.py) génère des analyses déterministes et calcule les métriques de tokens sans clé d'API.
- **Routage Multi-fournisseurs :** [ConfigurableLLMRouter](file:///home/mathieu/dev/eaiap/services/llm/router.py) charge [services/llm/config.yaml](file:///home/mathieu/dev/eaiap/services/llm/config.yaml) pour distribuer les requêtes selon le `task_type` (ex: `code_analysis` -> `claude-3-5-sonnet`, `summarization` -> `gpt-4o-mini`, ou modèles locaux Ollama).
- **Fallback Chain :** En cas d'indisponibilité d'un fournisseur ou de dépassement de quota, le routeur bascule automatiquement vers le modèle suivant.

---

## 5. Découplage de la Passerelle ("Application Chapeau")

L'application chapeau ([gateway/](file:///home/mathieu/dev/eaiap/gateway/)) sépare :

1. **Le Catalogue déclaratif ([gateway/services/catalog_service.py](file:///home/mathieu/dev/eaiap/gateway/services/catalog_service.py)) :**
   Lit les fichiers YAML dans `catalog/`. L'interface web et l'API de lancement s'adaptent dynamiquement aux paramètres déclarés.
2. **Le Plan de Contrôle ([gateway/services/orchestrator_client.py](file:///home/mathieu/dev/eaiap/gateway/services/orchestrator_client.py)) :**
   Interface `BaseOrchestratorClient`. Permet à l'App Chapeau de piloter Restate ou Dapr indifféremment.
3. **Le Registre HITL ([gateway/services/hitl_store.py](file:///home/mathieu/dev/eaiap/gateway/services/hitl_store.py)) :**
   Interface `BaseHITLStore` : implémentation en mémoire pour le MVP, remplaçable par Redis pour les déploiements haute disponibilité multi-répliques.
