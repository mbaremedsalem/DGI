from django.core.management.base import BaseCommand
from telepaiement.models import OAuthClient


class Command(BaseCommand):
    help = "Crée un client OAuth2 de démo (client_id=AUB / client_secret=secret)"

    def handle(self, *args, **kwargs):
        client, created = OAuthClient.objects.get_or_create(
            client_id="AUB",
            defaults={"client_secret": "secret_aub", "bank_name": "Banque de Test SA"},
        )
        if not created:
            client.client_secret = "secret_aub"
            client.save()
        self.stdout.write(self.style.SUCCESS(
            f"Client OAuth prêt -> client_id='{client.client_id}' client_secret='{client.client_secret}'"
        ))
