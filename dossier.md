# Dossier de Réalisation Technique — Architecture Agentique Durable v0.1

Ce document constitue le cahier des charges et le guide d'implémentation complet du MVP d'une plateforme d'agents sur Kubernetes. Il est directement exploitable par un ingénieur ou un agent de développement automatisé, et structuré pour servir de matière première à une publication technique (Medium).

---

## 1. Vision et Architecture (« Quoi & Pourquoi »)

### Le Problème

Déployer des agents d'intelligence artificielle sur Kubernetes conduit rapidement à deux écueils majeurs :

1. **Le gaspillage de ressources :** Faire tourner un Pod dédié par agent alors que 95 % de son temps d'existence est passé à attendre une validation humaine (*Human-in-the-Loop* / HITL) ou le retour d'un webhook externe sature inutilement les nœuds du cluster.
2. **L'incapacité de Kubernetes à gérer l'état asynchrone long :** Si un Pod est redémarré (mise à jour, éviction, OOM) pendant une boucle de raisonnement multi-outils, le contexte en mémoire vive est détruit, obligeant à relancer les étapes passées et gaspillant des tokens d'API.

### La Solution

Cette architecture couple trois principes :

* **Durable Execution (Restate) :** Restate agit en plan de contrôle d'exécution. Il journalise chaque étape (*step checkpointing*), gère les suspensions (*awakeables*) sans consommation de ressources et assure des reprises déterministes en cas de crash.
* **Elastic Worker Pool (K3s + KEDA + Prometheus) :** Les conteneurs agents ne sont pas des entités permanentes par agent, mais un pool de workers banalisés capables d'exécuter des dizaines d'instances logiques simultanées via des objets virtuels (*Virtual Objects*).
* **Application Chapeau (Gateway & HITL) :** Un service FastAPI/Uvicorn unifié expose un catalogue d'agents déclaratif, route les ordres vers Restate et présente une console d'interaction humaine centralisée.

### Schéma d'Architecture Globale

```text
                           UTILISATEUR / DÉVELOPPEUR
                                      │
                         (Catalogue, Run, Arbitrage)
                                      ▼
             ┌──────────────────────────────────────────────────┐
             │       APPLICATION CHAPEAU (FastAPI / Uvicorn)    │
             │   - Lecture des manifests agents (Catalog)       │
             │   - Déclenchement d'instances auprès de Restate  │
             │   - Console Web Human-in-the-Loop (HITL)         │
             └────────┬────────────────────────────────┬────────┘
                      │                                ▲
             (1) POST /resolve                         │ (3) POST /hitl/notify
                      ▼                                │     (Token Awakeable)
             ┌─────────────────────────────────────────┴────────┐
             │            RESTATE SERVER (Control Plane)        │
             │   - Journal d'exécution déterministe (State)     │
             │   - Gestion des awakeables (Hibernation)         │
             │   - Exposition des métriques Prometheus (:9070)  │
             └────────┬─────────────────────────────────────────┘
                      │
           (2) Dispatch tâche / Fetch
                      ▼
             ┌──────────────────────────────────────────────────┐
             │        WORKER POOL AGENTS (K3s Deployment)       │
             │   - Service Uvicorn mutualisé                    │
             │   - VirtualObject Restate (Clé = Instance ID)    │
             │   - Logique d'analyse + simulation d'outils MCP  │
             └────────────────────────▲─────────────────────────┘
                                      │
                 (4) Scaling 1 -> N pods selon charge
                                      │
             ┌────────────────────────┴─────────────────────────┐
             │       KEDA OPERATOR + PROMETHEUS STACK           │
             │   Scrape Restate metrics : requêtes en attente   │
             └──────────────────────────────────────────────────┘

```

---

## 2. Guide d'Installation de l'Infrastructure Socle

L'infrastructure repose sur un cluster local **k3d** (K3s conteneurisé), avec **Argo CD**, la suite **kube-prometheus-stack** et **KEDA**.

### 2.1. Initialisation du Cluster k3d

Exécuter la commande suivante pour créer un cluster avec exposition des ports HTTP (App Chapeau / Restate) :

```bash
k3d cluster create agent-cluster \
  --port "8080:80@loadbalancer" \
  --port "9070:9070@loadbalancer" \
  --port "8000:8000@loadbalancer" \
  --agents 2

```

### 2.2. Installation d'Argo CD

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Attente de la disponibilité
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s

