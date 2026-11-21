# Need Anything? — Version 3 (push-ready)

## Local Windows test

1. Install Python 3.11+ and open Command Prompt in this folder.
2. `python server.py`
3. Open http://localhost:8000. Lists work locally, but phone notifications require a deployed HTTPS URL.

## Push setup for HTTPS hosting

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Generate VAPID keys: `python generate_keys.py` (once). Save the printed PEM private key in a secure file outside the public app folder, e.g. `C:\secrets\vapid_private.pem` on Windows. **Never upload the private key to a public repository.**
3. Set environment variables on your host: `VAPID_PUBLIC_KEY` (generated base64 public key), `VAPID_PRIVATE_KEY` (absolute path to private PEM file), `VAPID_CONTACT_EMAIL` (your contact email, used only in push protocol). The email is not requested from app users.
4. Host `server.py` behind HTTPS with a persistent disk for SQLite. Ensure the host does not expose the private key or database.
5. On each phone, open the hosted app, join the shared list, and tap **Enable notifications**. Browser/device support varies; on iPhone, add the web app to the Home Screen before enabling web push.
6. Test by starting a trip from the other phone. Push delivery depends on device/browser permission and network; it is not guaranteed instantaneous.

## Privacy and limitations

No accounts or analytics. Lists, invitation secrets, and push subscription endpoints/keys are stored in SQLite on the server. The hosting provider may process IP addresses. The invite link grants edit access to anyone who has it. There is no revocation, list deletion, or retention policy yet; do not publicly launch without these protections. The list updates by polling every 2.5 seconds while open. Web push requires HTTPS and browser permission. This package is not yet deployed or independently tested on physical phones.
