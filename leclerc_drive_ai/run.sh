#!/usr/bin/env bash
set -e

echo "[INFO] Démarrage de l'Add-on E.Leclerc Drive AI..."

# Création du stockage persistant pour les modèles Ollama dans /data
mkdir -p /data/ollama/models
export OLLAMA_MODELS=/data/ollama/models
export OLLAMA_HOST=127.0.0.1:11434

# Démarrage du serveur Ollama intégré en tâche de fond
echo "[INFO] Lancement du serveur Ollama intégré (port 11434)..."
ollama serve > /data/ollama.log 2>&1 &
OLLAMA_PID=$!

# Attente de disponibilité du serveur Ollama (jusqu'à 20s)
for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        echo "[INFO] Serveur Ollama prêt !"
        break
    fi
    sleep 1
done

# Téléchargement automatique en arrière-plan du modèle qwen2.5:1.5b si absent
(
    if ! ollama list 2>/dev/null | grep -q "qwen2.5:1.5b"; then
        echo "[INFO] Modèle qwen2.5:1.5b non présent dans /data/ollama. Téléchargement initial en cours..."
        ollama pull qwen2.5:1.5b >/dev/null 2>&1 || echo "[WARN] Échec du téléchargement initial qwen2.5:1.5b (recommencera au prochain test)"
        echo "[INFO] Modèle qwen2.5:1.5b prêt !"
    else
        echo "[INFO] Modèle qwen2.5:1.5b déjà présent dans le stockage persistant."
    fi
) &

# Lancement de l'application FastAPI principale
exec python3 -u app.py
