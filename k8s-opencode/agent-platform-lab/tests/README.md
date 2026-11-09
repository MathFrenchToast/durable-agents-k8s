# Tests et critères d'acceptation

1. `./tests/wake.sh` : un seul Pod Running ; PVC Bound.
2. `./tests/initialize-session.sh` : réponse attendue, base présente.
3. `./tests/hibernate.sh` : 0 Pod et PVC intact.
4. `./tests/wake.sh` : base et historique présents (contrôle `opencode debug paths db`).
5. `./tests/migrate.sh` : arrêt propre A -> B et réutilisation de la base.
6. Crash en plein appel LLM/MCP : **test manuel non automatisé**, vérifier reprise et doublons.
7. Panne VM worker : **test manuel distinct**, contrôler délais et état Longhorn ; ne jamais simuler par arrêt du serveur K3s unique.

Les tests 1-5 valident la plomberie d'infrastructure. Ils **ne prouvent pas** une durable execution transparente de l'agent. Pour tester la session il faut tracer ses identifiants et événements depuis l'API ou le CLI v2.
