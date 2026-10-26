from django.urls import path
from . import views

urlpatterns = [
    # II. Autorisation
    path("oauth/token", views.TokenView.as_view(), name="oauth-token"),

    # Client OAuth2 côté banque : récupère un token auprès du vrai STT/DGI (Keycloak)
    path("stt/token", views.STTTokenView.as_view(), name="stt-token"),

    # Login local (username/password) -> token local + profil utilisateur
    path("api/login/", views.LoginView.as_view(), name="api-login"),
    path("api/profile/", views.ProfileView.as_view(), name="api-profile"),
    path("api/change-password/", views.ChangePasswordView.as_view(), name="api-change-password"),
    path("api/credentials/", views.CredentialsView.as_view(), name="api-credentials"),

    # Proxys vers l'API réelle du STT (évitent le CORS depuis le navigateur)
    path("stt/notification/ACCOUNT/<str:transaction_id>", views.STTAccountDetailProxyView.as_view(), name="stt-account-detail"),
    path("stt/notification/PAYMENT/<str:transaction_id>", views.STTPaymentDetailProxyView.as_view(), name="stt-payment-detail"),
    path("stt/update", views.STTUpdateProxyView.as_view(), name="stt-update"),


    path('stt/notificationstt/<str:notif_type>/<str:transaction_id>/', views.get_notification, name='stt-notification'),
    path('stt/updatestt/', views.update_operation, name='stt-update-real'),

    # Aides de test (hors spécification) pour simuler le contribuable côté STT
    path("api/test/accounts/", views.CreateAccountTestView.as_view(), name="test-create-account"),
    path("api/test/payments/", views.CreatePaymentTestView.as_view(), name="test-create-payment"),
    path("api/test/rejection-codes/", views.RejectionReasonsView.as_view(), name="test-rejection-codes"),

    # IV.5 Traitement / archivage d'un paiement EXECUTED
    path("api/payments/<str:transaction_id>/process/", views.ProcessPaymentView.as_view(), name="process-payment"),
    path("api/payments/archive/", views.ArchivedPaymentListView.as_view(), name="archived-payments-list"),
    path("api/payments/archive/<str:transaction_id>/", views.ArchivedPaymentDetailView.as_view(), name="archived-payment-detail"),

    # III.2 / IV.2 Récupération des informations
    path("notification/ACCOUNT/<str:transaction_id>", views.AccountNotificationDetailView.as_view(), name="account-detail"),
    path("notification/PAYMENT/<str:transaction_id>", views.PaymentNotificationDetailView.as_view(), name="payment-detail"),

    # III.3 / IV.3 / IV.4 Décision / initiation / validation
    path("update", views.UpdateView.as_view(), name="update"),

    
]
