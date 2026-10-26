import base64
import binascii
import decimal
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import status, permissions
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import BearerTokenAuthentication, STTIssuedTokenAuthentication
from .models import (
    AccessToken, AccountTransaction, ArchivedPayment, IssuedSttToken, OAuthClient, PaymentTransaction,
    REJECTION_REASONS, UserProfile,
)
from .serializers import (
    AccountCreateSerializer, AccountDetailSerializer, ArchivedPaymentSerializer,
    ChangePasswordSerializer, PaymentCreateSerializer, PaymentDetailSerializer,
    UpdateRequestSerializer, UserProfileSerializer,
)


# ---------------------------------------------------------------------------
# II. Autorisations d'accès à l'API — POST /oauth/token
# ---------------------------------------------------------------------------
class TokenView(APIView):
    """
    POST /oauth/token — grant_type=client_credentials (RFC 6749 §4.4).

    Le client peut s'authentifier de deux façons :
      - client_secret_basic : en-tête `Authorization: Basic base64(client_id:client_secret)`
        (dans ce cas aucun body n'est nécessaire, grant_type vaut
        "client_credentials" par défaut puisque c'est le seul grant type
        supporté par cette API)
      - client_secret_post  : `client_id` / `client_secret` (+ `grant_type`)
        dans le corps de la requête
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def _client_credentials_from_basic_header(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return None, None
        try:
            decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
            client_id, client_secret = decoded.split(":", 1)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None, None
        return client_id, client_secret

    def post(self, request):
        data = request.data

        client_id, client_secret = self._client_credentials_from_basic_header(request)
        if client_id is None:
            client_id = data.get("client_id")
            client_secret = data.get("client_secret")
            grant_type = data.get("grant_type")
        else:
            # Client authentifié via Basic Auth : le body est facultatif,
            # "client_credentials" est le seul grant type supporté ici.
            grant_type = data.get("grant_type") or "client_credentials"

        if grant_type != "client_credentials":
            return Response(
                {"error": "unsupported_grant_type", "error_description": "grant_type doit être 'client_credentials'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not client_id or not client_secret:
            return Response(
                {"error": "invalid_request", "error_description": "client_id et client_secret sont requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            client = OAuthClient.objects.get(client_id=client_id, client_secret=client_secret)
        except OAuthClient.DoesNotExist:
            response = Response(
                {"error": "invalid_client", "error_description": "client_id / client_secret invalide."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            response["WWW-Authenticate"] = "Basic"
            return response

        token = AccessToken.objects.create(client=client)
        # Format standard OAuth2 (RFC 6749 §5.1) : STT lit le champ `access_token`.
        return Response(
            {
                "access_token": token.token,
                "token_type": "Bearer",
                "expires_in": int(AccessToken.TOKEN_LIFETIME.total_seconds()),
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Client OAuth2 côté banque — POST /stt/token
# 1) Vérifie que le client (username/password) existe dans notre base locale.
# 2) Si oui, récupère un access token auprès du vrai serveur STT/DGI (Keycloak)
#    avec le couple client_id/client_secret attribué à cette banque (settings.STT_*)
#    et renvoie tel quel le JSON retourné par Keycloak.
# 3) Si le client n'existe pas / mauvais mot de passe, renvoie une erreur typée.
# ---------------------------------------------------------------------------
def _fetch_stt_credentials():
    """Demande un access token à Keycloak avec le couple client_id/secret de
    CETTE banque (settings.STT_*) et renvoie (payload, status_code)."""
    if not settings.STT_CLIENT_ID or not settings.STT_CLIENT_SECRET:
        return (
            {
                "error": "server_error",
                "error_description": "STT_CLIENT_ID / STT_CLIENT_SECRET non configurés côté serveur.",
            },
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    try:
        upstream = requests.post(
            settings.STT_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.STT_CLIENT_ID,
                "client_secret": settings.STT_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
    except requests.RequestException as exc:
        return ({"error": "upstream_unreachable", "error_description": str(exc)}, status.HTTP_502_BAD_GATEWAY)

    try:
        payload = upstream.json()
    except ValueError:
        payload = {"error": "upstream_invalid_response", "error_description": upstream.text}

    return (payload, upstream.status_code)


class STTTokenView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        if not username or not password:
            return Response(
                {"error": "invalid_request", "error_description": "username et password sont requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response(
                {"error": "invalid_client", "error_description": "Client inconnu ou identifiants invalides."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        payload, status_code = _fetch_stt_credentials()
        if 200 <= status_code < 300:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            payload["profile"] = UserProfileSerializer(profile).data

            access_token = payload.get("access_token")
            if access_token:
                expires_in = payload.get("expires_in")
                expires_at = timezone.now() + timedelta(seconds=expires_in) if expires_in else None
                IssuedSttToken.objects.update_or_create(
                    token=access_token,
                    defaults={"user": user, "expires_at": expires_at},
                )

        return Response(payload, status=status_code)


# ---------------------------------------------------------------------------
# Login local — POST /api/login/
# Vérifie username/password, renvoie un token local (Authorization: Token
# <token>) + le profil de l'utilisateur. Ce token local sert ensuite à
# authentifier /api/profile/ et /api/credentials/ sans redemander le mot de
# passe à chaque appel.
# ---------------------------------------------------------------------------
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        if not username or not password:
            return Response(
                {"error": "invalid_request", "error_description": "username et password sont requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response(
                {"error": "invalid_credentials", "error_description": "Identifiants invalides."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token, _ = Token.objects.get_or_create(user=user)
        profile, _ = UserProfile.objects.get_or_create(user=user)

        return Response(
            {"token": token.key, "profile": UserProfileSerializer(profile).data},
            status=status.HTTP_200_OK,
        )


class ProfileView(APIView):
    """GET /api/profile/ — profil de l'utilisateur connecté. Accepte soit le
    token local (Authorization: Token <token>, obtenu via /api/login/), soit
    l'access token STT (Authorization: Bearer <access_token>, obtenu via
    /stt/token)."""

    authentication_classes = [TokenAuthentication, STTIssuedTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        return Response(UserProfileSerializer(profile).data, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    """POST /api/change-password/ — change le mot de passe de l'utilisateur
    connecté. Body attendu: { oldPassword, newPassword }. Le token local
    existant est invalidé et un nouveau token est renvoyé."""

    authentication_classes = [TokenAuthentication, STTIssuedTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)

        return Response(
            {"message": "Mot de passe modifié avec succès.", "token": token.key},
            status=status.HTTP_200_OK,
        )


class CredentialsView(APIView):
    """GET /api/credentials/ — à partir du token local de l'utilisateur connecté
    (Authorization: Token <token>), renvoie ses credentials STT (access token
    Keycloak), sans redemander username/password."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        payload, status_code = _fetch_stt_credentials()
        return Response(payload, status=status_code)


