# UrbanSanity v13

**Urban Waste Collection Planning Tool — Powered by OpenStreetMap**

> Outil de planification géospatiale pour l'optimisation des points de collecte de déchets urbains en Afrique subsaharienne.

##  Démarrage rapide

```bash
cd urbansanity-v13
WEB_PORT=18700 docker compose up --build
```

Ouvrir : **http://localhost:18700**

## Fonds de carte (CARTO) — clé API

Depuis le 23 septembre 2026, CARTO ajoute un filigrane « API KEY REQUIRED » sur toutes les tuiles appelées sans clé.

- Demander une clé gratuite : https://carto.com/basemaps/apikey/ (non commercial : 5 M tuiles/mois).
- Railway → service → **Variables** → ajouter `CARTO_API_KEY = <votre clé>`.
- Le frontend lit la clé via `GET /api/config`. Sans clé, « CartoDB Positron » et « CartoDB Sombre » basculent automatiquement sur les fonds Esri Canvas équivalents (sans clé, sans filigrane).

Solution proposée par **Geo Wakanda**.
