from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import authenticate
from .authentication import BearerTokenAuthentication
import requests
from .models import (
    AccessToken, AccountTransaction, OAuthClient, PaymentTransaction, REJECTION_REASONS,
)
from .serializers import (
    AccountCreateSerializer, AccountDetailSerializer,
    PaymentCreateSerializer, PaymentDetailSerializer,
    UpdateRequestSerializer,
)
from django.conf import settings

# ---------------------------------------------------------------------------
# II. Autorisations d'accès à l'API — POST /oauth/token
# ---------------------------------------------------------------------------
import base64
import binascii

import base64
import binascii

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

# Adaptez ces imports à votre projet
# from .models import OAuthClient, AccessToken


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
        return Response({"accessToken": token.token}, status=status.HTTP_200_OK)




# ---------------------------------------------------------------------------
# Client OAuth2 côté banque — POST /stt/token
# 1) Vérifie que le client (username/password) existe dans notre base locale.
# 2) Si oui, récupère un access token auprès du vrai serveur STT/DGI (Keycloak)
#    avec le couple client_id/client_secret attribué à cette banque (settings.STT_*)
#    et renvoie tel quel le JSON retourné par Keycloak.
# 3) Si le client n'existe pas / mauvais mot de passe, renvoie une erreur typée.
# ---------------------------------------------------------------------------
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

        if not settings.STT_CLIENT_ID or not settings.STT_CLIENT_SECRET:
            return Response(
                {
                    "error": "server_error",
                    "error_description": "STT_CLIENT_ID / STT_CLIENT_SECRET non configurés côté serveur.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
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
            return Response(
                {"error": "upstream_unreachable", "error_description": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            payload = upstream.json()
        except ValueError:
            payload = {"error": "upstream_invalid_response", "error_description": upstream.text}

        return Response(payload, status=upstream.status_code)


# ---------------------------------------------------------------------------
# Endpoints de TEST (hors spec) permettant de simuler un contribuable qui
# ajoute un compte, ou confirme un ordre de paiement sur STT. C'est ce qui,
# dans le vrai système, déclenche la notification POST vers la banque.
# ---------------------------------------------------------------------------

class CreateAccountTestView(APIView):
    """POST /api/test/accounts/ — crée une transaction ACCOUNT (status=PENDING).

    Body attendu:
    {
        "transactionId": "3a957d71-81e2-49bf-ae7a-3d3119af37e6",
        "notificationType": "ACCOUNT"
    }
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        """GET /api/test/accounts/ — liste transactionId + type + status de chaque compte créé."""
        data = [
            {
                "transactionId": str(obj.transaction_id),
                "operationType": "ACCOUNT",
                "status": obj.status,
            }
            for obj in AccountTransaction.objects.all().order_by("-operation_date")
        ]
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
        """GET /api/test/payments/ — liste transactionId + type + status de chaque paiement créé."""
        data = [
            {
                "transactionId": str(obj.transaction_id),
                "operationType": "PAYMENT",
                "status": obj.status,
            }
            for obj in PaymentTransaction.objects.all().order_by("-operation_date")
        ]
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
    #authentication_classes = []
    #permission_classes = [permissions.AllowAny]

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
    #authentication_classes = []
    #permission_classes = [permissions.AllowAny]

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
    #authentication_classes = []
    #permission_classes = [permissions.AllowAny]

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


















import os
import requests
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# Pas de dépendance à settings.py — lu directement depuis l'environnement
# (déjà chargé via load_dotenv dans settings.py), avec fallback en dur.
STT_API_BASE_URL = os.environ.get('STT_API_BASE_URL', 'https://api.stt-pp.e-tax.impots.gov.mr')


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
    Proxy vers GET {STT_API_BASE_URL}/notification/{ACCOUNT|PAYMENT}/{transactionId}
    """
    if notif_type not in ('ACCOUNT', 'PAYMENT'):
        return Response({'error': 'Type invalide'}, status=status.HTTP_400_BAD_REQUEST)

    url = f"{STT_API_BASE_URL}/notification/{notif_type}/{transaction_id}"

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


#@api_view(['POST'])
#def update_operation(request):
    """
    Proxy vers POST {STT_API_BASE_URL}/update
    Body attendu: { transactionId, notificationType, status, rejectionReasonCode? }
    """
#    url = f"{STT_API_BASE_URL}/update"
#    headers = _forward_headers(request)
#    headers['Content-Type'] = 'application/json'

#    try:
#        resp = requests.post(url, json=request.data, headers=headers, timeout=15)
#    except requests.RequestException as exc:
#        return Response(
#            {'error': 'Impossible de contacter le STT', 'detail': str(exc)},
#            status=status.HTTP_502_BAD_GATEWAY,
#        )

#    try:
#        data = resp.json()
#    except ValueError:
#        data = {'raw': resp.text}

#    return Response(data, status=resp.status_code)


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
    url = f"{STT_API_BASE_URL}/update"
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




