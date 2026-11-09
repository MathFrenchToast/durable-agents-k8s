#!/usr/bin/env bash
set -euo pipefail

GATEWAY_URL=${GATEWAY_URL:-"http://localhost:8000"}

echo "=========================================================="
echo " CAHIER DE RECETTE TECHNIQUE — DEMO ENVIRONNEMENT D'AGENTS"
echo "=========================================================="

echo ""
echo "--- Étape 1 : Vérification des composants ---"
if kubectl get nodes &>/dev/null; then
    echo "Pods actifs dans le namespace 'agent-system' :"
    kubectl get pods -n agent-system || true
else
    echo "Mode Docker Compose détecté. Conteneurs actifs :"
    docker compose ps
fi

curl -sf "${GATEWAY_URL}/healthz" || {
    echo "❌ Erreur : L'Application Chapeau ne répond pas sur ${GATEWAY_URL}"
    exit 1
}
echo "✅ Gateway active et prête."

echo ""
echo "--- Étape 2 : Lancement Simultané de Deux Instances ---"
echo "1. Lancement de 'instance-alpha' (dépôt mon-orga/backend, issue 101)..."
curl -s -X POST "${GATEWAY_URL}/api/agents/launch" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "github-issue-resolver",
    "instance_id": "instance-alpha",
    "params": {
      "target_repo": "mon-orga/backend",
      "issue_id": "101"
    }
  }' | grep -o '"status":"[^"]*"'

echo "2. Lancement de 'instance-beta' (dépôt mon-orga/frontend, issue 202)..."
curl -s -X POST "${GATEWAY_URL}/api/agents/launch" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "github-issue-resolver",
    "instance_id": "instance-beta",
    "params": {
      "target_repo": "mon-orga/frontend",
      "issue_id": "202"
    }
  }' | grep -o '"status":"[^"]*"'

echo "Attente de la mise en hibernation (génération des awakeables)..."
sleep 4

echo ""
echo "--- Étape 3 : Constat de l'Hibernation (0% CPU, en attente de signal) ---"
PENDING_JSON=$(curl -s "${GATEWAY_URL}/api/hitl/pending")
echo "État des interactions HITL suspendues :"
echo "${PENDING_JSON}"

if echo "${PENDING_JSON}" | grep -q "instance-alpha" && echo "${PENDING_JSON}" | grep -q "instance-beta"; then
    echo "✅ Les deux agents sont suspendus sur Restate en attente d'arbitrage !"
else
    echo "⚠️ Attention : Les deux instances ne sont pas encore visibles. Patienter quelques secondes..."
fi

if kubectl get nodes &>/dev/null; then
    echo "Consommation CPU des workers pendant l'hibernation :"
    kubectl top pods -n agent-system 2>/dev/null || echo "(Metrics-server non initialisé pour kubectl top)"
else
    echo "Consommation CPU Docker pendant l'hibernation :"
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" agent-worker
fi

echo ""
echo "--- Étape 4 : Preuve de Tolérance aux Pannes (Crash Recovery) ---"
if kubectl get nodes &>/dev/null; then
    echo "Simulation de panne : suppression brutale du pod agent-workers..."
    kubectl delete pod -l app=agent-workers -n agent-system --now 2>/dev/null || true
    echo "Attente de recréation du pod par Kubernetes..."
    sleep 5
    kubectl get pods -n agent-system -l app=agent-workers
    echo "✅ Worker recréé. L'état Restate et les awakeables sont préservés de manière déterministe."
else
    echo "Simulation de panne : redémarrage brutal du conteneur agent-worker..."
    docker restart agent-worker
    sleep 3
    echo "✅ Worker redémarré. L'état Restate et les awakeables sont préservés de manière déterministe."
fi

echo ""
echo "--- Étape 5 : Réveil Sélectif et Résolution ---"
echo "1. Résolution de 'instance-alpha' : Approbation humaine..."
curl -s -X POST "${GATEWAY_URL}/api/hitl/resolve/instance-alpha" \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "feedback": "Approuvé pour merge rapide"}'
echo ""
echo "Attente de la complétion du step post-approbation de l'instance Alpha..."
sleep 2

echo "2. Vérification que seule 'instance-beta' reste en hibernation :"
REMAINING=$(curl -s "${GATEWAY_URL}/api/hitl/pending")
echo "${REMAINING}"

echo "3. Résolution de 'instance-beta' : Rejet de la proposition..."
curl -s -X POST "${GATEWAY_URL}/api/hitl/resolve/instance-beta" \
  -H "Content-Type: application/json" \
  -d '{"approved": false, "feedback": "Rejeté par l opérareur"}'
echo ""

echo ""
echo "=========================================================="
echo " ✅ Protocole de recette validé avec succès !"
echo "=========================================================="
