#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo " [2/4] Installation des prérequis socle (ArgoCD, Prometheus, KEDA)"
echo "=========================================================="

# 1. Argo CD
echo "==> Installation d'Argo CD..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

echo "Attente de la disponibilité du serveur Argo CD..."
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s

# 2. Prometheus Stack
echo "==> Installation de la Prometheus Stack (kube-prometheus-stack)..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
helm repo update

helm upgrade --install prom-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false

# 3. KEDA
echo "==> Installation de KEDA..."
helm repo add kedacore https://kedacore.github.io/charts || true
helm repo update

helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace

echo "==> Tous les composants socle sont installés !"
kubectl get pods -A
