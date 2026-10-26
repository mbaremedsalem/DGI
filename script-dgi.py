#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
process_pending_operations.py  (VERSION CORRIGÉE - anti-doublon)

Cronjob de traitement automatique des opérations STT en attente :
  - Comptes (ACCOUNT) : autorise ou rejette la création de compte.
  - Paiements (PAYMENT) : PENDING -> IN_PROGRESS -> EXECUTED / REJECTED

CORRECTIONS PAR RAPPORT À L'ANCIENNE VERSION
---------------------------------------------
1) ORDRE API PUIS INSERTION (fin des doublons MVTS)
   Avant : insertion MVTS, puis update STT. Si l'update échouait, le
   paiement restait IN_PROGRESS et était réinséré au passage suivant
   avec un nouveau NOOPER  => doublons.
   Maintenant :
     a. on RÉSERVE un NOOPER (enregistré dans un journal local),
     b. on appelle l'API STT -> EXECUTED avec ce NOOPER,
     c. SEULEMENT si l'API répond OK, on insère dans MVTS.
   Si l'API échoue : aucune insertion, et au passage suivant on réessaie
   avec le MÊME NOOPER (jamais un nouveau).

2) CALCUL DU NOOPER SUR MVTS + MVTC
   Les mouvements de MVTS partent dans MVTC en fin de journée. Le NOOPER
   suivant = MAX(dernier DGI de MVTS, dernier DGI de MVTC, dernier NOOPER
   réservé dans le journal) + 1, avec vérification qu'il n'existe ni
   dans MVTS ni dans MVTC.

3) JOURNAL LOCAL D'ÉTAT (idempotence / reprise)
   Fichier JSON (JOURNAL_FILE). États d'un paiement :
     NOOPER_RESERVE            NOOPER choisi, API pas encore confirmée
     STT_EXECUTED              API OK, insertion MVTS à faire
     MVTS_OK                   insertion faite, post-traitement à faire
     POST_TRAITEMENT_EN_COURS  SSH/email en cours
     TERMINE                   tout est fait
     TERMINE_SSH_KO            MVTS OK mais procédures SSH en échec (manuel)
     A_VERIFIER                plantage pendant le SSH (manuel)
     ABANDONNE                 paiement finalement rejeté, NOOPER libéré
   Une PASSE DE REPRISE en début d'exécution termine les paiements
   restés à mi-chemin, sans jamais réinsérer.

4) GARDE-FOU EN BASE : avant chaque insertion, si le NOOPER existe déjà
   dans MVTS ou MVTC, on n'insère pas.

--- FLUX PAIEMENTS ---
  PASSE 0 - Reprise : termine les paiements inachevés du journal.
  PASSE 1 - PENDING -> IN_PROGRESS.
  PASSE 2 - IN_PROGRESS -> vérif IBAN -> REJECTED
                                      ou réservation NOOPER
                                         -> API EXECUTED
                                         -> insertion MVTS
                                         -> SSH (mvtd + solde dispo)
                                         -> email

Règles de décision (/users/cpt-by-iban/) :
  - data_count == 0 ou data == null   -> REJETÉ "001" (compte inexistant)
  - data.DATFRM renseigné             -> REJETÉ "002" (compte clôturé)
  - data.CODDCI == "DI"               -> REJETÉ "003" (opposition)
  - sinon                             -> éligible
