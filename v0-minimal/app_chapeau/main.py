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
    agent_type: str = "github"  # "github" ou "meeting"
    target_repo: str = "mon-orga/backend"
    issue_id: str = "42"
    topic: str = "Point architecture V0"
    participants: str = "alice@corp.com, bob@corp.com"


@app.post("/api/launch")
async def launch_agent(body: LaunchPayload):
    if body.agent_type == "meeting":
        service = "MeetingScheduler"
        handler = "schedule"
        payload = {"topic": body.topic, "participants": body.participants}
    else:
        service = "GitHubIssueResolver"
        handler = "resolve"
        payload = {"target_repo": body.target_repo, "issue_id": body.issue_id}

    url = f"{RESTATE_INGRESS}/{service}/{body.instance_id}/{handler}"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=3.0)
        except httpx.ReadTimeout:
            pass
    return {
        "status": "dispatched",
        "service": service,
        "handler": handler,
        "instance_id": body.instance_id,
    }


class NotifyPayload(BaseModel):
    instance_id: str
    token: str
    message: str
    agent_type: str = "GitHubIssueResolver"


@app.post("/api/hitl/notify")
def receive_notification(body: NotifyPayload):
    pending[body.instance_id] = {
        "token": body.token,
        "message": body.message,
        "agent_type": body.agent_type,
        "status": "waiting",
    }
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
    <meta charset="UTF-8"><title>Agent Console v0 (Multi-Agents)</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 860px; margin: 30px auto; padding: 0 20px; color: #1e293b; background: #f8fafc; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
        .card { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        h1 { font-size: 22px; margin-bottom: 8px; }
        .subtitle { color: #64748b; font-size: 14px; margin-bottom: 24px; }
        .badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 9999px; text-transform: uppercase; }
        .badge-github { background: #dbeafe; color: #1d4ed8; }
        .badge-meeting { background: #fce7f3; color: #be185d; }
        label { display: block; font-weight: 600; font-size: 12px; margin: 8px 0 3px; color: #475569; }
        input { width: 100%; padding: 7px; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 13px; }
        button { background: #2563eb; color: white; border: none; padding: 8px 14px; border-radius: 4px; cursor: pointer; font-weight: 600; margin-top: 10px; font-size: 13px; }
        .btn-schedule { background: #be185d; }
        .btn-reject { background: #dc2626; margin-left: 6px; }
        .hitl-box { background: #fef3c7; border: 1px solid #fde68a; border-left: 4px solid #f59e0b; padding: 12px; border-radius: 4px; margin-bottom: 12px; }
    </style>
</head>
<body>
    <h1>🤖 Plateforme Agentique Durable — Console v0 Multi-Agents</h1>
    <div class="subtitle">Worker unique hébergeant 2 agents distincts (<code>GitHubIssueResolver</code> et <code>MeetingScheduler</code>) avec chacun leurs instances indépendantes.</div>

    <div class="grid">
        <!-- Agent 1 : GitHub Resolver -->
        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0;">Agent 1 : Résolveur GitHub</h3>
                <span class="badge badge-github">POST /resolve</span>
            </div>
            <label>ID d'Instance :</label><input id="gh_inst" value="issue-101">
            <label>Dépôt Git :</label><input id="gh_repo" value="mon-orga/backend">
            <label>Numéro d'Issue :</label><input id="gh_issue" value="42">
            <button onclick="launchGitHub()">🚀 Lancer résolution issue</button>
        </div>

        <!-- Agent 2 : Meeting Scheduler -->
        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0;">Agent 2 : Planificateur Réunion</h3>
                <span class="badge badge-meeting">POST /schedule</span>
            </div>
            <label>ID d'Instance :</label><input id="meet_inst" value="meet-201">
            <label>Sujet de Réunion :</label><input id="meet_topic" value="Revue Architecture K8s">
            <label>Participants :</label><input id="meet_parts" value="dev@orga.com, lead@orga.com">
            <button class="btn-schedule" onclick="launchMeeting()">📅 Lancer planification</button>
        </div>
    </div>

    <div class="card">
        <h3>Arbitrage Humain Requis (0% CPU - En attente de signal HITL)</h3>
        <div id="pendingList">Chargement...</div>
    </div>

    <script>
        async function launchGitHub() {
            const inst = document.getElementById('gh_inst').value;
            await fetch('/api/launch', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    agent_type: 'github',
                    instance_id: inst,
                    target_repo: document.getElementById('gh_repo').value,
                    issue_id: document.getElementById('gh_issue').value
                })
            });
            document.getElementById('gh_inst').value = 'issue-' + Math.floor(Math.random()*900+100);
            poll();
        }

        async function launchMeeting() {
            const inst = document.getElementById('meet_inst').value;
            await fetch('/api/launch', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    agent_type: 'meeting',
                    instance_id: inst,
                    topic: document.getElementById('meet_topic').value,
                    participants: document.getElementById('meet_parts').value
                })
            });
            document.getElementById('meet_inst').value = 'meet-' + Math.floor(Math.random()*900+100);
            poll();
        }

        async function poll() {
            const res = await fetch('/api/hitl/pending');
            const data = await res.json();
            const div = document.getElementById('pendingList');
            const keys = Object.keys(data);
            if (keys.length === 0) { div.innerHTML = '<em>Aucun agent suspendu. Tous les agents sont inactifs ou terminés.</em>'; return; }
            div.innerHTML = keys.map(k => {
                const item = data[k];
                const isMeeting = item.agent_type === 'MeetingScheduler';
                const approveLabel = isMeeting ? '✓ Confirmer Réunion' : '✓ Approuver & Créer PR';
                const rejectLabel = isMeeting ? '✗ Annuler Réunion' : '✗ Rejeter';
                const badgeClass = isMeeting ? 'badge-meeting' : 'badge-github';
                return `
                    <div class="hitl-box">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <strong>Instance : ${k}</strong>
                            <span class="badge ${badgeClass}">${item.agent_type || 'Agent'}</span>
                        </div>
                        <p style="margin: 8px 0; font-size: 13px;">${item.message}</p>
                        <input id="fb-${k}" placeholder="Commentaire optionnel...">
                        <div style="margin-top: 8px;">
                            <button onclick="resolve('${k}', true)">${approveLabel}</button>
                            <button class="btn-reject" onclick="resolve('${k}', false)">${rejectLabel}</button>
                        </div>
                    </div>
                `;
            }).join('');
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
