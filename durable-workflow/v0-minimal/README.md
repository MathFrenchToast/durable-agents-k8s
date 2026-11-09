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
             │ (2) POST /{Service}/{id}/{handler}
             ▼
┌─────────────────────────┐
│     Restate Server      │  Moteur d'Exécution Durable (State Log + Awakeables)
└────────────┬────────────┘
             │ (3) Dispatch la tâche
             ▼
┌─────────────────────────┐
│    Pool de Workers      │  Worker mutualisé hébergeant 2 agents distincts :
│    (agent_service.py)   │  - GitHubIssueResolver (handler: resolve)
│                         │  - MeetingScheduler (handler: schedule)
└─────────────────────────┘
             ▲
             │ (4) Scale selon métrique de file d'attente Restate
┌─────────────────────────┐
│     KEDA Operator       │  Métrique : restate_service_pending_invocations
└─────────────────────────┘
```

### Architecture de Déploiement : Zéro RWX, Zéro BDD Externe

Une interrogation classique consiste à se demander où réside l'état et si des volumes partagés (*ReadWriteMany* / NFS) sont requis :

1. **Les Workers sont 100 % Stateless (Sans Disque) :**
   - Les Pods workers (`agent-worker`) ne montent **aucun volume PVC**.
   - Ils scalent librement de 0 à $N$ Pods sur n'importe quel nœud du cluster Kubernetes.
   - Ils ne font qu'exécuter des coroutines I/O et communiquent avec Restate exclusivement par le réseau (HTTP).
2. **Le Serveur Restate est Autonome (*Self-Contained*) :**
   - Contrairement à Temporal ou Airflow, Restate **n'exige aucune base de données externe** (aucun PostgreSQL ni Redis à gérer).
   - Écrit en Rust, il embarque son propre moteur transactionnel (*Write-Ahead Log* + *LSM-Tree*).
   - En **v0** : Restate utilise un simple disque bloc standard **ReadWriteOnce (RWO)** (ou stockage éphémère).
   - En **v1 (Production)** : La résilience s'obtient via un cluster multi-réplicas avec consensus **Raft** et archivage de snapshots sur **Object Storage (S3/GCS)** — toujours sans aucun système de fichiers partagé RWX.

### Pourquoi KEDA plutôt qu'un HPA classique ?

Le HPA standard de Kubernetes basé sur le CPU ou la mémoire est un **piège pour les agents IA** :
* **Le faux négatif de l'attente humaine (HITL) :** Si 50 agents attendent un arbitrage humain, ils sont toujours actifs en mémoire Restate mais consomment **0.1 % de CPU**. Le HPA natif conclut à une inactivité et tue les Pods.
* **Les agents sont "I/O Bound" :** Attendre des tokens streamés d'un LLM consomme des miettes de CPU. Un worker saturant ses connexions réseau ne dépassera peut-être jamais le seuil HPA de 70% CPU.
* **KEDA surveille la file d'attente Restate :** Il scale en fonction de `restate_service_pending_invocations`.

#### La Dynamique en Direct (Scénario de Vie) :
1. **Au repos :** 0 tâche active $\rightarrow$ KEDA maintient le worker au plancher (`minReplicaCount: 1`, soit ~40 Mo de RAM, 0% CPU, zéro cold start).
2. **Pic d'arrivée :** 15 requêtes arrivent d'un coup $\rightarrow$ La métrique Restate passe à 15 $\rightarrow$ KEDA scale immédiatement à 5 Pods.
3. **Calcul & Hibernation :** Les Pods exécutent l'étape active puis s'endorment sur `await promise` $\rightarrow$ La file active retombe à 0.
4. **Descente (*Cooldown*) :** KEDA redescend automatiquement à 1 Pod après 30s de temporisation, pendant que les 15 agents attendent leur validation sans aucun CPU gaspillé.

---

## 3. Le Code : en Moins de 2 Minutes

### Le Worker (`agent_worker/agent_service.py` — ~75 lignes)
Un déploiement de worker unique enregistre **deux agents de démonstration distincts** sous forme de `VirtualObject` Restate :

```python
import asyncio, os, httpx, restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")

