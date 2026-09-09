from django.urls import path
from . import views

urlpatterns = [
    # II. Autorisation
    path("oauth/token", views.TokenView.as_view(), name="oauth-token"),
    # Client OAuth2 côté banque : récupère un token auprès du vrai STT/DGI (Keycloak)
    path("stt/token", views.STTTokenView.as_view(), name="stt-token"),

    # Aides de test (hors spécification) pour simuler le contribuable côté STT
    path("api/test/accounts/", views.CreateAccountTestView.as_view(), name="test-create-account"),
    path("api/test/payments/", views.CreatePaymentTestView.as_view(), name="test-create-payment"),
    path("api/test/rejection-codes/", views.RejectionReasonsView.as_view(), name="test-rejection-codes"),

    path('stt/notificationstt/<str:notif_type>/<str:transaction_id>/', views.get_notification, name='stt-notification'),
    path('stt/updatestt/', views.update_operation, name='stt-update'),    
    # III.2 / IV.2 Récupération des informations
    path("notification/ACCOUNT/<str:transaction_id>/", views.AccountNotificationDetailView.as_view(), name="account-detail"),
    path("notification/PAYMENT/<str:transaction_id>/", views.PaymentNotificationDetailView.as_view(), name="payment-detail"),

    # III.3 / IV.3 / IV.4 Décision / initiation / validation
    path("update", views.UpdateView.as_view(), name="update"),
]
