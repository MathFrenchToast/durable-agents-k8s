# Dapr Runtime Integration (Extensibility Guide)

Ce dossier prépare l'extension de la plateforme vers **Dapr (Distributed Application Runtime)** comme moteur d'exécution durable alternatif ou complémentaire à Restate.

## Architecture de Connexion

Grâce au découplage strict réalisé dans `core/interfaces/runtime.py` (`ExecutionContext`), le code des agents métier (`core/agents/`) ne dépend ni de Restate ni de Dapr.

```
┌────────────────────────────────────────┐
│     Agent Metier (ex: GitHub Resolver) │
└───────────────────┬────────────────────┘
                    │ Appelle l'interface ExecutionContext
                    ▼
┌────────────────────────────────────────────────────────┐
│               ExecutionContext (Interface)              │
│  - run(step_name, action)                              │
│  - awakeable() -> (token, promise)                     │
│  - get_state(key) / set_state(key, val)                │
└───────────┬────────────────────────────────┬───────────┘
            │                                │
            ▼ (Actuel)                       ▼ (Extension)
┌───────────────────────┐        ┌───────────────────────┐
│ RestateContextAdapter │        │   DaprContextAdapter  │
│ (Virtual Objects API) │        │ (Dapr Workflows/Actor)│
└───────────────────────┘        └───────────────────────┘
```

## Mapping des Primitives Dapr

1. **Durable Steps (`ctx.run`) :**
   - En Dapr Workflows : `yield ctx.call_activity(step_name, input_data)`
   - Chaque activité bénéficie de retries automatiques et de mémorisation dans le state store Dapr.

2. **Awakeables / HITL Suspension (`ctx.awakeable`) :**
   - En Dapr Workflows : `yield ctx.wait_for_external_event("hitl_approval")`
   - Le token correspond à `instance_id` ou un event name unique.
   - La résolution se fait via l'API Dapr : `POST /v1.0-alpha1/workflows/<workflow_name>/<instance_id>/raise-event/hitl_approval`.

3. **État Persistant (`get_state` / `set_state`) :**
   - Dapr State Management API (`/v1.0/state/<store-name>`).
