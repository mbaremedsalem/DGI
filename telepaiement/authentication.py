from rest_framework import authentication, exceptions

from .models import AccessToken, IssuedSttToken


class AuthenticatedBank:
    """Petit wrapper pour exposer un objet 'request.user' compatible DRF
    (is_authenticated=True) tout en gardant accès au OAuthClient réel."""

    is_authenticated = True

    def __init__(self, oauth_client):
        self.oauth_client = oauth_client

    def __str__(self):
        return str(self.oauth_client)


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """
    Vérifie l'entête `Authorization: Bearer <token>` généré par
    POST /oauth/token (grant_type=client_credentials), conformément
    au chapitre II du cahier des charges.
    """

    keyword = "Bearer"

    def authenticate_header(self, request):
        # Force DRF à répondre 401 (au lieu de 403) quand aucun token n'est fourni.
        return self.keyword

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header:
            return None  # laisse DRF renvoyer 401 via IsAuthenticated

        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise exceptions.AuthenticationFailed(
                "Entête Authorization invalide. Format attendu: 'Bearer <token>'."
            )

        token_value = parts[1]
        try:
            token = AccessToken.objects.select_related("client").get(token=token_value)
        except AccessToken.DoesNotExist:
            raise exceptions.AuthenticationFailed("Token invalide ou inconnu.")

        if not token.is_valid:
            raise exceptions.AuthenticationFailed("Token expiré. Merci de regénérer un token via /oauth/token.")

        return (AuthenticatedBank(token.client), token)


class STTIssuedTokenAuthentication(authentication.BaseAuthentication):
    """
    Vérifie l'entête `Authorization: Bearer <access_token>` avec l'access
    token Keycloak renvoyé par POST /stt/token, et retrouve l'utilisateur
    local qui l'avait obtenu (voir IssuedSttToken, rempli par STTTokenView).
    """

    keyword = "Bearer"

    def authenticate_header(self, request):
        return self.keyword

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            return None  # laisse une autre authentification (ou 401) prendre le relais

        try:
            issued = IssuedSttToken.objects.select_related("user").get(token=parts[1])
        except IssuedSttToken.DoesNotExist:
            return None  # pas un token STT connu -> laisse DRF renvoyer 401

        if not issued.is_valid:
            raise exceptions.AuthenticationFailed("Token STT expiré. Merci de regénérer un token via /stt/token.")

        return (issued.user, issued)
