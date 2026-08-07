from django.contrib import admin
from .models import OAuthClient, AccessToken, AccountTransaction, PaymentTransaction


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    list_display = ("client_id", "client_secret", "bank_name")


@admin.register(AccessToken)
class AccessTokenAdmin(admin.ModelAdmin):
    list_display = ("token", "client", "created_at", "expires_at")


@admin.register(AccountTransaction)
class AccountTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "tax_payer_legal_name", "status", "operation_date")


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "tax_payer_legal_name", "amount", "status", "operation_date")
