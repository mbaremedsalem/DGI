from rest_framework import serializers

from .models import AccountTransaction, PaymentTransaction


class AccountCreateSerializer(serializers.ModelSerializer):
    transactionId = serializers.CharField(source="transaction_id", max_length=64)
    notificationType = serializers.CharField(write_only=True)

    # Champs optionnels — mapping snake_case -> camelCase si besoin
    tax_payer_iban = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=64
    )
    tax_payer_legal_name = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=255
    )
    nif = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=64
    )
    tax_payer_phone_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=32
    )

    class Meta:
        model = AccountTransaction
        fields = [
            "transactionId",
            "notificationType",
            "tax_payer_iban",
            "tax_payer_legal_name",
            "nif",
            "tax_payer_phone_number",
        ]

    def validate_notificationType(self, value):
        if value != "ACCOUNT":
            raise serializers.ValidationError("notificationType doit valoir 'ACCOUNT'.")
        return value

    def validate_transactionId(self, value):
        if AccountTransaction.objects.filter(transaction_id=value).exists():
            raise serializers.ValidationError(
                "Ce transactionId existe déjà. Il doit être unique."
            )
        return value

    def create(self, validated_data):
        validated_data.pop("notificationType", None)
        return AccountTransaction.objects.create(**validated_data)

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

    transactionId = serializers.CharField(source="transaction_id", max_length=64)
    notificationType = serializers.CharField(write_only=True)

    class Meta:
        model = PaymentTransaction
        fields = [
            "transactionId", "notificationType",
            "tax_payer_iban", "tax_payer_legal_name", "nif", "ntd",
            "tax_payer_phone_number", "amount",
        ]

    def validate_notificationType(self, value):
        if value != "PAYMENT":
            raise serializers.ValidationError("notificationType doit valoir 'PAYMENT'.")
        return value

    def validate_transactionId(self, value):
        if PaymentTransaction.objects.filter(transaction_id=value).exists():
            raise serializers.ValidationError(
                "Ce transactionId existe déjà. Il doit être unique."
            )
        return value

    def create(self, validated_data):
        validated_data.pop("notificationType", None)
        return PaymentTransaction.objects.create(**validated_data)


class PaymentDetailSerializer(serializers.ModelSerializer):
    transactionId = serializers.CharField(source="transaction_id")
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

    transactionId = serializers.CharField()
    notificationType = serializers.ChoiceField(choices=["ACCOUNT", "PAYMENT"])
    status = serializers.CharField()
    rejectionReasonCode = serializers.CharField(required=False, allow_blank=True, default="")
    transactionIdBank = serializers.CharField(required=False, allow_blank=True, default="")
