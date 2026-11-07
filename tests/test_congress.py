import os
import requests
from dotenv import load_dotenv

load_dotenv()

resp = requests.get(
    "https://api.congress.gov/v3/member",
    headers={"X-Api-Key": os.getenv("CONGRESS_GOV_API_KEY")},
    params={"limit": 1, "format": "json"},
    timeout=10,
)
print("Statuskode:", resp.status_code)
if resp.status_code == 200:
    members = resp.json().get("members", [])
    print("Congress.gov virker, medlemmer returneret:", len(members))
else:
    print("Fejl:", resp.text[:200])