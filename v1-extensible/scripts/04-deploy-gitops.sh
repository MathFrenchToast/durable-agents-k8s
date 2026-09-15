#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo " [4/4] Déploiement GitOps de la plateforme agentique"
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Application des manifestes dans l'ordre
echo "==> Création du namespace agent-system..."
kubectl apply -f "${ROOT_DIR}/gitops/namespace.yaml"

echo "==> Déploiement de Restate Server..."
kubectl apply -f "${ROOT_DIR}/gitops/restate.yaml"

echo "==> Déploiement du ServiceMonitor Restate..."
kubectl apply -f "${ROOT_DIR}/gitops/restate-monitor.yaml"

echo "==> Déploiement de l'Application Chapeau..."
kubectl apply -f "${ROOT_DIR}/gitops/app-chapeau.yaml"

echo "==> Déploiement du Pool de Workers et KEDA ScaledObject..."
kubectl apply -f "${ROOT_DIR}/gitops/agent-workers.yaml"

echo "Attente de la disponibilité des pods dans agent-system..."
kubectl rollout status deployment/restate -n agent-system --timeout=120s
kubectl rollout status deployment/app-chapeau -n agent-system --timeout=120s
kubectl rollout status deployment/agent-workers -n agent-system --timeout=120s

echo "=========================================================="
echo " ✅ Plateforme déployée avec succès !"
echo " Dashboard disponible sur : http://localhost:8000"
echo " Restate Ingress :          http://localhost:8080"
echo " Restate Admin / Metrics :  http://localhost:9070"
echo "=========================================================="
kubectl get pods -n agent-system