```

### 2.3. Installation de la Prometheus Stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install prom-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false

```

### 2.4. Installation de KEDA

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm install keda kedacore/keda \
  --namespace keda \
  --create-namespace

```

---

## 3. Définition de l'Agent et Code du Harness

L'agent modèle est `github-issue-resolver`. Son comportement logique :

1. Analyse le ticket GitHub cible.
2. Formule une proposition de correctif.
3. Se met en **hibernation complète** en générant un jeton `awakeable` transmis à l'App Chapeau.
4. Reprend son exécution dès réception de l'arbitrage humain et valide l'action finale.

### 3.1. Manifeste Catalogue de l'Agent (`catalog/github-issue-resolver.yaml`)

Ce fichier décrit les métadonnées et le schéma d'entrée exploités par l'application d'orchestration :

```yaml
id: "github-issue-resolver"
name: "Résolveur de Tickets GitHub"
description: "Agent autonome d'analyse d'issues et de génération de pull requests."
service_name: "GitHubIssueResolver"
handler: "resolve"
parameters:
  - name: "target_repo"
    label: "Dépôt GitHub"
    type: "string"
    required: true
  - name: "issue_id"
    label: "Numéro de l'issue"
    type: "string"
    required: true

```

### 3.2. Code du Worker Restate (`agent_worker/agent_service.py`)

Le worker implémente un `VirtualObject`. Chaque identifiant d'instance (`ctx.key()`) constitue un espace d'état totalement isolé :

```python
import os
import restate
from restate import ObjectContext
import httpx

agent_object = restate.VirtualObject("GitHubIssueResolver")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://app-chapeau.agent-system.svc.cluster.local:8000")

# Activité résiliente d'analyse
async def execute_issue_analysis(repo: str, issue_id: str) -> str:
    # Simulation d'appels outillés (MCP / LLM) avec retries natifs gérés par Restate
    return f"Correctif proposé pour {repo}#{issue_id} : patch sur src/security.py ligne 88."

# Activité de notification HITL
async def notify_hitl_gateway(instance_id: str, message: str, token: str):
    payload = {
        "instance_id": instance_id,
        "token": token,
        "message": message
    }
    async with httpx.AsyncClient() as client:
        await client.post(f"{GATEWAY_URL}/api/hitl/notify", json=payload, timeout=10.0)

@agent_object.handler()
async def resolve(ctx: ObjectContext, payload: dict) -> dict:
    instance_id = ctx.key()
    repo = payload["target_repo"]
    issue_id = payload["issue_id"]

    # 1. Étape d'analyse durable
    solution = await ctx.run("analyze", lambda: execute_issue_analysis(repo, issue_id))

    # 2. Point de suspension HITL (Hibernation)
    awakeable_id, promise = ctx.awakeable()

    # Envoi du jeton de déblocage vers la console humaine
    await ctx.run("notify_ui", lambda: notify_hitl_gateway(
        instance_id=instance_id,
        message=f"Analyse terminée pour #{issue_id}. Solution : {solution}",
        token=awakeable_id
    ))

    # SUSPENSION DU RUN : Libération immédiate des ressources sur le worker
    decision = await promise

    # 3. Reprise post-approbation
    if decision.get("approved") is True:
        # Simulation de la création de la Pull Request
        return {
            "status": "completed",
            "instance": instance_id,
            "action": "PR_CREATED",
            "comment": decision.get("feedback", "Approuvé sans modification")
        }

    return {
        "status": "aborted",
        "instance": instance_id,
        "action": "REJECTED",
        "comment": decision.get("feedback", "Rejeté par l'opérateur")
    }

app = restate.app(services=[agent_object])

```

### 3.3. `agent_worker/Dockerfile`

Le conteneur utilise **Uvicorn** et s'auto-enregistre auprès du serveur Restate au démarrage grâce à son adresse IP de pod :

```dockerfile
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir restate-sdk uvicorn httpx

COPY agent_service.py .

EXPOSE 9080

CMD ["sh", "-c", "uvicorn agent_service:app --host 0.0.0.0 --port 9080 & sleep 2 && curl -s -X POST http://restate.agent-system.svc.cluster.local:9070/deployments -H 'Content-Type: application/json' -d '{\"uri\": \"http://'$(hostname -i)':9080\"}' && wait"]

