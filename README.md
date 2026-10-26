# Télépaiement DGI — API de test (Django REST Framework)

Ce projet est une implémentation **côté STT/DGI** de l'API décrite dans le cahier
des charges « Télépaiement des obligations fiscales ». Il permet de tester,
sans dépendre d'une vraie banque, l'ensemble du protocole :

- OAuth2 `client_credentials` (chapitre II)
- Ajout de compte bancaire (chapitre III)
- Exécution des ordres de paiement (chapitre IV)

## 1. Installation

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo   # crée le client OAuth de démo : client_id=Tresor / client_secret=secret
python manage.py runserver
```

Le serveur écoute par défaut sur `http://127.0.0.1:8000`.

## 2. Vue d'ensemble des endpoints

| Méthode | URL | Rôle |
|---|---|---|
| POST | `/oauth/token` | Obtenir un access token OAuth2 (grant_type=client_credentials, 5 min) |
| POST | `/api/test/accounts/` | **[Aide de test]** simule un contribuable qui ajoute un compte |
| POST | `/api/test/payments/` | **[Aide de test]** simule un contribuable qui confirme un paiement |
| GET | `/api/test/rejection-codes/` | Liste des codes de rejet (Annexe 2) |
| GET | `/notification/ACCOUNT/{transactionId}` | Détails d'une demande d'ajout de compte |
| GET | `/notification/PAYMENT/{transactionId}` | Détails d'un ordre de paiement |
| POST | `/update` | Décision banque / initiation / validation |

> Dans le vrai système, c'est STT qui notifie la banque en `POST`-ant sur
> l'endpoint que la banque expose. Ici, comme cette application **joue le
> rôle de STT**, les endpoints `/api/test/...` remplacent ce déclenchement :
> ils créent la transaction et vous donnent le `transactionId` à utiliser
> pour la suite du scénario (comme si la banque venait de recevoir la
> notification).

### 2.1 Authentification OAuth2 (`client_credentials`)

`POST /oauth/token` implémente le grant type `client_credentials` de la RFC 6749.
Le client peut s'authentifier de deux manières :

- **client_secret_post** : `client_id` / `client_secret` dans le corps `application/x-www-form-urlencoded` (ou JSON), avec `grant_type=client_credentials`.
- **client_secret_basic** : en-tête `Authorization: Basic base64(client_id:client_secret)`, avec `grant_type=client_credentials` dans le corps.

Réponse en cas de succès (200) :

```json
{
  "access_token": "…",
  "token_type": "Bearer",
  "expires_in": 300
}
```

Le token doit ensuite être envoyé en en-tête `Authorization: Bearer <access_token>`
sur les endpoints protégés. En cas d'erreur, la réponse suit le format standard
OAuth2 `{"error": "...", "error_description": "..."}` (`invalid_request`,
`invalid_client`, `unsupported_grant_type`).

## 3. Machine à états

**Compte bancaire** : `PENDING → AUTHORIZED` ou `PENDING → REJECTED`
(une seule décision possible, toute tentative supplémentaire renvoie 409).

**Paiement** : `PENDING → IN_PROGRESS → EXECUTED` ou `IN_PROGRESS → REJECTED`
(l'initiation doit précéder la validation, impossible de valider un paiement
encore `PENDING`).

## 4. Tester avec Postman

Importez `Telepaiement_DGI.postman_collection.json`. La collection contient 4
dossiers (Authentification, Ajout de compte, Paiement, Divers) avec, pour
chaque cas, un script de test qui vérifie le code HTTP et enchaîne
automatiquement les `transactionId` / `token` d'une requête à l'autre (variables
de collection). Il suffit d'exécuter les requêtes dans l'ordre, ou de lancer
tout le dossier via le Collection Runner.

Variables de collection utilisées : `base_url`, `client_id`, `client_secret`,
`token`, `account_transaction_id`, `payment_transaction_id`.

## 5. Administration

Un compte super-utilisateur peut être créé avec `python manage.py createsuperuser`
puis consulté sur `/admin/` pour voir/éditer les clients OAuth et les
transactions directement en base.
"# DGI" 
