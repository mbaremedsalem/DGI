from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from telepaiement.models import UserProfile


class Command(BaseCommand):
    help = "Crée le client local de démo pour /stt/token (username=10016 / password=pass1234)"

    def handle(self, *args, **kwargs):
        user, created = User.objects.get_or_create(username="10016")
        user.set_password("pass1234")
        user.save()
        UserProfile.objects.get_or_create(user=user, defaults={"role": UserProfile.ROLE_ADMIN})
        action = "créé" if created else "mis à jour"
        self.stdout.write(self.style.SUCCESS(f"Client STT local {action} -> username='10016' password='pass1234'"))
