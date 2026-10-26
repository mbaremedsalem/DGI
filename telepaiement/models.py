import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Profil utilisateur - chaque utilisateur (User Django) a un rôle parmi 4.
# ---------------------------------------------------------------------------
class UserProfile(models.Model):
    ROLE_ADMIN = "ADMIN"
    ROLE_COMMERCIAL = "COMMERCIAL"
    ROLE_OPERATION = "OPERATION"
    ROLE_COMPTABILITE = "COMPTABILITE"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Administrateur"),
        (ROLE_COMMERCIAL, "Commercial"),
        (ROLE_OPERATION, "Opération"),
        (ROLE_COMPTABILITE, "Comptabilité"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_OPERATION)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


# ---------------------------------------------------------------------------
# Associe un access token Keycloak (renvoyé par POST /stt/token) à
# l'utilisateur local qui l'a demandé, pour retrouver son profil à partir de
# ce même token (GET /api/profile/, Authorization: Bearer <access_token>).
# ---------------------------------------------------------------------------
class IssuedSttToken(models.Model):
    token = models.TextField(unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stt_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        return self.expires_at is None or timezone.now() < self.expires_at

    def __str__(self):
        return f"STT token de {self.user.username}"


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
    "803": "Dépassement de plafond",
}


# ---------------------------------------------------------------------------
# III. Ajout du compte bancaire
# ---------------------------------------------------------------------------
import uuid

from django.db import models
from django.utils import timezone


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

    transaction_id = models.CharField(max_length=64, primary_key=True, editable=True)
    tax_payer_iban = models.CharField(max_length=34, blank=True, default="")
    tax_payer_legal_name = models.CharField(max_length=150, blank=True, default="")
    nif = models.CharField(max_length=20, blank=True, default="")
    ntd = models.CharField(max_length=30, blank=True, default="")
    tax_payer_phone_number = models.CharField(max_length=20, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    operation_date = models.DateTimeField(default=timezone.now)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING)
    rejection_reason_code = models.CharField(max_length=5, blank=True, default="")
    transaction_id_bank = models.CharField(max_length=50, blank=True, default="")

    def __str__(self):
        return f"PAYMENT {self.transaction_id} [{self.status}]"


# ---------------------------------------------------------------------------
# IV.5 — Archive des paiements traités (EXECUTED, confirmés auprès du STT)
# ---------------------------------------------------------------------------
class ArchivedPayment(models.Model):
    transaction_id = models.CharField(max_length=64, primary_key=True, editable=True)
    tax_payer_iban = models.CharField(max_length=34, blank=True, default="")
    tax_payer_legal_name = models.CharField(max_length=150, blank=True, default="")
    nif = models.CharField(max_length=20, blank=True, default="")
    ntd = models.CharField(max_length=30, blank=True, default="")
    tax_payer_phone_number = models.CharField(max_length=20, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    operation_date = models.DateTimeField(null=True, blank=True)
    transaction_id_bank = models.CharField(max_length=50, blank=True, default="")
    status = models.CharField(max_length=15, default=PaymentTransaction.STATUS_EXECUTED)
    stt_response = models.JSONField(blank=True, default=dict)
    archived_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ARCHIVE {self.transaction_id}"
