import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# OAuth2 (client_credentials) - simule ADIAS / DGI qui distribue un couple
# client_id / client_secret à chaque banque (IF).
# ---------------------------------------------------------------------------
class OAuthClient(models.Model):
    client_id = models.CharField(max_length=100, unique=True)
    client_secret = models.CharField(max_length=100)
    bank_name = models.CharField(max_length=150, default="Banque de Test")

    def __str__(self):
        return f"{self.client_id} ({self.bank_name})"


def generate_token():
    return uuid.uuid4().hex


class AccessToken(models.Model):
    token = models.CharField(max_length=64, unique=True, default=generate_token)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    TOKEN_LIFETIME = timedelta(minutes=5)

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + self.TOKEN_LIFETIME
        super().save(*args, **kwargs)

    @property
    def is_valid(self):
        return timezone.now() < self.expires_at

    def __str__(self):
        return self.token


# ---------------------------------------------------------------------------
# Codes de rejet (Annexe 2)
# ---------------------------------------------------------------------------
REJECTION_REASONS = {
    "001": "Numéro de compte inexistant ou invalide",
    "002": "Compte clôturé",
    "003": "Opposition sur le compte",
    "005": "Provision insuffisante",
    "006": "Décision judiciaire (saisie arrêt, blocage ou faillite)",
    "007": "Titulaire décédé",
    "008": "Doublon",
    "009": "Non prise en charge",
    "800": "Pas de réponse",
    "801": "Rejet de la demande",
    "802": "Incident technique",
    "999": "Dépassement de plafond",
}


# ---------------------------------------------------------------------------
# III. Ajout du compte bancaire
# ---------------------------------------------------------------------------
class AccountTransaction(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_AUTHORIZED = "AUTHORIZED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "En attente de la décision banque"),
        (STATUS_AUTHORIZED, "Autorisé"),
        (STATUS_REJECTED, "Rejeté"),
    ]

    transaction_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tax_payer_iban = models.CharField(max_length=34)
    tax_payer_legal_name = models.CharField(max_length=150)
    nif = models.CharField(max_length=20)
    tax_payer_phone_number = models.CharField(max_length=20)
    operation_date = models.DateTimeField(default=timezone.now)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING)
    rejection_reason_code = models.CharField(max_length=5, blank=True, default="")

    def __str__(self):
        return f"ACCOUNT {self.transaction_id} [{self.status}]"


# ---------------------------------------------------------------------------
# IV. Exécution des ordres de paiement
# ---------------------------------------------------------------------------
class PaymentTransaction(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_EXECUTED = "EXECUTED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "En attente de prise en charge"),
        (STATUS_IN_PROGRESS, "En cours de traitement"),
        (STATUS_EXECUTED, "Exécuté"),
        (STATUS_REJECTED, "Rejeté"),
    ]

    transaction_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tax_payer_iban = models.CharField(max_length=34)
    tax_payer_legal_name = models.CharField(max_length=150)
    nif = models.CharField(max_length=20)
    ntd = models.CharField(max_length=30)
    tax_payer_phone_number = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    operation_date = models.DateTimeField(default=timezone.now)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING)
    rejection_reason_code = models.CharField(max_length=5, blank=True, default="")
    transaction_id_bank = models.CharField(max_length=50, blank=True, default="")

    def __str__(self):
        return f"PAYMENT {self.transaction_id} [{self.status}]"
