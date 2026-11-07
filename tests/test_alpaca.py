import os
import requests
from dotenv import load_dotenv

load_dotenv()

resp = requests.get(
    f"{os.getenv('ALPACA_BASE_URL')}/v2/account",
    headers={
        "APCA-API-KEY-ID": os.getenv("ALPACA_KEY_ID"),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY"),
    },
    timeout=10,
)
print("Statuskode:", resp.status_code)
data = resp.json()
print("Kontostatus:", data.get("status"))
print("Kontanter:", data.get("cash"))