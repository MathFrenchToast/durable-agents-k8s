# Documentation Utilisateur — Console Control Plane

Cette interface web pilote le cycle de vie des agents OpenCode v2 déployés sur le cluster Kubernetes (K3s + Longhorn). Elle regroupe à la fois les fonctions d'une **plateforme agentique** (déclaration d'agents, envoi d'événements/prompts, validation humaine HITL, consultation des fichiers et sessions) et des commandes de **manipulation de l'infrastructure du lab** (arrêt d'un nœud K3s, migration d'un Pod entre VMs, pause de l'hyperviseur).

---

## 1. Barre supérieure (Pilotage du Lab & Documentation)

| Élément / Bouton | Catégorie | Action réalisée |
| :--- | :--- | :--- |
| **`🖥️ agentlab-server` / `agentlab-worker`** | Infra Lab | Indique l'état KVM/libvirt (`running` ou `shut off`) des deux machines virtuelles K3s et la RAM allouée sur l'hôte (`8 Go` quand les 2 VMs tournent). |
| **`📖 Architecture & Article`** | Documentation | Ouvre cette fenêtre de documentation (Overview, Doc Utilisateur, Doc Technique, Article complet). |
| **`⏹️ Stop Nœud Worker (-4 Go)` / `▶️ Start Nœud Worker`** | Infra Lab (Chaos) | Éteint ou rallume la VM `agentlab-worker` pour simuler la perte d'un nœud Kubernetes ou libérer 4 Go de RAM sur la machine hôte. |
| **`🌙 Pause Lab` / `☀️ Réveiller VMs`** | Infra Lab | Arrête proprement ou redémarre les deux VMs du lab (`./pause.sh` / `./resume.sh`) pour tomber à 0 % CPU/RAM sur l'hôte. |

---

## 2. Colonne de gauche (Liste des Agents & Instanciation)

| Élément / Bouton | Catégorie | Action réalisée |
| :--- | :--- | :--- |
| **`+ Patron YAML`** | Plateforme Agentique | Ouvre l'éditeur déclaratif (`kind: DurableAgent`) permettant de choisir un patron d'agent (*boilerplate*) ou d'écrire un nouveau YAML et de le déployer dans Kubernetes (`POST /api/agents/apply`). |
| **Filtres (`Tous`, `Work`, `HITL`, `Sleep`)** | Plateforme Agentique | Filtre la liste des agents selon leur état courant (`WORKING`, `WAITING_HITL`, `HIBERNATED`). |
| **Carte d'un agent** | Plateforme Agentique | Sélectionne l'agent pour afficher son flux d'activité, ses fichiers et ses sessions SQLite. Affiche en résumé son état, le nœud K3s où il tourne (ou `0/1 hiberné`) et le compte à rebours avant auto-hibernation. |

---

## 3. En-tête de l'agent sélectionné (Actions de cycle de vie & Tests K8s)

| Bouton / Contrôle | Catégorie | Action réalisée |
| :--- | :--- | :--- |
| **`🌐 Aperçu Site Web ↗`** *(si fichier `.html` présent)* | Plateforme Agentique | Ouvre dans un nouvel onglet le rendu web des fichiers HTML générés par l'agent dans `/data/workspace` (fonctionne même quand l'agent est en hibernation `0/1` grâce au Snapshot Control Plane). |
| **`⚡ Wake (0 → 1)`** | Infra / Forçage | Réveille manuellement le Pod de l'agent (`replicas: 1`) et monte son volume Longhorn sans lui envoyer de tâche. *(Optionnel : envoyer un prompt réveille déjà l'agent automatiquement).* |
| **`💤 Hibernate (1 → 0)`** | Infra / Forçage | Prend immédiatement un snapshot de `/data/workspace` et `/data/opencode.db`, puis éteint le Pod (`replicas: 0`, 0 Mo RAM). |
| **`🔀 Migrate Node (A → B)`** | Infra Lab (Chaos) | Force le déplacement à chaud du Pod de l'agent vers l'autre VM K3s (`cordon` + `delete pod` + `rollout`) pour démontrer que le volume Longhorn et la session SQLite suivent le Pod d'un nœud à l'autre. |
| **`📜 Logs K8s / PVC`** | Infra / Diagnostic | Affiche les événements Kubernetes du namespace `agent-lab`, le descriptif du `PVC` Longhorn et les logs du conteneur. |
| **`🔌 API & Événements`** | Plateforme Agentique | Ouvre la documentation technique et le simulateur d'événements API (`POST /api/agents/{name}/message`, `apply`, `stop`, `DELETE`). |
| **`📄 Patron YAML / KEDA`** | Plateforme Agentique | Affiche le manifeste `kind: DurableAgent` de l'agent ainsi que les manifestes Kubernetes (`Deployment`, `PVC`, `ConfigMap`, `ScaledObject` KEDA) générés par le Control Plane. |
| **`🗑️`** | Plateforme Agentique | Supprime l'agent, son `Deployment`, son `ConfigMap`, son `PVC` Longhorn et son cache local (`DELETE /api/agents/{name}`). |
| **`⏱️ Auto-Hibernate` & sélecteur (`60s`–`300s`)** | Plateforme Agentique | Active/désactive l'hibernation automatique après inactivité (`idleTimeoutSeconds`) et affiche le compte à rebours (`Scale 1→0 dans Xs`). |

