"""Generate VAPID keypair. Run once; keep the private key secret."""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
key=ec.generate_private_key(ec.SECP256R1())
private=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
public=key.public_key().public_numbers()
raw=b'\x04'+public.x.to_bytes(32,'big')+public.y.to_bytes(32,'big')
print('VAPID_PUBLIC_KEY='+base64.urlsafe_b64encode(raw).decode().rstrip('='))
print('VAPID_PRIVATE_KEY (save this text as vapid_private.pem and set its path in VAPID_PRIVATE_KEY):')
print(private)
