from rest_framework import serializers

from .models import AccountTransaction, PaymentTransaction


class AccountCreateSerializer(serializers.ModelSerializer):
    """Utilisé UNIQUEMENT par l'endpoint de test qui simule un contribuable
    ajoutant un compte bancaire sur son espace STT (déclenche la notification)."""

    class Meta:
        model = AccountTransaction
        fields = ["tax_payer_iban", "tax_payer_legal_name", "nif", "tax_payer_phone_number"]


class AccountDetailSerializer(serializers.ModelSerializer):
    transactionId = serializers.UUIDField(source="transaction_id")
    operationType = serializers.SerializerMethodField()
    taxPayerIban = serializers.CharField(source="tax_payer_iban")
    taxPayerLegalName = serializers.CharField(source="tax_payer_legal_name")
    nif = serializers.CharField()
    taxPayerPhoneNumber = serializers.CharField(source="tax_payer_phone_number")
    operationDate = serializers.DateTimeField(source="operation_date")

    class Meta:
        model = AccountTransaction
        fields = [
            "transactionId", "operationType", "taxPayerIban", "taxPayerLegalName",
            "nif", "taxPayerPhoneNumber", "operationDate",
        ]

    def get_operationType(self, obj):
        return "ACCOUNT"


class PaymentCreateSerializer(serializers.ModelSerializer):
    """Endpoint de test simulant un contribuable confirmant un ordre de paiement."""

    class Meta:
        model = PaymentTransaction
        fields = [
            "tax_payer_iban", "tax_payer_legal_name", "nif", "ntd",
            "tax_payer_phone_number", "amount",
        ]


class PaymentDetailSerializer(serializers.ModelSerializer):
    transactionId = serializers.UUIDField(source="transaction_id")
    operationType = serializers.SerializerMethodField()
    taxPayerIban = serializers.CharField(source="tax_payer_iban")
    taxPayerLegalName = serializers.CharField(source="tax_payer_legal_name")
    nif = serializers.CharField()
    ntd = serializers.CharField()
    taxPayerPhoneNumber = serializers.CharField(source="tax_payer_phone_number")
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    operationDate = serializers.DateTimeField(source="operation_date")

    class Meta:
        model = PaymentTransaction
        fields = [
            "transactionId", "operationType", "taxPayerIban", "taxPayerLegalName",
            "nif", "ntd", "taxPayerPhoneNumber", "amount", "operationDate",
        ]

    def get_operationType(self, obj):
        return "PAYMENT"


class UpdateRequestSerializer(serializers.Serializer):
    """Corps de la requête POST /update (commun ACCOUNT et PAYMENT)."""

    transactionId = serializers.UUIDField()
    notificationType = serializers.ChoiceField(choices=["ACCOUNT", "PAYMENT"])
    status = serializers.CharField()
    rejectionReasonCode = serializers.CharField(required=False, allow_blank=True, default="")
    transactionIdBank = serializers.CharField(required=False, allow_blank=True, default="")