---

## 4. Colonne centrale (Flux temps réel, HITL & Envoi de tâches)

| Élément / Bouton | Catégorie | Action réalisée |
| :--- | :--- | :--- |
| **Barre des 4 états (`1. HIBERNATED`, `2. WORKING`, `3. WAITING HITL`, `4. IDLE`)** | Plateforme Agentique | Indique visuellement dans quelle étape du cycle de vie se trouve l'agent sélectionné. |
| **`⏹️ Stopper la tâche`** *(visible pendant `WORKING`)* | Plateforme Agentique | Interrompt immédiatement le processus `opencode run` en cours (`POST /api/agents/{name}/stop`). |
| **Encadré jaune `🛡️ Arbitrage Humain Requis (HITL)`** | Plateforme Agentique | Apparaît lorsque l'agent attend une validation humaine (pendant que son Pod est hiberné à `0/1`). Permet de saisir une consigne, de cocher éventuellement **`🔀 Forcer le réveil sur l'autre nœud K3s`**, puis de cliquer sur **`✓ Approuver & Exécuter`** ou **`✗ Rejeter`**. |
| **Boutons `Exemples` (au-dessus de la zone de texte)** | Démo | Pré-remplit la zone de prompt avec un cas d'usage type (création de site web DevOps, module TypeScript avec tests, ou scénario HITL). |
| **Sélecteur de mode (`⚡ Mode Direct` / `🛡️ Mode HITL`)** | Plateforme Agentique | Choisit entre une exécution directe (`--agent build`) ou une exécution en 2 phases avec validation humaine (`--agent plan` → hibernation `0/1` → approbation → `--agent build`). |
| **`🚀 Réveiller (0→1) & Envoyer` / `🚀 Envoyer à l'agent`** | Plateforme Agentique | Envoie la tâche à l'agent (`POST /api/agents/{name}/message`). Si l'agent est en hibernation (`0/1`), le Control Plane réveille d'abord le Pod, monte le volume Longhorn et stream la sortie NDJSON en direct. |

---

## 5. Colonne de droite (Snapshot `/data/workspace` & Sessions SQLite)

| Élément / Bouton | Catégorie | Action réalisée |
| :--- | :--- | :--- |
| **`⬇️ .zip`** | Plateforme Agentique | Télécharge une archive `.zip` contenant tous les fichiers générés dans `/data/workspace` ainsi que l'export JSON/Markdown des sessions SQLite. |
| **`🗑️` (en haut du workspace)** | Plateforme Agentique | Vide le répertoire `/data/workspace` et réinitialise la base SQLite `/data/opencode.db` de l'agent pour repartir à zéro. |
| **Nom d'un fichier (`📄 index.html`, etc.)** | Plateforme Agentique | Affiche le contenu du fichier (lu depuis le Pod s'il est actif, ou depuis le Snapshot Control Plane s'il est hiberné à `0/1`). |
| **`🌐 Ouvrir ↗` / `📋` / `⬇️`** | Plateforme Agentique | Prévisualise un fichier `.html` dans le navigateur, copie son code dans le presse-papiers ou le télécharge individuellement. |
| **Carte de session (`🔍 ses_...`)** | Plateforme Agentique | Ouvre la transcription complète de la session enregistrée dans la base SQLite (`/data/opencode.db`) : prompts, réflexions du LLM, outils appelés et nombre de tokens. |
