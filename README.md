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
| POST | `/oauth/token` | Obtenir un access token (5 min) |
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
