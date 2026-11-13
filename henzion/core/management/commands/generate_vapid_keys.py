import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Command(BaseCommand):
    help = "Create the VAPID key pair used to send push notifications. Paste the two lines into .env."

    def handle(self, *args, **options):
        key = ec.generate_private_key(ec.SECP256R1())
        private = _b64(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        public = _b64(key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        self.stdout.write("Add these to your .env file (keep the private key secret, and never change it later\n"
                          "or every phone will have to turn notifications on again):\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write("VAPID_CONTACT=mailto:you@example.com")
