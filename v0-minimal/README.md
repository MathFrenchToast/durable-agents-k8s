# Article 1 — Zero-Waste AI Agents on Kubernetes : Le Pattern Socle avec Restate

> **Ce dossier contient le code minimal, brut et fonctionnel servant de matière première au premier article Medium.**
> L'objectif pédagogique : comprendre le concept en **moins de 150 lignes de code**, sans framework intermédiaire ni couche d'abstraction.

---

## 1. Le Problème : Pourquoi 1 Pod par Agent est une Hérésie

Dans la majorité des architectures agentiques sur Kubernetes, on observe deux failles majeures :
1. **95 % de CPU gaspillé :** Un agent passe l'essentiel de son temps à attendre (validation humaine *HITL*, webhook GitHub, réponse API). Garder un Pod réservé consomme de la mémoire et des vCPU pour… ne rien faire.
2. **Vulnérabilité aux crashs :** Si le Pod est redémarré (mise à jour de nœud, OOMKilled), la mémoire de l'agent est détruite. Il faut recommencer les appels LLM précédents, gaspillant temps et tokens d'API.

---

## 2. La Solution : Le Triptyque Gagnant

```text
  [ Utilisateur ]
         │ (1) Lance l'agent / Arbitre la décision
         ▼
┌─────────────────────────┐
│      App Chapeau        │  FastAPI (Gateway HTTP & Dashboard inline)
└────────────┬────────────┘
             │ (2) POST /GitHubIssueResolver/{id}/resolve
             ▼
┌─────────────────────────┐
│     Restate Server      │  Moteur d'Exécution Durable (State Log + Awakeables)
└────────────┬────────────┘
             │ (3) Dispatch la tâche
             ▼
┌─────────────────────────┐
│    Pool de Workers      │  Worker mutualisé (VirtualObject Restate)
│    (agent_service.py)   │  1 Pod traite N agents simultanés
└─────────────────────────┘
             ▲
             │ (4) Scale 1 -> N selon file d'attente Restate
┌─────────────────────────┐
│     KEDA Operator       │  Métrique : restate_service_pending_invocations
└─────────────────────────┘
```

---

## 3. Le Code : 100% Lisible en Moins de 2 Minutes

### Le Worker (`agent_worker/agent_service.py` — ~45 lignes)
Un `VirtualObject` Restate où chaque clé d'objet (`ctx.key()`) est une instance d'agent indépendante :

```python
import os, httpx, restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")
agent_object = restate.VirtualObject("GitHubIssueResolver")

@agent_object.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    repo, issue = payload["target_repo"], payload["issue_id"]

    # 1. Étape d'analyse durable (mémorisée en cas de crash)
    async def analyze():
        return f"Correctif proposé pour {repo}#{issue} : patch sur security.py"
    solution = await ctx.run("analyze", analyze)

    # 2. Point de suspension HITL (0% CPU, Restate garde l'état)
    token, promise = ctx.awakeable()
    
    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(f"{GATEWAY_URL}/api/hitl/notify", json={
                "instance_id": instance_id, "token": token, "message": f"Analyse #{issue} : {solution}"
            })
        return True
    await ctx.run("notify_ui", notify)

    # HIBERNATION COMPLETE : Le thread est libéré immédiatement !
    decision = await promise

    # 3. Reprise post-approbation
    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "action": "PR_CREATED" if approved else "REJECTED",
        "comment": decision.get("feedback", ""),
    }

app = restate.app(services=[agent_object])
```

### L'Application Chapeau (`app_chapeau/main.py` — ~90 lignes)
Centralise le déclenchement, reçoit les awakeables et sert le dashboard web en un seul fichier :
* `POST /api/launch` : invoque Restate de façon asynchrone (`POST /GitHubIssueResolver/{id}/resolve`).
* `POST /api/hitl/notify` : mémorise le jeton `token` en attente.
* `POST /api/hitl/resolve/{id}` : réveille Restate (`POST /restate/awakeables/{token}/resolve`).
* `GET /` : Dashboard HTML embarqué avec polling automatique.

---

## 4. Démarrage et Démonstration Locale en 30 Secondes

### 1. Lancer la stack avec Docker Compose
```bash
docker compose up --build -d
```
* **Console Web HITL :** [http://localhost:8000](http://localhost:8000)
* **Restate Ingress :** [http://localhost:8080](http://localhost:8080)
* **Restate Admin :** [http://localhost:9070](http://localhost:9070)

### 2. Exécuter le test de recette automatique
```bash
./test-demo.sh
```

Ce script valide :
1. Le lancement simultané de 2 instances (`issue-alpha` et `issue-beta`).
2. L'hibernation immédiate des deux agents.
3. La consommation CPU retombant au plancher (**< 0.2% CPU**).
4. La tolérance aux crashs : si on redémarre le conteneur worker, aucun état n'est perdu !
5. Le réveil sélectif : validation d'Alpha (la PR est créée), tandis que Beta reste endormi.

---

## 5. Déploiement sur Kubernetes (k8s/)

Les manifestes situés dans `k8s/` peuvent être appliqués directement :
```bash
kubectl apply -f k8s/01-restate.yaml
kubectl apply -f k8s/02-app-chapeau.yaml
kubectl apply -f k8s/03-agent-workers.yaml
```

---

## 6. Prochaine Étape : Vers l'Industrialisation (Article 2)

Cette v0 démontre le pattern brut. Pour passer à l'échelle en entreprise, le dossier `../v1-extensible/` montre comment :
* Isoler l'agent du framework via une **Architecture Hexagonale (Ports & Adapters)**.
* Remplacer Restate par **Dapr** en modifiant seulement un adaptateur.
* Observer chaque appel et latence via **Arize Phoenix** et OpenTelemetry.
* Router intelligemment les modèles grâce à un **LLM Router** (LiteLLM / fallbacks).
