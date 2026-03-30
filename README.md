# UrbanSanity v13

**Urban Waste Collection Planning Tool — Powered by OpenStreetMap**

> Outil de planification géospatiale pour l'optimisation des points de collecte de déchets urbains en Afrique subsaharienne.

---

## 🚀 Démarrage rapide

```bash
cd urbansanity-v13
WEB_PORT=18700 docker compose up --build
```

Ouvrir : **http://localhost:18700**

---

## ✨ Nouveautés v13

### Frontend — Design professionnel
- Palette Inter + JetBrains Mono · système de design tokens complet
- Panels épurés, ombres multicouches, transitions cubic-bezier
- Info panel avec animation slide-in, toasts glassmorphism
- Couleurs sémantiques cohérentes (bleu/vert/ambre/rouge/violet)

### Backend — Algorithme optimisé

| Feature | v12.8 | v13 |
|---------|-------|-----|
| Analyse adaptative | Bouton manuel | **Automatique** |
| Rééquilibrage spatial | ❌ | **✅ Auto** |
| Détection bacs redondants | ❌ | **✅** |
| Relocalisation vers zones non couvertes | ❌ | **✅** |

#### Logique v13
1. **Analyse fraîche** : sélection multi-critères + couverture
2. **Auto-adaptatif** : si réseau existant > réseau proposé sur R1 → réutilise les bacs existants comme ancres automatiquement (sans bouton)
3. **Rééquilibrage spatial** (nouveau) :
   - Détecte les bacs avec faible couverture unique (redondants)
   - Les relocalise vers les zones à haute demande non couvertes
   - Garantit une distribution spatiale uniforme sur toute l'AOI
   - 3 itérations maximum, s'arrête si tous les bacs contribuent

---

## 📊 Paramètres (inchangés)

| Paramètre | Défaut |
|-----------|--------|
| PPH | 5.0 |
| kg déchets/hab/jour | 0.42 |
| Taille grille | 200m |
| R1 / R2 / R3 | 150 / 300 / 500m |
