# Du Chatbot à la Plateforme Agentique : Implémenter l'Exécution Durable sur Kubernetes avec Restate et KEDA

## *Guide pratique pour concevoir un runtime d'agents asynchrones tolérant aux pannes et sobre en ressources.*

---

![Architecture Overview](https://raw.githubusercontent.com/restatedev/restate/main/docs/static/img/restate_hero.png)
*(Le plan de contrôle d'exécution durable au cœur de l'infrastructure d'agents)*

---

### 1. Le Fossé Technique : Pourquoi un Agent n'est pas un Chatbot

Déployer un chatbot sur Kubernetes est un problème résolu : une requête HTTP synchrone arrive, le modèle génère un flux de tokens pendant 5 à 15 secondes, le Pod renvoie la réponse et la connexion se ferme. Le service est sans état (*stateless*), les requêtes sont isolées, et un `Deployment` Kubernetes standard associé à un Horizontal Pod Autoscaler (HPA) basé sur le CPU ou le trafic HTTP remplit parfaitement son rôle.

**Dès que l'on bascule vers une plateforme agentique, cette mécanique s'effondre.**

Un agent d'IA ne se contente pas de répondre à une question : il poursuit un objectif en exécutant une suite d'actions asynchrones réparties dans le temps :
* Il inspecte des systèmes tiers (APIs, bases de données, outils via le protocole MCP).
* Il formule un plan ou génère un artefact (un patch de code, un document, une transaction).
* Il doit **suspendre son exécution** pour attendre un arbitrage humain (*Human-in-the-Loop* / HITL) ou un webhook externe, ce qui peut prendre de quelques minutes à plusieurs jours.
* Il reprend son calcul dès réception du signal pour appliquer l'action finale (déploiement, merge de Pull Request, envoi d'email).

```text
               CYCLE SYNCHRONE DU CHATBOT (Simple, Stateless)
  Utilisateur ──( HTTP POST )──> [ Pod Chatbot ] ──( Inférence LLM )──> Réponse (3s)

            CYCLE ASYNCHRONE DE L'AGENT (Long-lived, Stateful, HITL)
  Trigger ──> [ Étape 1 : Analyse ] ──> [ Outils MCP ]
                      │
                      ▼
              [ SUSPENSION HITL ] ──( Attente humaine : 10 min à 48h ? )
                      │
   Signal Humain ─────┘
          │
          ▼
     [ Étape 2 : Action à effet de bord (PR, Déploiement) ]
```

#### Pourquoi Kubernetes échoue nativement sur ce cas d'usage :
1. **L'inadéquation de la réservation de ressources :** Garder un Pod et un thread Python alloués pendant qu'un humain lit un rapport immobilise inutilement de la mémoire et des réservations de vCPU. Multiplié par des centaines d'agents, le cluster sature à ne rien faire.
2. **La vulnérabilité aux évictions (*Loss of In-Flight State*) :** Dans un cluster dynamique (mises à jour de nœuds, redémarrages OOMKilled, instances Spot/Preemptible), un Pod peut être tué à tout instant. Si le conteneur meurt pendant que l'agent est à l'étape 3 sur 5, toute la mémoire vive est détruite. Deux choix s'offrent alors à vous, tous deux désastreux :
   * Relancer l'agent depuis le début (gaspillage de tokens LLM et risque de ré-exécuter des outils qui ont déjà produit des effets de bord dans vos systèmes).
   * Concevoir soi-même un système complexe de persistance par base de données, files RabbitMQ/Kafka, gestion d'idempotence et machines à états.

Pour résoudre ce problème sans réinventer une plomberie distribuée sur mesure, nous combinons **Restate** (moteur d'Exécution Durable) et **KEDA** (Kubernetes Event-driven Autoscaling).

---

## 2. Le Paradigme de l'Exécution Durable (*Durable Execution*)

L'Exécution Durable garantit qu'un programme reprend son cours exactement là où il s'est arrêté en cas de crash, sans jamais ré-exécuter les étapes déjà validées.

Dans cette architecture :
* **Restate** agit comme un plan de contrôle d'exécution léger. Il intercepte chaque étape de l'agent, consigne les résultats dans un journal d'événements immuable et gère les réveils asynchrones.
* **Les Workers** sont banalisés. Au lieu d'avoir un Pod dédié par agent, un pool de workers génériques exécute des objets virtuels (*Virtual Objects*). L'identifiant de la tâche (ex: le ticket GitHub) sert de clé d'isolation d'état (`ctx.key()`).
* **Les Awakeables** permettent la suspension à coût processeur nul. L'agent génère un jeton d'attente et libère immédiatement son thread d'exécution. La consommation CPU du conteneur retombe à zéro absolu.

```text
                           UTILISATEUR / SYSTÈME
                                      │
                         (1) Lancement de l'agent
                         (3) Arbitrage humain (Approuver / Rejeter)
                                      ▼
             ┌──────────────────────────────────────────────────┐
             │       APPLICATION CHAPEAU (FastAPI / Gateway)    │
             │   - Console Web d'arbitrage (HITL)               │
             │   - Ingestion et dispatch vers Restate           │
             └────────┬────────────────────────────────▲────────┘
                      │                                │
             (1) POST /resolve                         │ (2) POST /hitl/notify
                      ▼                                │     (Token Awakeable)
             ┌─────────────────────────────────────────┴────────┐
             │            RESTATE SERVER (Control Plane)        │
             │   - Journal d'état déterministe (Event Log)      │
             │   - Gestion des promesses d'attente (Awakeables) │
             │   - Métriques Prometheus de file d'attente (:9070)│
             └────────┬─────────────────────────────────────────┘
                      │
           (2) Dispatch tâche
                      ▼
             ┌──────────────────────────────────────────────────┐
             │        WORKER POOL AGENTS (K3s Deployment)       │
             │   - Restate VirtualObject ("GitHubIssueResolver")│
             │   - 1 Pod traite N agents simultanés             │
             └────────────────────────▲─────────────────────────┘
                                      │
                 (4) Scaling 1 -> N pods selon charge
                                      │
             ┌────────────────────────┴─────────────────────────┐
             │       KEDA OPERATOR + PROMETHEUS STACK           │
             │   Scrape métrique : restate_service_pending_invocations │
             └──────────────────────────────────────────────────┘
```

---

## 3. Implémentation : Moins de 150 Lignes de Code Direct

Pour ce premier article, nous adoptons une implémentation directe, sans couche d'abstraction superflue, pour exposer la mécanique brute.

### A. Le Worker Agent (`agent_worker/agent_service.py` — 45 lignes)

L'agent modèle est un résolveur de tickets GitHub : il analyse le problème, propose un patch, se met en hibernation pour validation humaine, puis crée la Pull Request.

```python
import os
import httpx
import restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")
agent_object = restate.VirtualObject("GitHubIssueResolver")

@agent_object.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key() # Clé d'isolation logique (ex: repo#issue)
    repo = payload.get("target_repo", "mon-orga/repo")
    issue_id = payload.get("issue_id", "42")

    # 1. ÉTAPE DURABLE (Idempotente & Journalisée par Restate)
    async def analyze():
        # Simulation d'inspection de code (MCP / LLM)
        return f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."

    solution = await ctx.run("analyze", analyze)

    # 2. POINT DE SUSPENSION HITL (Génération de token)
    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{GATEWAY_URL}/api/hitl/notify",
                json={
                    "instance_id": instance_id,
                    "token": token,
                    "message": f"Analyse #{issue_id} sur {repo} terminée. Solution : {solution}",
                },
                timeout=10.0,
            )
        return True

    await ctx.run("notify_ui", notify)

    # ⚠️ HIBERNATION : Le thread est immédiatement libéré.
    # Aucune ressource CPU ni mémoire vive n'est retenue sur le worker.
    decision = await promise

    # 3. REPRISE POST-ARBITRAGE
    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "PR_CREATED" if approved else "REJECTED",
        "comment": decision.get("feedback", "Approuvé" if approved else "Rejeté"),
    }

app = restate.app(services=[agent_object])
```

#### Ce qui s'exécute réellement :
1. **`ctx.run("analyze", analyze)`** : Restate exécute la fonction et sérialise la réponse dans son log. Si le pod redémarre juste après, Restate injecte le résultat sans ré-exécuter `analyze()`.
2. **`token, promise = ctx.awakeable()`** : Restate génère un token d'attente externe.
3. **`await promise`** : L'exécution se fige. Le serveur Restate met la tâche en sommeil et le worker Uvicorn redevient 100 % disponible pour d'autres agents.

---

### B. L'Application Chapeau (`app_chapeau/main.py` — 90 lignes)

Un service FastAPI unique sert à la fois d'API d'ingestion et de console web de contrôle humaine.

```python
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="App Chapeau v0")
RESTATE_INGRESS = os.getenv("RESTATE_INGRESS_URL", "http://restate:8080").rstrip("/")
pending = {} # Registre local des instances en attente d'arbitrage

class LaunchPayload(BaseModel):
    instance_id: str
    target_repo: str = "mon-orga/backend"
    issue_id: str = "42"

@app.post("/api/launch")
async def launch_agent(body: LaunchPayload):
    # Appel non-bloquant vers le Virtual Object de Restate
    url = f"{RESTATE_INGRESS}/GitHubIssueResolver/{body.instance_id}/resolve"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json={"target_repo": body.target_repo, "issue_id": body.issue_id}, timeout=3.0)
        except httpx.ReadTimeout:
            pass # Pris en charge de manière asynchrone par Restate
    return {"status": "dispatched", "instance_id": body.instance_id}

@app.post("/api/hitl/notify")
def receive_notification(body: dict):
    pending[body["instance_id"]] = {"token": body["token"], "message": body["message"], "status": "waiting"}
    return {"status": "stored"}

@app.get("/api/hitl/pending")
def list_pending():
    return {k: v for k, v in pending.items() if v["status"] == "waiting"}

@app.post("/api/hitl/resolve/{instance_id}")
async def resolve_interaction(instance_id: str, body: dict):
    item = pending.get(instance_id)
    if not item or item["status"] != "waiting":
        raise HTTPException(status_code=404, detail="Instance introuvable ou déjà résolue")

    # Résolution de l'awakeable auprès de l'API Ingress de Restate
    url = f"{RESTATE_INGRESS}/restate/awakeables/{item['token']}/resolve"
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json={"approved": body.get("approved", True), "feedback": body.get("feedback", "")})
        if res.status_code >= 400:
            raise HTTPException(status_code=500, detail="Échec de résolution Restate")

    item["status"] = "resolved"
    return {"status": "success", "instance_id": instance_id}
```

---

## 4. Autoscaling Piloté par les Files d'Attente : KEDA + Prometheus

Dans une architecture agentique, dimensionner les workers sur le CPU est une erreur : un worker qui traite 50 agents en hibernation consomme 0 % de CPU. L'autoscaleur classique de Kubernetes (HPA) ne verrait aucune charge et refuserait de scaler.

Nous configurons **KEDA** pour écouter la métrique Prometheus exposée directement par Restate sur son port d'administration `:9070` :

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: keda-agent-workers
spec:
  scaleTargetRef:
    name: agent-workers
  minReplicaCount: 1
  maxReplicaCount: 5
  pollingInterval: 5
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prom-stack-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090
        metricName: restate_pending_invocations
        query: sum(restate_service_pending_invocations{service="GitHubIssueResolver"})
        threshold: "2"
```

* **Sous la pression :** Si plus de 2 calculs d'agents sont en file d'attente active, KEDA augmente le nombre de Pods workers (jusqu'à 5).
* **Au repos :** Dès que les agents passent en attente HITL ou se terminent, la file active tombe à 0. KEDA réduit la flotte à son minimum (1 Pod).

---

## 5. Le Crash-Test Pratique : Vérification des Promesses

Pour tester la robustesse du système, nous avons déroulé le protocole suivant (reproductible via `./test-demo.sh` dans le dépôt) :

```text
  1. Lancement de 2 instances en parallèle : issue-alpha et issue-beta
                      │
                      ▼
  2. Vérification de l'hibernation HITL (Console Web & docker stats)
     --> CPU Worker mesuré : 0.11 %  (Zéro thread actif)
                      │
                      ▼
  3. CHAOS TEST : Kill brutal du conteneur worker (docker restart / kill -9)
                      │
                      ▼
  4. Réveil post-redémarrage : Approbation de l'instance Alpha
     --> Résultat : L'étape 'analyze' N'EST PAS ré-exécutée.
         Alpha crée sa PR. Beta reste en pause intacte.
```

### Le relevé de métriques en direct :
```bash
$ docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" v0-agent-worker
NAME              CPU %     MEM USAGE / LIMIT
v0-agent-worker   0.11%     28.41MiB / 30.99GiB
```

Lorsque le worker est redémarré de force pendant l'hibernation :
1. Le nouveau conteneur worker s'auto-enregistre auprès de Restate en 1 seconde.
2. Lorsqu'un opérateur clique sur **Approuver** sur `issue-alpha`, Restate réactive la promesse.
3. Le nouveau worker reprend l'exécution directement à la ligne `decision = await promise`. **L'appel d'analyse préalable n'a pas été rejoué**.
4. L'instance `issue-beta` continue de sommeiller dans Restate sans aucune perturbation.

---

## Synthèse : Du Script Éphémère à l'Agent Durable

| Critère | Approche Pod par Agent | Exécution Durable (Restate + KEDA) |
| :--- | :--- | :--- |
| **Coût d'attente humaine (HITL)** | Pod réservé, thread bloqué | **0 % CPU, thread libéré immédiatement** |
| **Comportement sur crash du Pod** | Contexte perdu, ré-exécution totale | **Reprise chirurgicale au checkpoint exact** |
| **Idempotence des outils MCP** | Risque d'effets de bord dupliqués | **Garantie par le journal de Restate (`ctx.run`)** |
| **Capacité d'accueil par nœud** | Limitée par le nombre de Pods (~100) | **Des milliers d'agents logiques par worker** |

---

## Quelle Suite pour l'Échelle Entreprise ?

Ce pattern socle prouve qu'un environnement d'exécution d'agents léger et robuste tient en un minimum de code. Mais pour une intégration en production à grande échelle, des questions fondamentales se posent :

1. **Comment éviter d'enfermer sa logique métier dans le SDK Restate** et conserver la liberté de basculer vers **Dapr Workflows** sans réécrire les agents ?
2. **Comment tracer les chaînes de pensée, latences et coûts de tokens** à l'aide d'outils d'observabilité spécialisés comme **Arize Phoenix** et **OpenTelemetry** ?
3. **Comment router dynamiquement les requêtes de modèles** vers les LLMs les plus efficients (Claude 3.5 Sonnet, GPT-4o-mini, modèles locaux vLLM/Ollama) avec des chaînes de repli automatiques ?

👉 **Rendez-vous dans notre second article :** *« Architecture d'une Plateforme Agentique d'Entreprise : Découplage Hexagonal, Observabilité Phoenix et Routage Multi-LLM »* (implémenté dans le répertoire `v1-extensible/` du projet).

---

*Le code source complet, les Dockerfiles et le script de test automatique sont disponibles dans le dossier `v0-minimal/` de ce dépôt.*