```

---

## 4. L'Application Chapeau (Gateway & Console HITL)

Ce composant unique centralise le déclenchement, la visualisation des agents en pause et le réveil déterministe via l'API Restate.

### 4.1. Code Applicatif (`app_chapeau/main.py`)

```python
import os
import yaml
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="Agent Control Plane")

RESTATE_INGRESS = os.getenv("RESTATE_INGRESS_URL", "http://restate.agent-system.svc.cluster.local:8080")
CATALOG_PATH = Path(os.getenv("CATALOG_PATH", "./catalog"))

# Registre d'état des suspensions (En mémoire pour le MVP)
pending_interactions = {}

@app.get("/api/catalog")
def get_catalog():
    specs = []
    for manifest in CATALOG_PATH.glob("*.yaml"):
        with open(manifest) as f:
            specs.append(yaml.safe_load(f))
    return specs

class LaunchPayload(BaseModel):
    agent_id: str
    instance_id: str
    params: dict

@app.post("/api/agents/launch")
async def launch_instance(body: LaunchPayload):
    manifest_path = CATALOG_PATH / f"{body.agent_id}.yaml"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Agent non répertorié")

    with open(manifest_path) as f:
        spec = yaml.safe_load(f)

    # Invocation de la clé Virtual Object sur Restate
    service = spec["service_name"]
    handler = spec["handler"]
    url = f"{RESTATE_INGRESS}/{service}/{body.instance_id}/{handler}"

    async with httpx.AsyncClient() as client:
        try:
            # Appel non-bloquant (mode detached / async Restate)
            await client.post(url, json=body.params, timeout=5.0)
        except httpx.ReadTimeout:
            pass # Restate prend en charge l'exécution asynchrone

    return {"status": "dispatched", "instance_id": body.instance_id}

class NotifyPayload(BaseModel):
    instance_id: str
    token: str
    message: str

@app.post("/api/hitl/notify")
def receive_hitl_notification(body: NotifyPayload):
    pending_interactions[body.instance_id] = {
        "token": body.token,
        "message": body.message,
        "status": "waiting"
    }
    return {"status": "stored"}

@app.get("/api/hitl/pending")
def list_pending():
    return pending_interactions

class ActionPayload(BaseModel):
    approved: bool
    feedback: str = ""

