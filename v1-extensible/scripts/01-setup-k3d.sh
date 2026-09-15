#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="agent-cluster"

echo "=========================================================="
echo " [1/4] Création du cluster k3d : ${CLUSTER_NAME}"
echo "=========================================================="

if k3d cluster list | grep -q "^${CLUSTER_NAME}"; then
    echo "Le cluster k3d '${CLUSTER_NAME}' existe déjà."
else
    k3d cluster create "${CLUSTER_NAME}" \
      --port "8080:80@loadbalancer" \
      --port "9070:9070@loadbalancer" \
      --port "8000:8000@loadbalancer" \
      --agents 2
    echo "Cluster '${CLUSTER_NAME}' créé avec succès."
fi

echo "Basculement du contexte kubectl vers ${CLUSTER_NAME}..."
kubectl cluster-info
echo "Prêt pour l'étape 2 (02-install-prereqs.sh)."
