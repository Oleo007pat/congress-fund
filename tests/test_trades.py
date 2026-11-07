import requests

resp = requests.get(
    "https://www.bargo.ai/free-apis/congress/v1/trades",
    params={"ticker": "NVDA", "limit": 3},
    timeout=15,
)
print("Statuskode:", resp.status_code)
if resp.status_code == 200:
    print(str(resp.json())[:800])
else:
    print("Fejl:", resp.text[:200])