"""

import fcntl
import json
import logging
import os
import smtplib
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText

import requests

try:
    import oracledb
except ImportError:
    oracledb = None

try:
    import paramiko
except ImportError:
    paramiko = None


# --------------------------------------------------------------------------
# CONFIGURATION - APIs
# --------------------------------------------------------------------------

STT_BASE_URL = "http://192.168.10.15:7050"
CPT_BASE_URL = "http://192.168.10.15:8084"

ACCOUNTS_LIST_URL = f"{STT_BASE_URL}/api/test/accounts/"
PAYMENTS_LIST_URL = f"{STT_BASE_URL}/api/test/payments/"
TOKEN_URL = f"{STT_BASE_URL}/stt/token"
UPDATE_URL = f"{STT_BASE_URL}/stt/updatestt/"
CPT_BY_IBAN_URL = f"{CPT_BASE_URL}/users/cpt-by-iban/"


def details_url(operation_type, transaction_id):
    return f"{STT_BASE_URL}/stt/notificationstt/{operation_type}/{transaction_id}"


AUTH_USERNAME = os.environ.get("STT_USERNAME", "10016")
AUTH_PASSWORD = os.environ.get("STT_PASSWORD", "pass1234@")

REQUEST_TIMEOUT = 30  # secondes

REJECTION_CODE_INVALID_ACCOUNT = "001"
REJECTION_CODE_CLOSED_ACCOUNT = "002"
REJECTION_CODE_OPPOSITION = "003"

STATUS_PENDING = "PENDING"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_EXECUTED = "EXECUTED"
STATUS_REJECTED = "REJECTED"


# --------------------------------------------------------------------------
# CONFIGURATION - Base comptable Oracle
# --------------------------------------------------------------------------

DB_DSN = os.environ.get("DB_DSN", "172.16.3.1:1521/AUBV4")
DB_USER = os.environ.get("DB_USER", "aub")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "MolFersac2023$")

INTERMEDIARY_ACCOUNT = "03260140009"
MOVEMENT_CODE = "91000"
CURRENCY = "MRU"
FIXED_EXPL_VALUE = "AU"
PAYMENT_LABEL_PREFIX = "PAIEMENT QUITANCE DGI"

NOOPER_PREFIX = "DGI"
NOOPER_MIN_DIGITS = 4


# --------------------------------------------------------------------------
# CONFIGURATION - SSH post-traitement
# --------------------------------------------------------------------------

SSH_HOST = os.environ.get("SSH_HOST", "172.16.3.1")
SSH_USER = os.environ.get("SSH_USER", "aub")
SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "Megrap2024!")
SSH_TIMEOUT = 30

SSH_POST_PROCESS_COMMAND_TEMPLATES = [
    "cd $HOME && sql @pmvtsopmvtd.sql {nooper}",   # Ajout dans mvtd
    "cd $HOME && sql @sldisp {nooper}",            # Maj solde disponible
]


# --------------------------------------------------------------------------
# CONFIGURATION - Email
# --------------------------------------------------------------------------

EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "true").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "aubnet@aub.mr")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "cpyq rnue suib ibyt")

EMAIL_RECIPIENTS = [
    "mmeimouna@aub.mr",
    "athiam@aub.mr",
    "sznagui@aub.mr",
    "mleila@aub.mr",
    "bngaide@aub.mr",
    "mbare@aub.mr",
    "dsidahmed@aub.mr",
    "sberrou@aub.mr",
]


# --------------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------------

LOG_DIR = os.environ.get("LOG_DIR", "/appli/bac-cheque/logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "process_pending_operations.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# VERROU ANTI-CHEVAUCHEMENT DE CRON
# --------------------------------------------------------------------------

LOCK_FILE_PATH = os.environ.get("LOCK_FILE_PATH", "/appli/bac-cheque/process_pending_operations.lock")


class AlreadyRunningError(Exception):
    pass


def acquire_lock():
    lock_fd = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        raise AlreadyRunningError("Une exécution précédente est déjà en cours.")
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


# --------------------------------------------------------------------------
# JOURNAL LOCAL D'ÉTAT DES PAIEMENTS
# --------------------------------------------------------------------------

JOURNAL_FILE = os.environ.get("JOURNAL_FILE", "/appli/bac-cheque/data/payments_journal.json")

J_RESERVE = "NOOPER_RESERVE"
J_STT_OK = "STT_EXECUTED"
J_MVTS_OK = "MVTS_OK"
J_POST = "POST_TRAITEMENT_EN_COURS"
J_TERMINE = "TERMINE"
J_SSH_KO = "TERMINE_SSH_KO"
J_A_VERIFIER = "A_VERIFIER"
J_ABANDONNE = "ABANDONNE"

# États pour lesquels l'API STT a déjà accepté EXECUTED : on ne doit
# JAMAIS repasser par une nouvelle réservation / insertion.
J_STATES_DEJA_EXECUTES = {J_STT_OK, J_MVTS_OK, J_POST, J_TERMINE, J_SSH_KO, J_A_VERIFIER}


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_nooper_seq(nooper):
    """'DGI0012' -> 12 ; None si format non reconnu."""
    if not nooper:
        return None
    nooper = str(nooper).strip()
    if nooper.startswith(NOOPER_PREFIX) and nooper[len(NOOPER_PREFIX):].isdigit():
        return int(nooper[len(NOOPER_PREFIX):])
    return None


def format_nooper(seq):
    return NOOPER_PREFIX + str(int(seq)).zfill(NOOPER_MIN_DIGITS)


class PaymentJournal:
    """
    Journal persistant (JSON) : transactionId -> {state, nooper, amount, iban, ...}
    Écriture atomique (fichier temporaire + os.replace) pour ne jamais
    corrompre le fichier en cas de plantage.
    """

    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        # Si le fichier est corrompu on lève une exception : on préfère
        # arrêter les paiements plutôt que de risquer des doublons.
        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return json.loads(content) if content else {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def get(self, transaction_id):
        return self.data.get(str(transaction_id))

    def set(self, transaction_id, **fields):
        entry = self.data.setdefault(str(transaction_id), {})
        entry.setdefault("created_at", _now_str())
        entry.update(fields)
        entry["updated_at"] = _now_str()
        self.save()
        return entry

    def entries_in_states(self, states):
        return [(tid, e) for tid, e in list(self.data.items()) if e.get("state") in states]

    def noopers_in_use(self):
        """NOOPER réservés/utilisés (hors paiements abandonnés)."""
        return {
            e.get("nooper") for e in self.data.values()
            if e.get("nooper") and e.get("state") != J_ABANDONNE
        }

    def max_reserved_seq(self):
        seqs = [parse_nooper_seq(n) for n in self.noopers_in_use()]
        seqs = [s for s in seqs if s is not None]
        return max(seqs) if seqs else 0


# --------------------------------------------------------------------------
# AUTHENTIFICATION
# --------------------------------------------------------------------------

def get_access_token():
    logger.info("Récupération du token d'authentification: %s", TOKEN_URL)
    payload = {"username": AUTH_USERNAME, "password": AUTH_PASSWORD}
    resp = requests.post(TOKEN_URL, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("Aucun access_token retourné par l'API d'authentification")
    logger.info("Token récupéré avec succès (expire dans %ss)", token_data.get("expires_in"))
    return access_token


# --------------------------------------------------------------------------
# VÉRIFICATION IBAN
# --------------------------------------------------------------------------

def check_iban(iban):
    resp = requests.post(CPT_BY_IBAN_URL, json={"iban": iban}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def evaluate_iban_eligibility(cpt_response):
    data_count = cpt_response.get("data_count")
    data = cpt_response.get("data")

    if not data_count or data is None:
        return False, REJECTION_CODE_INVALID_ACCOUNT
    if data.get("DATFRM"):
        return False, REJECTION_CODE_CLOSED_ACCOUNT
    if data.get("CODDCI") == "DI":
        return False, REJECTION_CODE_OPPOSITION
    return True, None


def extract_local_account_number(iban):
    """
    IBAN mauritanien : MR + clé(2) + banque(5) + agence(5) + compte(11) + clé(2).
    Retourne le n° de compte local (11 chiffres).
    """
    if not iban or len(iban) < 25:
        raise ValueError(f"IBAN invalide ou trop court pour en extraire le compte local: {iban}")
    return iban[14:25]


def parse_amount(value):
    """Convertit le montant en Decimal (évite les erreurs d'arrondi float)."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


# --------------------------------------------------------------------------
# MISE À JOUR STT
# --------------------------------------------------------------------------

def update_stt_status(transaction_id, notification_type, status, token,
                      rejection_reason_code=None, transaction_id_bank=None):
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "transactionId": transaction_id,
        "notificationType": notification_type,
        "status": status,
    }
    if status == STATUS_REJECTED and rejection_reason_code:
        body["rejectionReasonCode"] = rejection_reason_code
    if status == STATUS_EXECUTED and transaction_id_bank:
        body["transactionIdBank"] = transaction_id_bank

    resp = requests.post(UPDATE_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _safe_update(notification_type, transaction_id, status, token,
                 rejection_code=None, transaction_id_bank=None):
    """Retourne True si l'API a accepté la mise à jour, False sinon."""
    try:
        result = update_stt_status(transaction_id, notification_type, status, token,
                                   rejection_code, transaction_id_bank)
        logger.info("[%s] Mise à jour envoyée pour %s: %s (bank=%s) -> %s",
                    notification_type, transaction_id, status, transaction_id_bank, result)
        return True
    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            body = (e.response.text or "")[:1000]
        logger.error("[%s] API a refusé la mise à jour %s pour %s (bank=%s) : %s | réponse: %s",
                     notification_type, status, transaction_id, transaction_id_bank, e, body)
        return False
    except Exception:
        logger.exception("[%s] Échec de la mise à jour du statut (%s) pour %s",
                         notification_type, status, transaction_id)
        return False


# ============================================================================
# FLUX COMPTES (ACCOUNT)
# ============================================================================

def get_pending_accounts():
    logger.info("Récupération de la liste des comptes: %s", ACCOUNTS_LIST_URL)
    resp = requests.get(ACCOUNTS_LIST_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    accounts = resp.json()
    pending = [a for a in accounts if a.get("status") == STATUS_PENDING]
    logger.info("Comptes: total=%d, PENDING=%d", len(accounts), len(pending))
    return pending


def get_account_details(transaction_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(details_url("ACCOUNT", transaction_id), headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def process_account(account, token):
    transaction_id = account.get("transactionId")
    logger.info("---- [ACCOUNT] Traitement transactionId=%s ----", transaction_id)

    try:
        details = get_account_details(transaction_id, token)
    except Exception:
        logger.exception("[ACCOUNT] Échec récupération détails pour %s", transaction_id)
        return

    iban = details.get("taxPayerIban")
    logger.info("[ACCOUNT] IBAN=%s | Contribuable=%s", iban, details.get("taxPayerLegalName"))

    if not iban:
        logger.error("[ACCOUNT] Pas d'IBAN pour %s, rejet par sécurité (001)", transaction_id)
        _safe_update("ACCOUNT", transaction_id, STATUS_REJECTED, token, REJECTION_CODE_INVALID_ACCOUNT)
        return

    try:
        cpt_response = check_iban(iban)
    except Exception:
        logger.exception("[ACCOUNT] Échec vérification IBAN %s pour %s", iban, transaction_id)
        return

    logger.info("[ACCOUNT] Réponse cpt-by-iban: %s", cpt_response)
    eligible, rejection_code = evaluate_iban_eligibility(cpt_response)
    status = "AUTHORIZED" if eligible else STATUS_REJECTED
    logger.info("[ACCOUNT] Décision pour %s: %s%s", transaction_id, status,
                f" (code {rejection_code})" if rejection_code else "")

    _safe_update("ACCOUNT", transaction_id, status, token, rejection_code)


def process_all_pending_accounts(token):
    try:
        pending_accounts = get_pending_accounts()
    except Exception:
        logger.exception("[ACCOUNT] Échec récupération liste des comptes.")
        return

    for account in pending_accounts:
        process_account(account, token)


# ============================================================================
# BASE ORACLE : NOOPER + ÉCRITURE MVTS
# ============================================================================

# Dernier NOOPER DGI sur MVTS ET MVTC en une seule requête (même instantané
# de lecture Oracle, donc cohérent même pendant le transfert de fin de journée).
SQL_MAX_NOOPER = """
    SELECT NVL(MAX(seq), 0) FROM (
        SELECT TO_NUMBER(SUBSTR(RTRIM(nooper), 4)) AS seq
          FROM MVTS
         WHERE nooper LIKE 'DGI%'
           AND REGEXP_LIKE(RTRIM(nooper), '^DGI[0-9]+$')
        UNION ALL
        SELECT TO_NUMBER(SUBSTR(RTRIM(nooper), 4)) AS seq
          FROM MVTC
         WHERE nooper LIKE 'DGI%'
           AND REGEXP_LIKE(RTRIM(nooper), '^DGI[0-9]+$')
    )
"""

# Nombre de lignes portant ce NOOPER dans MVTS + MVTC
SQL_COUNT_NOOPER = """
    SELECT (SELECT COUNT(*) FROM MVTS WHERE nooper LIKE 'DGI%' AND RTRIM(nooper) = :nooper)
         + (SELECT COUNT(*) FROM MVTC WHERE nooper LIKE 'DGI%' AND RTRIM(nooper) = :nooper)
      FROM dual
"""


def db_connect():
    if oracledb is None:
        raise RuntimeError("Le module 'oracledb' n'est pas installé (pip3 install oracledb)")
    return oracledb.connect(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN)


def count_nooper_rows(cur, nooper):
    cur.execute(SQL_COUNT_NOOPER, {"nooper": nooper})
    (count,) = cur.fetchone()
    return int(count or 0)


def reserve_new_nooper(journal):
    """
    Calcule un NOOPER libre :
      MAX(dernier DGI dans MVTS, dernier DGI dans MVTC, dernier réservé
      dans le journal) + 1, puis vérifie qu'il n'existe ni dans MVTS,
      ni dans MVTC, ni dans le journal.
    """
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(SQL_MAX_NOOPER)
        (db_max,) = cur.fetchone()
        db_max = int(db_max or 0)

        journal_max = journal.max_reserved_seq()
        in_use = journal.noopers_in_use()

        seq = max(db_max, journal_max) + 1
        logger.info("[NOOPER] Dernier DGI en base (MVTS+MVTC)=%s, dernier réservé journal=%s -> candidat=%s",
                    format_nooper(db_max) if db_max else "aucun",
                    format_nooper(journal_max) if journal_max else "aucun",
                    format_nooper(seq))

        for _ in range(1000):
            candidate = format_nooper(seq)
            if candidate not in in_use and count_nooper_rows(cur, candidate) == 0:
                logger.info("[NOOPER] NOOPER retenu : %s", candidate)
                return candidate
            logger.warning("[NOOPER] %s déjà utilisé, on passe au suivant", candidate)
            seq += 1

        raise RuntimeError("Impossible de trouver un NOOPER libre après 1000 essais")
    finally:
        conn.close()


def book_payment_in_core_banking(nooper, amount, client_iban):
    """
    Débite le compte client et crédite le compte intermédiaire dans MVTS
    avec le NOOPER fourni, en une seule transaction (tout ou rien).

    Idempotent : si le NOOPER existe déjà (2 lignes dans MVTS ou MVTC),
    on n'insère rien et on retourne False.
    Retourne True si l'insertion a été faite.
    """
    compte_client = extract_local_account_number(client_iban)
    libelle = f"{PAYMENT_LABEL_PREFIX} {datetime.now().strftime('%d-%m-%Y')}"
    montant = abs(Decimal(str(amount)))

    conn = db_connect()
    try:
        cur = conn.cursor()

        existing = count_nooper_rows(cur, nooper)
        if existing == 2:
            logger.warning("[DB] NOOPER %s déjà présent dans MVTS/MVTC (2 lignes) : aucune insertion (anti-doublon)",
                           nooper)
            return False
        if existing != 0:
            raise RuntimeError(f"Situation incohérente : {existing} ligne(s) déjà présentes pour {nooper}")

        cur.execute("SELECT MAX(datoper) FROM state")
        row = cur.fetchone()
        dat_oper = row[0] if row else None
        if dat_oper is None:
            raise RuntimeError("Impossible de déterminer DATOPER (table STATE vide)")

        insert_sql = """
            INSERT INTO MVTS
                (COMPTE, DATOPER, DATVAL, MNTFRF, MNTDEV, LIBELLE, CODOPSC, LIGNE, DEVISE, EXPL, NOOPER, DATHGEN)
            VALUES
                (:compte, :datoper, :datval, :mntfrf, :mntdev, :libelle, :codopsc, :ligne, :devise, :expl, :nooper, SYSDATE)
        """

        # Ligne 1 : débit compte client
        cur.execute(insert_sql, {
            "compte": compte_client, "datoper": dat_oper, "datval": dat_oper,
            "mntfrf": -montant, "mntdev": -montant, "libelle": libelle,
            "codopsc": MOVEMENT_CODE, "ligne": "1", "devise": CURRENCY,
            "expl": FIXED_EXPL_VALUE, "nooper": nooper,
        })

        # Ligne 2 : crédit compte intermédiaire
        cur.execute(insert_sql, {
            "compte": INTERMEDIARY_ACCOUNT, "datoper": dat_oper, "datval": dat_oper,
            "mntfrf": montant, "mntdev": montant, "libelle": libelle,
            "codopsc": MOVEMENT_CODE, "ligne": "2", "devise": CURRENCY,
            "expl": FIXED_EXPL_VALUE, "nooper": nooper,
        })

        conn.commit()
        logger.info("[DB] Écriture OK: compte_client=%s, montant=%s, nooper=%s",
                    compte_client, montant, nooper)
        return True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================================
# POST-TRAITEMENT : SSH (mvtd / solde) + EMAIL
# ============================================================================

def run_remote_post_processing(nooper):
    if paramiko is None:
        raise RuntimeError("Le module 'paramiko' n'est pas installé (pip3 install paramiko)")
    if not nooper:
        raise ValueError("nooper manquant, impossible de lancer les procédures SSH")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(SSH_HOST, username=SSH_USER, password=SSH_PASSWORD, timeout=SSH_TIMEOUT)
        for template in SSH_POST_PROCESS_COMMAND_TEMPLATES:
            cmd = template.format(nooper=nooper)
            logger.info("[SSH] Exécution: %s", cmd)
            stdin, stdout, stderr = client.exec_command(cmd, timeout=SSH_TIMEOUT)
            exit_status = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()

            if out:
                logger.info("[SSH] stdout (%s): %s", cmd, out)
            if err:
                logger.warning("[SSH] stderr (%s): %s", cmd, err)

            if exit_status != 0:
                raise RuntimeError(f"Commande SSH terminée avec exit_status={exit_status}: {cmd}")
    finally:
        client.close()


def send_payment_success_email(transaction_id, amount, iban, nooper):
    if not EMAIL_HOST:
        logger.warning("[EMAIL] EMAIL_HOST non configuré, notification non envoyée pour %s", transaction_id)
        return

    subject = f"[DGI] Paiement exécuté avec succès - {transaction_id}"
    body = (
        f"Le paiement {transaction_id} a été exécuté avec succès.\n\n"
        f"Montant       : {amount} {CURRENCY}\n"
        f"IBAN          : {iban}\n"
        f"Référence bancaire (NOOPER) : {nooper}\n"
        f"Date          : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n"
    )

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_HOST_USER
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)

    try:
        if EMAIL_USE_SSL:
            server = smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, timeout=REQUEST_TIMEOUT)
        else:
            server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=REQUEST_TIMEOUT)
        try:
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, EMAIL_RECIPIENTS, msg.as_string())
        finally:
            server.quit()
        logger.info("[EMAIL] Notification de succès envoyée pour %s à %s", transaction_id, EMAIL_RECIPIENTS)
    except Exception:
        logger.exception("[EMAIL] Échec envoi notification pour %s", transaction_id)