@app.post("/api/hitl/resolve/{instance_id}")
async def resolve_interaction(instance_id: str, body: ActionPayload):
    item = pending_interactions.get(instance_id)
    if not item or item["status"] != "waiting":
        raise HTTPException(status_code=404, detail="Aucun signal attendu pour cette instance")

    token = item["token"]
    resolve_url = f"{RESTATE_INGRESS}/restate/awakeables/{token}/resolve"

    async with httpx.AsyncClient() as client:
        res = await client.post(resolve_url, json={"approved": body.approved, "feedback": body.feedback})
        if res.status_code >= 400:
            raise HTTPException(status_code=500, detail="Échec de résolution Restate")

    item["status"] = "resolved"
    return {"status": "success", "instance_id": instance_id}

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Console Orchestration Agents</title>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 30px auto; padding: 0 15px; }
            .card { border: 1px solid #e1e4e8; border-radius: 6px; padding: 20px; margin-bottom: 25px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            .input-group { margin-bottom: 12px; }
            label { display: block; font-weight: 600; margin-bottom: 4px; font-size: 14px; }
            input, select { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
            button { background: #0366d6; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; font-weight: 600; }
            button:hover { background: #0256b9; }
            .badge-waiting { background: #fffbdd; border: 1px solid #d9b728; padding: 15px; border-radius: 6px; margin-bottom: 15px; }
            .actions { margin-top: 10px; display: flex; gap: 10px; }
            .btn-reject { background: #d73a49; }
        </style>
    </head>
    <body>
        <h1>Plateforme Agentique — Console de Contrôle</h1>
        
        <div class="card">
            <h2>Lancer une nouvelle instance</h2>
            <div class="input-group">
                <label>Agent Cible :</label>
                <select id="agentId"><option value="github-issue-resolver">github-issue-resolver</option></select>
            </div>
            <div class="input-group">
                <label>Identifiant d'Instance :</label>
                <input type="text" id="instanceId" placeholder="ex: issue-core-101">
            </div>
            <div class="input-group">
                <label>Dépôt Git :</label>
                <input type="text" id="repo" value="mon-orga/backend-api">
            </div>
            <div class="input-group">
                <label>Numéro d'Issue :</label>
                <input type="text" id="issueId" value="42">
            </div>
            <button onclick="launchAgent()">Démarrer le Cycle</button>
        </div>

        <div class="card">
            <h2>Interactions Humaines Requises (HITL)</h2>
            <div id="pendingContainer">Chargement du statut...</div>
        </div>

        <script>
            async function launchAgent() {
                const payload = {
                    agent_id: document.getElementById('agentId').value,
                    instance_id: document.getElementById('instanceId').value,
                    params: {
                        target_repo: document.getElementById('repo').value,
                        issue_id: document.getElementById('issueId').value
                    }
                };
                await fetch('/api/agents/launch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                alert('Tâche transmise au plan de contrôle.');
                document.getElementById('instanceId').value = '';
                loadPending();
            }

            async function loadPending() {
                const res = await fetch('/api/hitl/pending');
                const items = await res.json();
                const container = document.getElementById('pendingContainer');
                container.innerHTML = '';
                
                let count = 0;
                for (const [id, data] of Object.entries(items)) {
                    if (data.status === 'waiting') {
                        count++;
                        container.innerHTML += `
                            <div class="badge-waiting">
                                <strong>Instance : ${id}</strong>
                                <p>${data.message}</p>
                                <input type="text" id="feedback-${id}" placeholder="Directive ou commentaire pour l'agent...">
                                <div class="actions">
                                    <button onclick="resolve('${id}', true)">Approuver & Créer PR</button>
                                    <button class="btn-reject" onclick="resolve('${id}', false)">Rejeter</button>
                                </div>
                            </div>
                        `;
                    }
                }
                if (count === 0) container.innerHTML = '<p>Aucun agent en attente de décision.</p>';
            }

            async function resolve(instanceId, approved) {
                const feedback = document.getElementById(`feedback-${instanceId}`).value;
                await fetch(`/api/hitl/resolve/${instanceId}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ approved, feedback })
                });
                loadPending();
            }

            setInterval(loadPending, 2000);
            loadPending();
        </script>
    </body>
    </html>
    """

```

---

## 5. Manifestes GitOps et Déploiement Déclaratif

L'ensemble des manifestes est structuré dans un dossier synchronisé par **Argo CD**.

### 5.1. Déploiement de Restate Server (`gitops/restate.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: restate
  namespace: agent-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: restate
  template:
    metadata:
      labels:
        app: restate
    spec:
      containers:
        - name: restate
          image: docker.io/restatedev/restate:latest
          ports:
            - name: ingress
              containerPort: 8080
            - name: admin
              containerPort: 9070
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
---
apiVersion: v1
kind: Service
metadata:
  name: restate
  namespace: agent-system
spec:
  selector:
    app: restate
  ports:
    - name: ingress
      port: 8080
      targetPort: 8080
    - name: admin
      port: 9070
      targetPort: 9070

```

### 5.2. ServiceMonitor pour Scrape Prometheus (`gitops/restate-monitor.yaml`)

Permet à la stack Prometheus d'ingérer les métriques natives de charge de Restate :

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: restate-monitor
  namespace: agent-system
  labels:
    release: prom-stack
spec:
  selector:
    matchLabels:
      app: restate
  endpoints:
    - port: admin
      path: /metrics
      interval: 5s

```

### 5.3. Déploiement de l'App Chapeau (`gitops/app-chapeau.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app-chapeau
  namespace: agent-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: app-chapeau
  template:
    metadata:
      labels:
        app: app-chapeau
    spec:
      containers:
        - name: app
          image: my-registry/app-chapeau:v0.1
          env:
            - name: RESTATE_INGRESS_URL
              value: "http://restate.agent-system.svc.cluster.local:8080"
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: app-chapeau
  namespace: agent-system
spec:
  type: LoadBalancer
  selector:
    app: app-chapeau
  ports:
    - port: 8000
      targetPort: 8000

```

### 5.4. Worker Pool et ScaledObject KEDA (`gitops/agent-workers.yaml`)

Les workers démarrent à 1 pod pour assurer une réactivité immédiate et s'étendent automatiquement jusqu'à 5 pods sous la pression des tâches en file d'attente Restate :

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-workers
  namespace: agent-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-workers
  template:
    metadata:
      labels:
        app: agent-workers
    spec:
      containers:
        - name: worker
          image: my-registry/agent-issue-worker:v0.1
          env:
            - name: GATEWAY_URL
              value: "http://app-chapeau.agent-system.svc.cluster.local:8000"
          ports:
            - containerPort: 9080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: keda-agent-workers
  namespace: agent-system
spec:
  scaleTargetRef:
    name: agent-workers
  minReplicaCount: 1
  maxReplicaCount: 5
  cooldownPeriod: 30
  pollingInterval: 5
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prom-stack-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090
        metricName: restate_pending_invocations
        query: sum(restate_service_pending_invocations{service="GitHubIssueResolver"})
        threshold: "2"

```

### 5.5. Manifeste Racine Argo CD (`argocd-root-app.yaml`)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: agent-platform
  namespace: argocd
spec:
  project: default
  source:
    repoURL: 'https://github.com/mon-orga/agent-platform-gitops.git'
    targetRevision: HEAD
    path: gitops
  destination:
    server: 'https://kubernetes.default.svc'
    namespace: agent-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true

```

---

## 6. Cahier de Recette et Déroulé de Démonstration

Ce protocole valide pas à pas la résilience, l'isolation multi-instances et l'hibernation à coût processeur nul.

### Étape 1 : Vérification de la Synchronisation GitOps

Vérifier l'état d'Argo CD et des pods initiaux :

```bash
kubectl get pods -n agent-system

```

**Attendu :** 1 pod `restate`, 1 pod `app-chapeau`, et 1 pod initial `agent-workers` en état `Running`.

### Étape 2 : Lancement Simultané de Deux Instances

Accéder à la console web : `http://localhost:8000`.

1. Renseigner l'Instance 1 :
* ID : `instance-alpha`
* Repo : `mon-orga/backend`
* Issue : `101`
* Cliquer sur **Démarrer le Cycle**.


2. Renseigner immédiatement l'Instance 2 :
* ID : `instance-beta`
* Repo : `mon-orga/frontend`
* Issue : `202`
* Cliquer sur **Démarrer le Cycle**.



**Attendu :** Les deux requêtes sont acquittées instantanément par l'App Chapeau et transmises à Restate.

### Étape 3 : Constat de l'Hibernation (Zéro Consommation CPU)

1. Observer la section **Interactions Humaines Requises (HITL)** du dashboard web. Deux cartes apparaissent avec leurs messages d'analyse distincts.
2. Mesurer la consommation des pods workers :

```bash
kubectl top pods -n agent-system

```

**Attendu :** Les deux agents sont suspendus au niveau de leur promesse `awakeable`. La consommation CPU du pod worker retombe à son strict niveau plancher (`~1-2m CPU`). Aucun thread bloquant ne tourne.

### Étape 4 : Preuve de Tolérance aux Pannes (Crash Recovery)

Simuler une panne d'infrastructure en tuant brutalement le worker pendant l'hibernation :

```bash
kubectl delete pod -l app=agent-workers -n agent-system

```

Kubernetes recrée un nouveau pod en quelques secondes.

### Étape 5 : Réveil Sélectif et Résolution

1. Sur le dashboard web, sous l'instance `instance-alpha`, saisir : *« Approuvé pour merge rapide »*, puis cliquer sur **Approuver & Créer PR**.
2. **Attendu :**
* Seule la carte `instance-alpha` disparaît de l'écran.
* Le nouveau pod worker reprend l'exécution de l'instance Alpha exactement au checkpoint post-suspension, sans ré-exécuter l'analyse de code préalable.
* `instance-beta` demeure intacte en hibernation dans Restate.


3. Résoudre ensuite l'instance Beta en cliquant sur **Rejeter**.

---

## 7. Synthèse des Bénéfices Techniques pour l'Article

* **Concurrence sans surcoût :** Les instances sont des espaces d'état logiques pilotés par les `VirtualObjects` de Restate, éliminant le pattern anti-GitOps d'un Pod par agent.
* **Garantie d'idempotence :** Un crash d'infrastructure ou un redémarrage de pod ne relance jamais deux fois un appel d'outil à effet de bord (création de PR, écriture en base).
* **Sobriété d'infrastructure :** Le couplage KEDA + Prometheus permet à la grappe de workers d'absorber des pics de traitement et de revenir à son dimensionnement minimal dès que les agents se mettent en attente d'arbitrage.

