# Du Chatbot à la Plateforme Agentique : Implémenter l'Exécution Durable sur Kubernetes avec Restate et KEDA

## *Guide pratique pour concevoir un runtime d'agents asynchrones tolérant aux pannes et sobre en ressources.*

---

![Architecture Overview](https://docs.restate.dev/img/overview/event_processing.png)
*(Le plan de contrôle d'exécution durable au cœur de l'infrastructure d'agents)*

---

### 1. Le Fossé Technique : Pourquoi un Agent n'est pas un Chatbot

Déployer un chatbot sur Kubernetes est un problème résolu : une requête HTTP synchrone arrive, le modèle génère un flux de tokens pendant 5 à 15 secondes, le Pod renvoie la réponse et la connexion se ferme. Le service est sans état (*stateless*), les requêtes sont isolées, et un `Deployment` Kubernetes standard associé à un Horizontal Pod Autoscaler (HPA) basé sur le CPU ou le trafic HTTP remplit parfaitement son rôle.

**Dès que l'on bascule vers une plateforme agentique, cette mécanique ne suffit plus.**

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

#### Kubernetes seul échoue nativement sur ce cas d'usage :
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

### Où Réside l'État ? Démystifier le Stockage (Zéro RWX, Zéro BDD Externe)

Une question d'architecture revient systématiquement lorsqu'on aborde la mutualisation d'agents sur Kubernetes : *« Si plusieurs workers traitent des instances d'agents simultanées, faut-il un volume partagé ReadWriteMany (RWX / NFS) ou une base PostgreSQL externe ? »*

La réponse est **non, absolument pas**. L'architecture repose sur une séparation étanche entre le **plan de calcul** (*compute*) et le **plan d'état** (*state store*) :

```text
                  PLAN DE CALCUL (Workers Python)
      ┌───────────────────────────────────────────────────────┐
      │  Pods Workers : 100 % STATELESS (Sans disque, sans PVC)│
      │  - Scalent librement de 0 à N Pods via KEDA           │
      │  - Exécutent le code et communiquent uniquement par HTTP│
      └──────────────────────────┬────────────────────────────┘
                                 │ HTTP réseau (:8080)
                                 ▼
                  PLAN D'ÉTAT (Moteur Restate en Rust)
      ┌───────────────────────────────────────────────────────┐
      │  Serveur Restate : AUTONOME (0 PostgreSQL, 0 Redis)   │
      │  - Journal Write-Ahead Log (WAL) + LSM-Tree intégré   │
      │  - Stockage : simple disque bloc ReadWriteOnce (RWO)  │
      └───────────────────────────────────────────────────────┘
```

1. **Les Workers sont 100 % Sans Disque (*Stateless*) :**
   Les Pods `agent-worker` ne montent aucun système de fichiers persistant. Ils reçoivent les requêtes par HTTP depuis Restate, exécutent l'étape (appel LLM, mock, outil), retournent la valeur par HTTP, et s'arrêtent. Ils peuvent scaler de 0 à 100 Pods sur 30 nœuds différents sans aucune friction de stockage partagé (le cauchemar opérationnel des volumes NFS/RWX est totalement évité).
2. **Restate est un Moteur Transactionnel Autonome (*Self-Contained*) :**
   Contrairement à des solutions comme Temporal (qui impose un cluster PostgreSQL ou Cassandra) ou Airflow, Restate est écrit en Rust et intègre nativement son propre moteur de stockage transactionnel (*Write-Ahead Log* + *LSM-Tree*).
   - **Dans notre V0 (socle de démonstration) :** Restate tourne avec un réplica unique adossé à un simple disque bloc standard **ReadWriteOnce (RWO)** (comme un EBS AWS ou un disque persistant GCP classique), voire un stockage éphémère de test.
   - **Pour la production (abordée dans l'Article 2 / V1) :** La haute disponibilité s'obtient par un cluster Restate distribué avec consensus **Raft** (chaque réplica disposant de son propre disque bloc RWO indépendant) et archivage des états froids sur un **Object Storage (S3/GCS)** — toujours sans aucun système de fichiers partagé RWX.

---

## 3. Implémentation en 150 Lignes de Code

Pour ce premier article, nous adoptons une implémentation directe, sans couche d'abstraction superflue, pour exposer la mécanique brute. Nous élaborerons plus tard une version plus aboutie sur la base de cette première version.

### A. Le Worker Agent (`agent_worker/agent_service.py` — ~75 lignes)

Pour prouver qu'un même déploiement de worker peut gérer **plusieurs instances** mais également **plusieurs types d'agents différents**, notre worker enregistre deux agents de démonstration sous forme de `VirtualObject` Restate :
1. **`GitHubIssueResolver`** : résout des tickets GitHub via sa méthode métier dédiée `resolve`.
2. **`MeetingScheduler`** : planifie des créneaux de réunions via sa méthode métier dédiée `schedule`.

```python
import asyncio
import os
import httpx
import restate
from restate import ObjectContext

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau:8000")

# 1. PREMIER AGENT : Résolveur GitHub (méthode métier: resolve)
github_issue_resolver = restate.VirtualObject("GitHubIssueResolver")

@github_issue_resolver.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key() # Clé d'isolation logique (ex: repo#issue)
    repo = payload.get("target_repo", "mon-orga/repo")
    issue_id = payload.get("issue_id", "42")

    # Étape durable (simulation temps de réponse LLM / analyse)
    async def analyze():
        await asyncio.sleep(2.0)
        return f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."

    solution = await ctx.run("analyze", analyze)

    # Point de suspension HITL (Génération de token)
    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(f"{GATEWAY_URL}/api/hitl/notify", json={
                "instance_id": instance_id,
                "agent_type": "GitHubIssueResolver",
                "token": token,
                "message": f"Analyse #{issue_id} sur {repo} terminée. Solution : {solution}",
            }, timeout=10.0)
        return True

    await ctx.run("notify_ui", notify)

    # ⚠️ HIBERNATION COMPLETE : Le thread et le CPU sont immédiatement libérés
    decision = await promise

    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "PR_CREATED" if approved else "REJECTED",
        "comment": decision.get("feedback", "Approuvé" if approved else "Rejeté"),
    }

# 2. SECOND AGENT : Planificateur de Réunions (méthode métier: schedule)
meeting_scheduler = restate.VirtualObject("MeetingScheduler")

@meeting_scheduler.handler()
async def schedule(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key() # Clé d'isolation (ex: meet-201)
    topic = payload.get("topic", "Point hebdo architecture")
    participants = payload.get("participants", "equipe@orga.com")

    async def find_slot():
        await asyncio.sleep(2.0) # Simulation vérification d'agendas
        return f"Créneau optimal pour '{topic}' ({participants}) : Jeudi 14h00-15h00."

    slot_proposal = await ctx.run("find_slot", find_slot)

    token, promise = ctx.awakeable()

    async def notify():
        async with httpx.AsyncClient() as client:
            await client.post(f"{GATEWAY_URL}/api/hitl/notify", json={
                "instance_id": instance_id,
                "agent_type": "MeetingScheduler",
                "token": token,
                "message": f"Confirmation de réunion requise : {slot_proposal}",
            }, timeout=10.0)
        return True

    await ctx.run("notify_ui", notify)

    decision = await promise # HIBERNATION

    approved = decision.get("approved", False)
    return {
        "status": "completed" if approved else "aborted",
        "instance": instance_id,
        "action": "INVITATION_SENT" if approved else "CANCELLED",
        "comment": decision.get("feedback", "Confirmée" if approved else "Annulée"),
    }

# Déploiement commun : un seul worker héberge les deux agents
app = restate.app(services=[github_issue_resolver, meeting_scheduler])
```

#### Ce qui s'exécute réellement :
1. **Isolation par clé et par type** : Pour un même agent (ex: `GitHubIssueResolver`), chaque instance (`issue-alpha`, `issue-beta`) possède son propre état et sa file isolée via `ctx.key()`. Deux agents de types différents cohabitent sans interférence dans le même runtime.
2. **`ctx.run(...)`** : Restate exécute la fonction et sérialise la réponse dans son log. Si le pod redémarre juste après, Restate injecte le résultat sans recalculer.
3. **`token, promise = ctx.awakeable()`** : Restate génère un token d'attente externe.
4. **`await promise`** : L'exécution se fige. Le serveur Restate met la tâche en sommeil et le worker Uvicorn redevient 100 % disponible pour d'autres agents.

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

## 4. Autoscaling Événementiel : Pourquoi KEDA Écrase le HPA Classique

### A. Le Paradoxe des Agents IA : Pourquoi le HPA Classique Échoue

Sur Kubernetes, l'approche réflexe pour l'autoscaling consiste à déployer un **HPA** (*Horizontal Pod Autoscaler*) basé sur la consommation CPU ou mémoire. Pour des agents IA durables, **ce modèle est un contresens architectural**.

On pourrait naïvement objecter : *« S'il n'y a pas de requêtes, il n'y a pas de CPU consommé, et quand des requêtes arrivent, le processeur chauffe : le HPA ne peut-il pas simplement détecter ces variations ? »*

La réalité du cycle de vie des agents révèle deux pièges majeurs :

1. **Le piège de l'attente humaine (HITL) : Le faux négatif du HPA**
   Imaginez que 50 agents soient déclenchés. Après 2 secondes d'analyse, ils génèrent chacun un jeton d'attente et se suspendent (`await promise`) en attendant la validation d'un chef de projet.
   - Ces 50 agents sont **toujours en cours d'exécution** dans le système (leur cycle de vie n'est pas terminé).
   - Pourtant, grâce à l'hibernation Restate, la consommation CPU du worker s'effondre à **0.1 %**.
   - Le HPA natif interprète ces 0.1 % de CPU comme une inactivité totale : il décide de désallouer les Pods. Dans une architecture non durable classique, les Pods sont tués et la mémoire des agents est détruite. Avec Restate, l'état survit, mais le HPA reste aveugle au fait que 50 tâches sont prêtes à reprendre dès le premier clic humain.

2. **Les agents sont "I/O Bound", pas "CPU Bound"**
   Même en phase active de réflexion, un agent passe 90 % de son temps à attendre des paquets réseau (tokens streamés d'un LLM comme Claude ou GPT-4, appels d'APIs tierces, webhooks). Attendre des sockets réseau en Python `asyncio` consomme des poussières de CPU. Un worker gérant 40 streams d'agents simultanés peut n'utiliser que 15 % de CPU : un HPA configuré avec un seuil standard (70 %) **ne scalera jamais**, laissant le worker saturer ses sockets réseau.

> **Le principe fondamental :** Pour les agents durables, le CPU et la RAM ne sont plus des indicateurs de charge fiables. **Le seul signal de vérité est la profondeur de la file d'attente des étapes prêtes à être calculées.**

---

### B. La Déclaration KEDA (`k8s/03-agent-workers.yaml`)

KEDA interroge la métrique Prometheus exposée nativement par le port d'administration de Restate (`:9070`) :

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: keda-agent-workers
spec:
  scaleTargetRef:
    name: agent-workers
  minReplicaCount: 1      # 1 Pod "Warm" maintenu pour éliminer tout cold start
  maxReplicaCount: 5      # Plafond de saturation
  pollingInterval: 5      # Interrogation de Restate toutes les 5 secondes
  cooldownPeriod: 30       # Temporisation de 30s avant de réduire la flotte
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prom-stack-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090
        metricName: restate_pending_invocations
        query: sum(restate_service_pending_invocations{service=~"GitHubIssueResolver|MeetingScheduler"})
        threshold: "2"    # Déclenche un nouveau Pod dès que 2 tâches s'accumulent
```

---

### C. La Dynamique en Direct : Scénario de Vie Pas-à-Pas

Voici le cycle de vie dynamique observé sur le cluster :

1. **Régime nominal (Au repos) :**
   Aucune invocation active. La file Restate est à 0. KEDA maintient le pool au plancher (`minReplicaCount: 1`). Ce Pod unique, hébergeant à la fois l'agent GitHub et l'agent Réunion, ne consomme que ~40 Mo de RAM et 0% de CPU.
2. **Arrivée d'un pic :**
   Une salve de 10 tickets GitHub et 5 réunions est injectée. Restate journalise les intentions et incrémente instantanément sa métrique interne : `pending_invocations = 15`.
3. **Réaction immédiate de KEDA :**
   KEDA constate $15 > 2$. Sans attendre que les processeurs ne chauffent, il commande à Kubernetes de scaler le pool au maximum : **5 Pods sont provisionnés**.
4. **Traitement parallèle :**
   Les 5 Pods absorbent les 15 calculs d'analyse en parallèle.
5. **Passage en hibernation HITL :**
   Dès que les agents émettent leur notification et exécutent `await promise`, l'étape active se clôture. Restate endort les coroutines. La métrique `pending_invocations` **retombe immédiatement à 0**, alors même que les 15 processus d'arbitrage restent ouverts.
6. **Descente contrôlée (*Cooldown*) :**
   Après 30 secondes de stabilité à 0, KEDA réduit automatiquement le cluster à 1 Pod. Les 15 agents attendent patiemment leur signal humain sans mobiliser le moindre cœur CPU superflu.

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
4. **Comment allier autoscaling horizontal et vertical (KEDA + VPA) :** Ajuster dynamiquement la mémoire des workers aux charges imprévisibles (diffs massifs, gros documents) sans redémarrer les Pods (*In-Place Pod Resize*) tout en laissant KEDA piloter le nombre de Pods ?

👉 **Rendez-vous dans notre second article :** *« Architecture d'une Plateforme Agentique d'Entreprise : Découplage Hexagonal, Observabilité Phoenix et Routage Multi-LLM »* (implémenté dans le répertoire `v1-extensible/` du projet).

---

*Le code source complet, les Dockerfiles et le script de test automatique sont disponibles dans le dossier `v0-minimal/` de ce dépôt.*
