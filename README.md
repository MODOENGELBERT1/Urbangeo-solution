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

## Contrôle d'accès (inscription → vérification e-mail → validation admin)

Pages : `/login` (connexion, demande d'accès, mot de passe oublié) · `/admin` (validation des demandes, réservé à l'administrateur).
Tout le reste de l'application et de l'API exige une session valide.

Variables Railway :

| Variable | Rôle |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (service PostgreSQL Railway) — **obligatoire en production** |
| `SECRET_KEY` | longue chaîne aléatoire (signature des sessions et des liens) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | compte administrateur (créé/mis à jour au démarrage) |
| `BREVO_API_KEY` | clé API Brevo pour l'envoi des e-mails |
| `MAIL_SENDER_EMAIL` | expéditeur vérifié dans Brevo |
| `MAIL_SENDER_NAME` | (optionnel) nom affiché de l'expéditeur |
| `APP_URL` | (optionnel) URL publique utilisée dans les liens des e-mails |
| `AUTH_ENABLED` | (optionnel) `false` pour désactiver temporairement le contrôle d'accès |

Sans `BREVO_API_KEY`, aucun e-mail n'est envoyé : les liens apparaissent dans les logs Railway et l'administrateur peut valider directement depuis `/admin`.

Note : en mode `docker compose` (nginx), les pages `/login` et `/admin` ne sont pas routées vers l'API ; le contrôle d'accès complet est prévu pour le déploiement mono-conteneur (Railway).
