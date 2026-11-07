import os
import requests
from dotenv import load_dotenv

load_dotenv()

resp = requests.post(
    "https://sandbox.plaid.com/sandbox/public_token/create",
    json={
        "client_id": os.getenv("PLAID_CLIENT_ID"),
        "secret": os.getenv("PLAID_SECRET"),
        "institution_id": "ins_109508",
        "initial_products": ["auth"],
    },
    timeout=10,
)
print("Statuskode:", resp.status_code)
data = resp.json()
if resp.status_code == 200:
    print("Plaid sandbox virker, testbank oprettet.")
else:
    print("Fejl:", data.get("error_code"), "-", data.get("error_message"))