# 1. Agent Résolveur GitHub (méthode métier propre : resolve)
github_issue_resolver = restate.VirtualObject("GitHubIssueResolver")

@github_issue_resolver.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    repo, issue = payload["target_repo"], payload["issue_id"]

    # Étape d'analyse durable (simulation travail LLM)
    async def analyze():
        await asyncio.sleep(2.0)  # Réflexion / inspection de code
        return f"Correctif proposé pour {repo}#{issue} : patch sur security.py"
    solution = await ctx.run("analyze", analyze)

    # Point de suspension HITL (0% CPU, Restate garde l'état)
    token, promise = ctx.awakeable()
    await ctx.run("notify_ui", lambda: httpx.AsyncClient().post(f"{GATEWAY_URL}/api/hitl/notify", json={
        "instance_id": instance_id, "agent_type": "GitHubIssueResolver", "token": token, "message": f"Analyse #{issue} : {solution}"
    }))

    decision = await promise # HIBERNATION : libération immédiate du thread !
    return {"status": "completed" if decision.get("approved") else "aborted", "action": "PR_CREATED" if decision.get("approved") else "REJECTED"}

# 2. Second Agent : Planificateur de réunions (méthode métier propre : schedule)
meeting_scheduler = restate.VirtualObject("MeetingScheduler")

@meeting_scheduler.handler()
async def schedule(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    topic, parts = payload.get("topic", "Sync"), payload.get("participants", "")

    async def find_slot():
        await asyncio.sleep(2.0)  # Négociation d'agendas
        return f"Créneau optimal trouvé pour '{topic}' : Jeudi 14h00."
    slot = await ctx.run("find_slot", find_slot)

    token, promise = ctx.awakeable()
    await ctx.run("notify_ui", lambda: httpx.AsyncClient().post(f"{GATEWAY_URL}/api/hitl/notify", json={
        "instance_id": instance_id, "agent_type": "MeetingScheduler", "token": token, "message": f"Validation réunion : {slot}"
    }))

    decision = await promise # HIBERNATION
    return {"status": "completed" if decision.get("approved") else "aborted", "action": "INVITATION_SENT" if decision.get("approved") else "CANCELLED"}

# Enregistrement des deux agents dans un worker commun
app = restate.app(services=[github_issue_resolver, meeting_scheduler])
```

### L'Application Chapeau (`app_chapeau/main.py`)
Centralise le déclenchement multi-agents, reçoit les awakeables et sert le dashboard web :
* `POST /api/launch` : route dynamiquement vers `POST /GitHubIssueResolver/{id}/resolve` ou `POST /MeetingScheduler/{id}/schedule`.
* `POST /api/hitl/notify` : mémorise le jeton `token` en attente pour chaque instance.
* `POST /api/hitl/resolve/{id}` : réveille Restate (`POST /restate/awakeables/{token}/resolve`).
* `GET /` : Console Web permettant de tester les deux types d'agents et plusieurs instances simultanées.

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
1. Le lancement simultané de 2 instances d'un 1er agent (`issue-alpha` et `issue-beta` sur `GitHubIssueResolver` via `resolve`).
2. Le lancement simultané d'un 2nd agent distinct (`meet-standup` sur `MeetingScheduler` via `schedule`).
3. L'hibernation immédiate des 3 agents dans le même conteneur worker.
4. La consommation CPU retombant au plancher (**< 0.2% CPU**) malgré 3 agents en attente.
5. Le réveil sélectif indépendant de chaque agent (`issue-alpha` approuvé, `meet-standup` confirmé, `issue-beta` rejeté).

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
* **Autoscaling Hybride (KEDA + VPA) :** KEDA scale à l'horizontal sur la file Restate tandis que le VPA ajuste la RAM/CPU à chaud selon la lourdeur des charges (*In-Place Pod Resize*).
