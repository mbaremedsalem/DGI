from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import BearerTokenAuthentication
from .models import (
    AccessToken, AccountTransaction, OAuthClient, PaymentTransaction, REJECTION_REASONS,
)
from .serializers import (
    AccountCreateSerializer, AccountDetailSerializer,
    PaymentCreateSerializer, PaymentDetailSerializer,
    UpdateRequestSerializer,
)


# ---------------------------------------------------------------------------
# II. Autorisations d'accès à l'API — POST /oauth/token
# ---------------------------------------------------------------------------
class TokenView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        grant_type = data.get("grant_type")
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")

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
            return Response(
                {"error": "invalid_client", "error_description": "client_id / client_secret invalide."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token = AccessToken.objects.create(client=client)
        return Response({"accessToken": token.token}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Endpoints de TEST (hors spec) permettant de simuler un contribuable qui
# ajoute un compte, ou confirme un ordre de paiement sur STT. C'est ce qui,
# dans le vrai système, déclenche la notification POST vers la banque.
# ---------------------------------------------------------------------------
class CreateAccountTestView(APIView):
    """POST /api/test/accounts/ — crée une transaction ACCOUNT (status=PENDING)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

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
