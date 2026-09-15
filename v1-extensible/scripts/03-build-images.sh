#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="agent-cluster"
APP_IMAGE="agent-platform/app-chapeau:v0.1"
WORKER_IMAGE="agent-platform/agent-worker:v0.1"

echo "=========================================================="
echo " [3/4] Construction et importation des images conteneurs"
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Construction de l'image App Chapeau (${APP_IMAGE})..."
docker build -t "${APP_IMAGE}" -f "${ROOT_DIR}/gateway/Dockerfile" "${ROOT_DIR}"

echo "==> Construction de l'image Agent Worker (${WORKER_IMAGE})..."
docker build -t "${WORKER_IMAGE}" -f "${ROOT_DIR}/runtimes/restate/Dockerfile" "${ROOT_DIR}"

echo "==> Importation des images dans le cluster k3d '${CLUSTER_NAME}'..."
k3d image import "${APP_IMAGE}" "${WORKER_IMAGE}" -c "${CLUSTER_NAME}"

echo "==> Images construites et importées avec succès !"
