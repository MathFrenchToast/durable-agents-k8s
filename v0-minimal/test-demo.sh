#!/usr/bin/env bash
set -e

GATEWAY="http://localhost:8000"

echo "==> 1. Lancement de 2 agents simultanés..."
curl -s -X POST "$GATEWAY/api/launch" -H "Content-Type: application/json" \
  -d '{"instance_id": "issue-alpha", "target_repo": "mon-orga/backend", "issue_id": "101"}'
echo ""
curl -s -X POST "$GATEWAY/api/launch" -H "Content-Type: application/json" \
  -d '{"instance_id": "issue-beta", "target_repo": "mon-orga/frontend", "issue_id": "202"}'
echo ""

sleep 3
echo "==> 2. Vérification de l'hibernation HITL (0% CPU) :"
curl -s "$GATEWAY/api/hitl/pending" | grep -o '"issue-[^"]*"' || true
echo ""

echo "==> 3. Consommation CPU des conteneurs :"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" v0-agent-worker || true

echo "==> 4. Approbation humaine de 'issue-alpha' :"
curl -s -X POST "$GATEWAY/api/hitl/resolve/issue-alpha" -H "Content-Type: application/json" \
  -d '{"approved": true, "feedback": "PR validée pour merge"}'
echo ""

sleep 2
echo "==> 5. 'issue-beta' toujours en attente :"
curl -s "$GATEWAY/api/hitl/pending"
echo ""

echo "==> 6. Rejet de 'issue-beta' :"
curl -s -X POST "$GATEWAY/api/hitl/resolve/issue-beta" -H "Content-Type: application/json" \
  -d '{"approved": false, "feedback": "Patch insuffisant"}'
echo ""
echo "✅ Démo v0 terminée avec succès !"