def _stt_bearer_token(request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[len("Bearer "):]


def _proxy_to_stt(method, path, token, json_body=None):
    """Relaie un appel vers l'API réelle du STT (settings.STT_API_BASE_URL)
    en réutilisant le token Bearer fourni par le front (obtenu via /stt/token).
    Évite tout appel cross-origin depuis le navigateur (pas de CORS côté STT)."""
    try:
        upstream = requests.request(
            method,
            f"{settings.STT_API_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=15,
        )
    except requests.RequestException as exc:
        return Response(
            {"error": "upstream_unreachable", "error_description": str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    try:
        payload = upstream.json()
    except ValueError:
        payload = {"error": "upstream_invalid_response", "error_description": upstream.text}
    return Response(payload, status=upstream.status_code)


class STTAccountDetailProxyView(APIView):
    """GET /stt/notification/ACCOUNT/{transactionId} — proxy vers le vrai STT."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, transaction_id):
        token = _stt_bearer_token(request)
        if not token:
            return Response(
                {"error": "invalid_request", "error_description": "Authorization: Bearer <token STT> requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _proxy_to_stt("GET", f"/notification/ACCOUNT/{transaction_id}", token)


class STTPaymentDetailProxyView(APIView):
    """GET /stt/notification/PAYMENT/{transactionId} — proxy vers le vrai STT."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, transaction_id):
        token = _stt_bearer_token(request)
        if not token:
            return Response(
                {"error": "invalid_request", "error_description": "Authorization: Bearer <token STT> requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _proxy_to_stt("GET", f"/notification/PAYMENT/{transaction_id}", token)


class STTUpdateProxyView(APIView):
    """POST /stt/update — proxy vers le vrai STT (décision / initiation / validation)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        token = _stt_bearer_token(request)
        if not token:
            return Response(
                {"error": "invalid_request", "error_description": "Authorization: Bearer <token STT> requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _proxy_to_stt("POST", "/update", token, json_body=request.data)


# ---------------------------------------------------------------------------
# Endpoints de TEST (hors spec) permettant de simuler un contribuable qui
# ajoute un compte, ou confirme un ordre de paiement sur STT. C'est ce qui,
# dans le vrai système, déclenche la notification POST vers la banque.
# ---------------------------------------------------------------------------

def extract_account_number_from_iban(iban: str) -> str | None:
    """
    Extrait le numéro de compte (11 chiffres) d'un IBAN mauritanien.
    Structure MR : MR(2) + clé(2) + banque(5) + agence(5) + compte(11) + clé RIB(2) = 27 caractères
    Exemple : MR1300026000010326013000301 -> 03260130003
    """
    if not iban:
        return None
    iban = iban.replace(" ", "").upper()
    if not iban.startswith("MR") or len(iban) != 27:
        return None
    return iban[14:25]


class CreateAccountTestView(APIView):
    """POST /api/test/accounts/ — crée une transaction ACCOUNT (status=PENDING)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        """GET /api/test/accounts/ — liste transactionId + type + status (+ motif si rejeté)."""
        data = []
        for obj in AccountTransaction.objects.all().order_by("-operation_date"):
            item = {
                "transactionId": str(obj.transaction_id),
                "operationType": "ACCOUNT",
                "status": obj.status,
                "accountNumber": extract_account_number_from_iban(obj.tax_payer_iban),
            }
            if obj.status == AccountTransaction.STATUS_REJECTED:
                item["rejectionReasonCode"] = obj.rejection_reason_code
                item["rejectionReasonLabel"] = REJECTION_REASONS.get(
                    obj.rejection_reason_code, "Motif inconnu"
                )
            data.append(item)
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = AccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(
            {
                "notificationType": "ACCOUNT",
                "transactionId": str(obj.transaction_id),
                "message": "Transaction créée. La banque doit maintenant appeler "
                           "GET /notification/ACCOUNT/{transactionId} puis POST /update.",
            },
            status=status.HTTP_201_CREATED,
        ) 


class CreatePaymentTestView(APIView):
    """POST /api/test/payments/ — crée une transaction PAYMENT (status=PENDING)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        """GET /api/test/payments/ — liste transactionId + type + status (+ motif si rejeté)
        pour les paiements PAS ENCORE traités/archivés."""
        archived_ids = ArchivedPayment.objects.values_list("transaction_id", flat=True)

        data = []
        for obj in (
            PaymentTransaction.objects
            .exclude(transaction_id__in=archived_ids)
            .order_by("-operation_date")
        ):
            item = {
                "transactionId": str(obj.transaction_id),
                "operationType": "PAYMENT",
                "status": obj.status,
                "accountNumber": extract_account_number_from_iban(obj.tax_payer_iban),
            }
            if obj.status == PaymentTransaction.STATUS_REJECTED:
                item["rejectionReasonCode"] = obj.rejection_reason_code
                item["rejectionReasonLabel"] = REJECTION_REASONS.get(
                    obj.rejection_reason_code, "Motif inconnu"
                )
            data.append(item)
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(
            {
                "notificationType": "PAYMENT",
                "transactionId": str(obj.transaction_id),
                "message": "Transaction créée. La banque doit maintenant appeler "
                           "GET /notification/PAYMENT/{transactionId} puis POST /update.",
            },
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# IV.5 — Traitement / archivage d'un paiement EXECUTED
# ---------------------------------------------------------------------------
class ProcessPaymentView(APIView):
    """POST /api/payments/{transactionId}/process/ — marque un paiement
    EXECUTED comme traité : reconfirme les informations auprès du vrai STT
    via GET stt/notificationstt/PAYMENT/{transactionId} (Authorization: Bearer
    <token STT> requis), puis archive la transaction dans ArchivedPayment."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, transaction_id):
        try:
            obj = PaymentTransaction.objects.get(transaction_id=transaction_id)
        except PaymentTransaction.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Transaction PAYMENT introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != PaymentTransaction.STATUS_EXECUTED:
            return Response(
                {
                    "error": "invalid_state",
                    "error_description": f"Seul un paiement EXECUTED peut être traité/archivé (statut actuel: '{obj.status}').",
                },
                status=status.HTTP_409_CONFLICT,
            )

        if ArchivedPayment.objects.filter(transaction_id=transaction_id).exists():
            return Response(
                {"error": "already_archived", "error_description": "Ce paiement est déjà archivé."},
                status=status.HTTP_409_CONFLICT,
            )

        token = _stt_bearer_token(request)
        if not token:
            return Response(
                {"error": "invalid_request", "error_description": "Authorization: Bearer <token STT> requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            upstream = requests.get(
                f"http://192.168.10.15:7050/stt/notification/PAYMENT/{transaction_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except requests.RequestException as exc:
            return Response(
                {"error": "upstream_unreachable", "error_description": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            stt_data = upstream.json()
        except ValueError:
            stt_data = {"raw": upstream.text}

        if not (200 <= upstream.status_code < 300):
            return Response(
                {
                    "error": "stt_error",
                    "error_description": "Le STT n'a pas confirmé ce paiement.",
                    "sttResponse": stt_data,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        amount = stt_data.get("amount", obj.amount)
        try:
            amount = decimal.Decimal(str(amount)) if amount is not None else None
        except decimal.InvalidOperation:
            amount = obj.amount

        archive = ArchivedPayment.objects.create(
            transaction_id=obj.transaction_id,
            tax_payer_iban=stt_data.get("taxPayerIban") or obj.tax_payer_iban,
            tax_payer_legal_name=stt_data.get("taxPayerLegalName") or obj.tax_payer_legal_name,
            nif=stt_data.get("nif") or obj.nif,
            ntd=stt_data.get("ntd") or obj.ntd,
            tax_payer_phone_number=stt_data.get("taxPayerPhoneNumber") or obj.tax_payer_phone_number,
            amount=amount,
            operation_date=obj.operation_date,
            transaction_id_bank=obj.transaction_id_bank,
            status=PaymentTransaction.STATUS_EXECUTED,
            stt_response=stt_data,
        )

        return Response(
            {
                "notificationType": "PAYMENT",
                "transactionId": str(archive.transaction_id),
                "message": "Paiement traité et archivé avec succès.",
                "payment": ArchivedPaymentSerializer(archive).data,
            },
            status=status.HTTP_200_OK,
        )


class ArchivedPaymentListView(APIView):
    """GET /api/payments/archive/ — liste des paiements archivés."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        archives = ArchivedPayment.objects.all().order_by("-archived_at")
        return Response(ArchivedPaymentSerializer(archives, many=True).data, status=status.HTTP_200_OK)


class ArchivedPaymentDetailView(APIView):
    """GET /api/payments/archive/{transactionId}/ — détail d'un paiement archivé."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, transaction_id):
        try:
            obj = ArchivedPayment.objects.get(transaction_id=transaction_id)
        except ArchivedPayment.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Paiement introuvable dans l'archive."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ArchivedPaymentSerializer(obj).data, status=status.HTTP_200_OK)


# class CreatePaymentTestView(APIView):
#     """POST /api/test/payments/ — crée une transaction PAYMENT (status=PENDING)."""

#     authentication_classes = []
#     permission_classes = [permissions.AllowAny]

#     def get(self, request):
#         """GET /api/test/payments/ — liste transactionId + type + status (+ motif si rejeté)."""
#         data = []
#         for obj in PaymentTransaction.objects.all().order_by("-operation_date"):
#             item = {
#                 "transactionId": str(obj.transaction_id),
#                 "operationType": "PAYMENT",
#                 "status": obj.status,
#             }
#             if obj.status == PaymentTransaction.STATUS_REJECTED:
#                 item["rejectionReasonCode"] = obj.rejection_reason_code
#                 item["rejectionReasonLabel"] = REJECTION_REASONS.get(
#                     obj.rejection_reason_code, "Motif inconnu"
#                 )
#             data.append(item)
#         return Response(data, status=status.HTTP_200_OK)

#     def post(self, request):
#         serializer = PaymentCreateSerializer(data=request.data)
#         serializer.is_valid(raise_exception=True)
#         obj = serializer.save()
#         return Response(
#             {
#                 "notificationType": "PAYMENT",
#                 "transactionId": str(obj.transaction_id),
#                 "message": "Transaction créée. La banque doit maintenant appeler "
#                            "GET /notification/PAYMENT/{transactionId} puis POST /update.",
#             },
#             status=status.HTTP_201_CREATED,
#         )

class RejectionReasonsView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(REJECTION_REASONS, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# III.2 / IV.2 — GET /notification/{ACCOUNT|PAYMENT}/{transactionId}
# ---------------------------------------------------------------------------
class AccountNotificationDetailView(APIView):
    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, transaction_id):
        try:
            obj = AccountTransaction.objects.get(transaction_id=transaction_id)
        except AccountTransaction.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Transaction ACCOUNT introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(AccountDetailSerializer(obj).data, status=status.HTTP_200_OK)


class PaymentNotificationDetailView(APIView):
    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, transaction_id):
        try:
            obj = PaymentTransaction.objects.get(transaction_id=transaction_id)
        except PaymentTransaction.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Transaction PAYMENT introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(PaymentDetailSerializer(obj).data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# III.3 / IV.3 / IV.4 — POST /update
# ---------------------------------------------------------------------------
class UpdateView(APIView):
    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = UpdateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        notification_type = payload["notificationType"]
        if notification_type == "ACCOUNT":
            return self._handle_account(payload)
        return self._handle_payment(payload)

    # -- ACCOUNT: PENDING -> AUTHORIZED | REJECTED -----------------------
    def _handle_account(self, payload):
        transaction_id = payload["transactionId"]
        new_status = payload["status"]
        rejection_code = payload.get("rejectionReasonCode", "")

        try:
            obj = AccountTransaction.objects.get(transaction_id=transaction_id)
        except AccountTransaction.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Transaction ACCOUNT introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if new_status not in (AccountTransaction.STATUS_AUTHORIZED, AccountTransaction.STATUS_REJECTED):
            return Response(
                {"error": "invalid_status", "error_description": "status doit être AUTHORIZED ou REJECTED."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if obj.status != AccountTransaction.STATUS_PENDING:
            return Response(
                {
                    "error": "invalid_state",
                    "error_description": f"Cette transaction a déjà été traitée (statut actuel: {obj.status}).",
                },
                status=status.HTTP_409_CONFLICT,
            )

        if new_status == AccountTransaction.STATUS_REJECTED:
            if not rejection_code:
                return Response(
                    {"error": "missing_field", "error_description": "rejectionReasonCode est requis quand status=REJECTED."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if rejection_code not in REJECTION_REASONS:
                return Response(
                    {"error": "invalid_rejection_code", "error_description": f"Code de rejet '{rejection_code}' inconnu."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj.rejection_reason_code = rejection_code

        obj.status = new_status
        obj.save()

        return Response(
            {"transactionId": str(obj.transaction_id), "notificationType": "OperationUpdate"},
            status=status.HTTP_200_OK,
        )

    # -- PAYMENT: PENDING -> IN_PROGRESS -> EXECUTED | REJECTED ----------
    def _handle_payment(self, payload):
        transaction_id = payload["transactionId"]
        new_status = payload["status"]
        rejection_code = payload.get("rejectionReasonCode", "")
        transaction_id_bank = payload.get("transactionIdBank", "")

        try:
            obj = PaymentTransaction.objects.get(transaction_id=transaction_id)
        except PaymentTransaction.DoesNotExist:
            return Response(
                {"error": "not_found", "error_description": "Transaction PAYMENT introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        valid_statuses = (
            PaymentTransaction.STATUS_IN_PROGRESS,
            PaymentTransaction.STATUS_EXECUTED,
            PaymentTransaction.STATUS_REJECTED,
        )
        if new_status not in valid_statuses:
            return Response(
                {"error": "invalid_status", "error_description": "status doit être IN_PROGRESS, EXECUTED ou REJECTED."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Étape 3 : Initiation -> IN_PROGRESS (uniquement depuis PENDING)
        if new_status == PaymentTransaction.STATUS_IN_PROGRESS:
            if obj.status != PaymentTransaction.STATUS_PENDING:
                return Response(
                    {
                        "error": "invalid_state",
                        "error_description": f"Impossible de passer en IN_PROGRESS depuis le statut '{obj.status}'.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            obj.status = PaymentTransaction.STATUS_IN_PROGRESS
            obj.save()
            return Response(
                {"transactionId": str(obj.transaction_id), "notificationType": "OperationUpdate"},
                status=status.HTTP_200_OK,
            )

        # Étape 4 : Validation -> EXECUTED | REJECTED (uniquement depuis IN_PROGRESS)
        if obj.status != PaymentTransaction.STATUS_IN_PROGRESS:
            return Response(
                {
                    "error": "invalid_state",
                    "error_description": f"La validation nécessite le statut IN_PROGRESS (statut actuel: '{obj.status}').",
                },
                status=status.HTTP_409_CONFLICT,
            )

        if new_status == PaymentTransaction.STATUS_EXECUTED:
            if not transaction_id_bank:
                return Response(
                    {"error": "missing_field", "error_description": "transactionIdBank est requis quand status=EXECUTED."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj.transaction_id_bank = transaction_id_bank
        else:  # REJECTED
            if not rejection_code:
                return Response(
                    {"error": "missing_field", "error_description": "rejectionReasonCode est requis quand status=REJECTED."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if rejection_code not in REJECTION_REASONS:
                return Response(
                    {"error": "invalid_rejection_code", "error_description": f"Code de rejet '{rejection_code}' inconnu."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj.rejection_reason_code = rejection_code

        obj.status = new_status
        obj.save()

        return Response(
            {"transactionId": str(obj.transaction_id), "notificationType": "OperationUpdate"},
            status=status.HTTP_200_OK,
        )



# ----------------
from rest_framework.decorators import api_view
def _forward_headers(request):
    """Récupère le header Authorization du frontend pour le relayer au STT."""
    auth = request.headers.get('Authorization')
    headers = {}
    if auth:
        headers['Authorization'] = auth
    return headers


@api_view(['GET'])
def get_notification(request, notif_type, transaction_id):
    """
    Proxy vers GET /notification/{ACCOUNT|PAYMENT}/{transactionId}
    """
    if notif_type not in ('ACCOUNT', 'PAYMENT'):
        return Response({'error': 'Type invalide'}, status=status.HTTP_400_BAD_REQUEST)

    url = f"{settings.STT_API_BASE_URL}/notification/{notif_type}/{transaction_id}"

    try:
        resp = requests.get(url, headers=_forward_headers(request), timeout=15)
    except requests.RequestException as exc:
        return Response(
            {'error': 'Impossible de contacter le STT', 'detail': str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        data = resp.json()
    except ValueError:
        data = {'raw': resp.text}

    return Response(data, status=resp.status_code)

def _sync_local_status(request_body, stt_status_code):
    """Répercute localement la décision une fois que le vrai STT a répondu 2xx :
    la transaction ACCOUNT/PAYMENT correspondante passe au même status que celui
    envoyé dans la requête (AUTHORIZED/REJECTED, IN_PROGRESS/EXECUTED/REJECTED).
    Si la transaction n'existe pas en local, on ignore silencieusement (elle a
    pu être créée directement côté STT sans passer par nos endpoints de test)."""
    if not (200 <= stt_status_code < 300):
        return

    transaction_id = request_body.get('transactionId')
    notification_type = request_body.get('notificationType')
    new_status = request_body.get('status')
    if not transaction_id or not notification_type or not new_status:
        return

    if notification_type == 'ACCOUNT':
        obj = AccountTransaction.objects.filter(transaction_id=transaction_id).first()
        if not obj:
            return
        obj.status = new_status
        obj.rejection_reason_code = request_body.get('rejectionReasonCode', obj.rejection_reason_code)
        obj.save()
    elif notification_type == 'PAYMENT':
        obj = PaymentTransaction.objects.filter(transaction_id=transaction_id).first()
        if not obj:
            return
        obj.status = new_status
        obj.rejection_reason_code = request_body.get('rejectionReasonCode', obj.rejection_reason_code)
        obj.transaction_id_bank = request_body.get('transactionIdBank', obj.transaction_id_bank)
        obj.save()


@api_view(['POST'])
def update_operation(request):
    """
    Proxy vers POST {STT_API_BASE_URL}/update
    Body attendu: { transactionId, notificationType, status, rejectionReasonCode? }

    Si le vrai STT répond 2xx, la transaction locale correspondante (créée via
    /api/test/accounts/ ou /api/test/payments/) est mise à jour avec le même
    status, pour que la liste locale reflète la décision prise.
    """
    url = f"{settings.STT_API_BASE_URL}/update"
    headers = _forward_headers(request)
    headers['Content-Type'] = 'application/json'

    try:
        resp = requests.post(url, json=request.data, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return Response(
            {'error': 'Impossible de contacter le STT', 'detail': str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        data = resp.json()
    except ValueError:
        data = {'raw': resp.text}

    _sync_local_status(request.data, resp.status_code)

    return Response(data, status=resp.status_code)
