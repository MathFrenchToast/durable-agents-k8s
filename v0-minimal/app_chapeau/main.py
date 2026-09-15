import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="App Chapeau v0 - Minimal")
RESTATE_INGRESS = os.getenv("RESTATE_INGRESS_URL", "http://restate:8080").rstrip("/")
pending = {}


class LaunchPayload(BaseModel):
    instance_id: str
    target_repo: str = "mon-orga/backend"
    issue_id: str = "42"


@app.post("/api/launch")
async def launch_agent(body: LaunchPayload):
    url = f"{RESTATE_INGRESS}/GitHubIssueResolver/{body.instance_id}/resolve"
    payload = {"target_repo": body.target_repo, "issue_id": body.issue_id}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=3.0)
        except httpx.ReadTimeout:
            pass
    return {"status": "dispatched", "instance_id": body.instance_id}


class NotifyPayload(BaseModel):
    instance_id: str
    token: str
    message: str


@app.post("/api/hitl/notify")
def receive_notification(body: NotifyPayload):
    pending[body.instance_id] = {"token": body.token, "message": body.message, "status": "waiting"}
    return {"status": "stored"}


@app.get("/api/hitl/pending")
def list_pending():
    return {k: v for k, v in pending.items() if v["status"] == "waiting"}


class ResolvePayload(BaseModel):
    approved: bool
    feedback: str = ""


@app.post("/api/hitl/resolve/{instance_id}")
async def resolve_interaction(instance_id: str, body: ResolvePayload):
    item = pending.get(instance_id)
    if not item or item["status"] != "waiting":
        raise HTTPException(status_code=404, detail="Aucun signal attendu pour cette instance")

    url = f"{RESTATE_INGRESS}/restate/awakeables/{item['token']}/resolve"
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json={"approved": body.approved, "feedback": body.feedback})
        if res.status_code >= 400:
            raise HTTPException(status_code=500, detail="Échec du réveil Restate")

    item["status"] = "resolved"
    return {"status": "success", "instance_id": instance_id}


@app.get("/healthz")
def health():
    return {"status": "healthy", "version": "v0-minimal"}


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8"><title>Agent Console v0 (Minimal)</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #1e293b; background: #f8fafc; }
        .card { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        h1 { font-size: 22px; margin-bottom: 20px; }
        label { display: block; font-weight: 600; font-size: 13px; margin: 10px 0 4px; }
        input { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 4px; }
        button { background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: 600; margin-top: 12px; }
        .btn-reject { background: #dc2626; margin-left: 8px; }
        .hitl-box { background: #fef3c7; border: 1px solid #fde68a; border-left: 4px solid #f59e0b; padding: 12px; border-radius: 4px; margin-bottom: 12px; }
    </style>
</head>
<body>
    <h1>🤖 Plateforme Agentique Durable — Console v0 Minimal</h1>
    <div class="card">
        <h3>Lancer une instance</h3>
        <label>ID d'Instance :</label><input id="inst" value="issue-101">
        <label>Dépôt Git :</label><input id="repo" value="mon-orga/backend">
        <label>Numéro d'Issue :</label><input id="issue" value="42">
        <button onclick="launch()">Démarrer le Cycle</button>
    </div>
    <div class="card">
        <h3>Arbitrage Humain Requis (0% CPU - En attente de signal)</h3>
        <div id="pendingList">Chargement...</div>
    </div>
    <script>
        async function launch() {
            await fetch('/api/launch', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ instance_id: document.getElementById('inst').value, target_repo: document.getElementById('repo').value, issue_id: document.getElementById('issue').value })
            });
            document.getElementById('inst').value = 'issue-' + Math.floor(Math.random()*900+100);
            poll();
        }
        async function poll() {
            const res = await fetch('/api/hitl/pending');
            const data = await res.json();
            const div = document.getElementById('pendingList');
            const keys = Object.keys(data);
            if (keys.length === 0) { div.innerHTML = '<em>Aucun agent suspendu.</em>'; return; }
            div.innerHTML = keys.map(k => `
                <div class="hitl-box">
                    <strong>Instance : ${k}</strong>
                    <p style="margin: 6px 0; font-size: 13px;">${data[k].message}</p>
                    <input id="fb-${k}" placeholder="Commentaire optionnel...">
                    <div>
                        <button onclick="resolve('${k}', true)">✓ Approuver & Créer PR</button>
                        <button class="btn-reject" onclick="resolve('${k}', false)">✗ Rejeter</button>
                    </div>
                </div>
            `).join('');
        }
        async function resolve(id, approved) {
            const fb = document.getElementById('fb-' + id).value;
            await fetch('/api/hitl/resolve/' + id, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ approved, feedback: fb })
            });
            poll();
        }
        setInterval(poll, 2000); poll();
    </script>
</body>
</html>"""
