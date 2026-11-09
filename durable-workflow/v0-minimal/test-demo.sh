#!/usr/bin/env bash
set -e

GATEWAY="http://localhost:8000"

echo "=================================================================="
echo "🤖 Démarrage du Test Multi-Agents v0 (Worker Commun Restate)"
echo "=================================================================="

echo ""
echo "==> 1. Lancement de 2 instances du 1er agent (GitHubIssueResolver - méthode 'resolve') :"
curl -s -X POST "$GATEWAY/api/launch" -H "Content-Type: application/json" \
  -d '{"agent_type": "github", "instance_id": "issue-alpha", "target_repo": "mon-orga/backend", "issue_id": "101"}' | jq . || true
echo ""
curl -s -X POST "$GATEWAY/api/launch" -H "Content-Type: application/json" \
  -d '{"agent_type": "github", "instance_id": "issue-beta", "target_repo": "mon-orga/frontend", "issue_id": "202"}' | jq . || true
echo ""

echo "==> 2. Lancement d'une instance du 2nd agent (MeetingScheduler - méthode propre 'schedule') :"
curl -s -X POST "$GATEWAY/api/launch" -H "Content-Type: application/json" \
  -d '{"agent_type": "meeting", "instance_id": "meet-standup", "topic": "Architecture Sync K8s", "participants": "devs@orga.com"}' | jq . || true
echo ""

sleep 4
echo "==> 3. Vérification des 3 agents en hibernation HITL simultanée (0% CPU) :"
curl -s "$GATEWAY/api/hitl/pending" | jq . || curl -s "$GATEWAY/api/hitl/pending"
echo ""

echo "==> 4. Consommation CPU du worker partagé hébergeant les 3 instances :"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" v0-agent-worker || true
echo ""

echo "==> 5. Arbitrage HITL 1 : Approbation de 'issue-alpha' (création PR) :"
curl -s -X POST "$GATEWAY/api/hitl/resolve/issue-alpha" -H "Content-Type: application/json" \
  -d '{"approved": true, "feedback": "PR validée par Tech Lead"}' | jq . || true
echo ""

echo "==> 6. Arbitrage HITL 2 : Confirmation de la réunion 'meet-standup' :"
curl -s -X POST "$GATEWAY/api/hitl/resolve/meet-standup" -H "Content-Type: application/json" \
  -d '{"approved": true, "feedback": "Créneau validé dans Google Calendar"}' | jq . || true
echo ""

sleep 2
echo "==> 7. 'issue-beta' est toujours en attente d'arbitrage sans consommer de ressource :"
curl -s "$GATEWAY/api/hitl/pending" | jq . || curl -s "$GATEWAY/api/hitl/pending"
echo ""

echo "==> 8. Arbitrage HITL 3 : Rejet de 'issue-beta' :"
curl -s -X POST "$GATEWAY/api/hitl/resolve/issue-beta" -H "Content-Type: application/json" \
  -d '{"approved": false, "feedback": "Patch insuffisant"}' | jq . || true
echo ""

sleep 1
echo "==> 9. État final (aucune instance en attente) :"
curl -s "$GATEWAY/api/hitl/pending" | jq . || curl -s "$GATEWAY/api/hitl/pending"
echo ""
echo "✅ Démo v0 Multi-Agents & Multi-Instances terminée avec succès !"