# ============================================================================
# FLUX PAIEMENTS
# ============================================================================

def get_all_payments():
    logger.info("Récupération de la liste des paiements: %s", PAYMENTS_LIST_URL)
    resp = requests.get(PAYMENTS_LIST_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_payments_by_status(status):
    payments = get_all_payments()
    filtered = [p for p in payments if p.get("status") == status]
    logger.info("Paiements: total=%d, %s=%d", len(payments), status, len(filtered))
    return filtered


def get_payment_details(transaction_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(details_url("PAYMENT", transaction_id), headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def complete_booking_and_post_processing(transaction_id, journal):
    """
    À appeler UNIQUEMENT quand l'API STT a déjà accepté EXECUTED.
    Fait avancer le paiement : STT_EXECUTED -> MVTS_OK -> TERMINE.
    Chaque étape est enregistrée dans le journal : en cas de plantage,
    la reprise continue là où on s'est arrêté, sans réinsertion.
    """
    entry = journal.get(transaction_id)
    if not entry:
        logger.error("[PAYMENT] %s absent du journal, post-traitement impossible", transaction_id)
        return

    nooper = entry.get("nooper")
    iban = entry.get("iban")
    amount = entry.get("amount")
    state = entry.get("state")

    # Étape 1 : insertion MVTS
    if state == J_STT_OK:
        try:
            inserted = book_payment_in_core_banking(nooper, Decimal(amount), iban)
        except Exception:
            logger.critical(
                "[PAYMENT] ÉCHEC écriture MVTS pour %s (nooper=%s) alors que STT est déjà EXECUTED. "
                "État journal laissé en STT_EXECUTED : nouvel essai au prochain passage.",
                transaction_id, nooper, exc_info=True,
            )
            return
        journal.set(transaction_id, state=J_MVTS_OK, mvts_inserted_now=inserted, mvts_at=_now_str())
        state = J_MVTS_OK

    # Étape 2 : SSH + email
    if state == J_MVTS_OK:
        journal.set(transaction_id, state=J_POST)
        ssh_ok = True
        try:
            run_remote_post_processing(nooper)
        except Exception:
            ssh_ok = False
            logger.critical(
                "[PAYMENT] ÉCHEC procédures SSH (mvtd/sldisp) pour %s (nooper=%s) — "
                "écriture MVTS faite, INTERVENTION MANUELLE REQUISE pour mvtd/solde disponible.",
                transaction_id, nooper, exc_info=True,
            )

        send_payment_success_email(transaction_id, amount, iban, nooper)
        journal.set(transaction_id, state=J_TERMINE if ssh_ok else J_SSH_KO, ssh_ok=ssh_ok)
        logger.info("[PAYMENT] %s terminé (nooper=%s, ssh_ok=%s)", transaction_id, nooper, ssh_ok)


# ---- PASSE 0 : REPRISE DES PAIEMENTS INACHEVÉS ---------------------------

def resume_unfinished_payments(journal):
    unfinished = journal.entries_in_states({J_RESERVE, J_STT_OK, J_MVTS_OK, J_POST})
    if not unfinished:
        return

    logger.info("[PAYMENT][REPRISE] %d paiement(s) inachevé(s) dans le journal", len(unfinished))
    status_by_tid = None

    for tid, entry in unfinished:
        state = entry.get("state")
        nooper = entry.get("nooper")

        if state in (J_STT_OK, J_MVTS_OK):
            logger.info("[PAYMENT][REPRISE] %s (nooper=%s) en état %s : on termine", tid, nooper, state)
            complete_booking_and_post_processing(tid, journal)

        elif state == J_POST:
            # Plantage pendant le SSH : on ne sait pas si mvtd/sldisp ont tourné,
            # on ne relance pas automatiquement (risque de double mise à jour).
            logger.critical(
                "[PAYMENT][REPRISE] %s (nooper=%s) : interruption pendant le post-traitement SSH. "
                "Vérifier manuellement mvtd et solde disponible.", tid, nooper,
            )
            journal.set(tid, state=J_A_VERIFIER)

        elif state == J_RESERVE:
            # L'appel API a peut-être réussi côté STT malgré une erreur/timeout
            # chez nous : on vérifie le statut réel.
            if status_by_tid is None:
                try:
                    status_by_tid = {str(p.get("transactionId")): p.get("status") for p in get_all_payments()}
                except Exception:
                    logger.exception("[PAYMENT][REPRISE] Impossible de lire la liste des paiements, "
                                     "reprise des NOOPER réservés reportée")
                    status_by_tid = {}
                    continue
            if not status_by_tid:
                continue

            stt_status = status_by_tid.get(tid)
            if stt_status == STATUS_EXECUTED:
                logger.warning("[PAYMENT][REPRISE] %s est EXECUTED côté STT (l'API avait réussi) : "
                               "on passe à l'écriture MVTS avec nooper=%s", tid, nooper)
                journal.set(tid, state=J_STT_OK)
                complete_booking_and_post_processing(tid, journal)
            elif stt_status in (STATUS_IN_PROGRESS, STATUS_PENDING):
                logger.info("[PAYMENT][REPRISE] %s toujours %s : sera retraité en passe 2 avec nooper=%s",
                            tid, stt_status, nooper)
            else:
                logger.warning("[PAYMENT][REPRISE] %s statut STT=%s : NOOPER %s libéré (ABANDONNE)",
                               tid, stt_status, nooper)
                journal.set(tid, state=J_ABANDONNE)


# ---- PASSE 1 : PENDING -> IN_PROGRESS ------------------------------------

def initialize_payment(payment, token):
    transaction_id = payment.get("transactionId")
    logger.info("---- [PAYMENT][PASSE 1] Initialisation transactionId=%s ----", transaction_id)

    try:
        details = get_payment_details(transaction_id, token)
    except Exception:
        logger.exception("[PAYMENT][PASSE 1] Échec récupération détails pour %s, non pris en charge", transaction_id)
        return

    iban = details.get("taxPayerIban")
    amount = parse_amount(details.get("amount"))

    if not iban or amount is None:
        logger.error("[PAYMENT][PASSE 1] Détails incomplets (iban=%s, amount=%s) pour %s, rejet direct (001)",
                     iban, details.get("amount"), transaction_id)
        _safe_update("PAYMENT", transaction_id, STATUS_REJECTED, token, REJECTION_CODE_INVALID_ACCOUNT)
        return

    if _safe_update("PAYMENT", transaction_id, STATUS_IN_PROGRESS, token):
        logger.info("[PAYMENT][PASSE 1] %s pris en charge (IN_PROGRESS)", transaction_id)


def process_all_pending_payments(token):
    try:
        pending_payments = get_payments_by_status(STATUS_PENDING)
    except Exception:
        logger.exception("[PAYMENT][PASSE 1] Échec récupération liste des paiements PENDING.")
        return

    for payment in pending_payments:
        initialize_payment(payment, token)


# ---- PASSE 2 : IN_PROGRESS -> EXECUTED / REJECTED ------------------------

def finalize_payment(payment, token, journal):
    transaction_id = str(payment.get("transactionId"))
    logger.info("---- [PAYMENT][PASSE 2] Traitement transactionId=%s ----", transaction_id)

    entry = journal.get(transaction_id)
    state = entry.get("state") if entry else None

    # Cas : on a déjà eu un EXECUTED accepté pour ce paiement, mais STT le
    # montre encore IN_PROGRESS. On renvoie EXECUTED avec le MÊME nooper,
    # SANS jamais réinsérer dans MVTS.
    if state in J_STATES_DEJA_EXECUTES:
        logger.warning("[PAYMENT][PASSE 2] %s déjà traité (journal=%s, nooper=%s) mais encore IN_PROGRESS "
                       "côté STT : renvoi de EXECUTED uniquement, aucune insertion.",
                       transaction_id, state, entry.get("nooper"))
        _safe_update("PAYMENT", transaction_id, STATUS_EXECUTED, token, transaction_id_bank=entry.get("nooper"))
        return

    # 1. Détails
    try:
        details = get_payment_details(transaction_id, token)
    except Exception:
        logger.exception("[PAYMENT][PASSE 2] Échec récupération détails pour %s (reste en IN_PROGRESS)", transaction_id)
        return

    iban = details.get("taxPayerIban")
    raw_amount = details.get("amount")
    amount = parse_amount(raw_amount)
    logger.info("[PAYMENT][PASSE 2] IBAN=%s | Montant=%s | Contribuable=%s",
                iban, raw_amount, details.get("taxPayerLegalName"))

    if not iban or amount is None:
        logger.error("[PAYMENT][PASSE 2] Détails incomplets (iban=%s, amount=%s) pour %s, rejet (001)",
                     iban, raw_amount, transaction_id)
        if _safe_update("PAYMENT", transaction_id, STATUS_REJECTED, token, REJECTION_CODE_INVALID_ACCOUNT) and entry:
            journal.set(transaction_id, state=J_ABANDONNE)
        return

    # 2. Vérification IBAN
    try:
        cpt_response = check_iban(iban)
    except Exception:
        logger.exception("[PAYMENT][PASSE 2] Échec vérification IBAN %s pour %s (reste en IN_PROGRESS)",
                         iban, transaction_id)
        return

    logger.info("[PAYMENT][PASSE 2] Réponse cpt-by-iban: %s", cpt_response)
    eligible, rejection_code = evaluate_iban_eligibility(cpt_response)

    if not eligible:
        logger.info("[PAYMENT][PASSE 2] %s rejeté (code %s)", transaction_id, rejection_code)
        if _safe_update("PAYMENT", transaction_id, STATUS_REJECTED, token, rejection_code) and entry:
            journal.set(transaction_id, state=J_ABANDONNE)
        return

    try:
        compte_client = extract_local_account_number(iban)
    except ValueError:
        logger.exception("[PAYMENT][PASSE 2] IBAN %s inexploitable pour %s (reste en IN_PROGRESS, à vérifier)",
                         iban, transaction_id)
        return

    # 3. NOOPER : on réutilise celui déjà réservé, sinon on en réserve un nouveau
    if state == J_RESERVE and entry.get("nooper"):
        nooper = entry["nooper"]
        logger.info("[PAYMENT][PASSE 2] Réutilisation du NOOPER déjà réservé %s pour %s", nooper, transaction_id)
        journal.set(transaction_id, amount=str(amount), iban=iban, compte=compte_client)
    else:
        try:
            nooper = reserve_new_nooper(journal)
        except Exception:
            logger.exception("[PAYMENT][PASSE 2] Impossible de calculer le NOOPER pour %s (reste en IN_PROGRESS)",
                             transaction_id)
            return
        journal.set(transaction_id, state=J_RESERVE, nooper=nooper,
                    amount=str(amount), iban=iban, compte=compte_client)

    # 4. D'ABORD l'API STT -> EXECUTED
    if not _safe_update("PAYMENT", transaction_id, STATUS_EXECUTED, token, transaction_id_bank=nooper):
        logger.error("[PAYMENT][PASSE 2] API EXECUTED refusée/en échec pour %s (nooper=%s) : "
                     "AUCUNE écriture MVTS. Nouvel essai au prochain passage avec le même NOOPER.",
                     transaction_id, nooper)
        return

    journal.set(transaction_id, state=J_STT_OK, stt_executed_at=_now_str())

    # 5. ENSUITE insertion MVTS + SSH + email
    complete_booking_and_post_processing(transaction_id, journal)


def process_all_in_progress_payments(token, journal):
    try:
        in_progress_payments = get_payments_by_status(STATUS_IN_PROGRESS)
    except Exception:
        logger.exception("[PAYMENT][PASSE 2] Échec récupération liste des paiements IN_PROGRESS.")
        return

    for payment in in_progress_payments:
        try:
            finalize_payment(payment, token, journal)
        except Exception:
            logger.exception("[PAYMENT][PASSE 2] Erreur inattendue sur %s", payment.get("transactionId"))


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    start = datetime.now()
    logger.info("========== Démarrage du traitement (%s) ==========", start.isoformat())

    try:
        lock_fd = acquire_lock()
    except AlreadyRunningError as e:
        logger.warning(str(e))
        return

    try:
        try:
            token = get_access_token()
        except Exception:
            logger.exception("Échec récupération du token. Arrêt.")
            return

        # Comptes
        process_all_pending_accounts(token)

        # Paiements : sans journal lisible, on ne traite rien (sécurité anti-doublon)
        try:
            journal = PaymentJournal(JOURNAL_FILE)
        except Exception:
            logger.critical("Journal des paiements illisible (%s) : traitement des paiements ANNULÉ "
                            "pour éviter tout doublon. Vérifier le fichier.", JOURNAL_FILE, exc_info=True)
            return

        resume_unfinished_payments(journal)          # Passe 0
        process_all_pending_payments(token)          # Passe 1
        process_all_in_progress_payments(token, journal)  # Passe 2

    finally:
        # On ne supprime pas le fichier de verrou : le supprimer pendant
        # qu'un autre process l'ouvre peut casser le verrou flock.
        lock_fd.close()

    duration = (datetime.now() - start).total_seconds()
    logger.info("========== Fin du traitement (durée: %.2fs) ==========", duration)


if __name__ == "__main__":
    main()
