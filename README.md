# Congress-fonden

Undersøger, om man kan tjene et overnormalt afkast ved at kopiere aktiehandler fra medlemmer
af Repræsentanternes Hus (offentliggjort under STOCK Act). Kun paper trading og sandbox, ingen rigtige penge.

## Mappestruktur

| Mappe | Indhold |
|---|---|
| `ingestion/` | Henter og læser indberetninger (`build_house_trades.py`, `house_ptr_parser.py`) og nylige handler via Bargo (`bargo_feed.py`) |
| `backtest/` | Historiske test: skip-regel (`backtest_delay.py`), top-medlemmer og udvalg (`backtest_members.py`), rebalanceringsmodel |
| `fund/` | Fondens drift: investor-regnskab, gebyrer, handelslogik og broker (endnu ikke koblet til data) |
| `tests/` | Forbindelsestest til Alpaca, Plaid, Congress.gov og Bargo |
| `data/` | Handelsdata, kursdata (cache) og referencedata om udvalg, medlemmer og sektorer |
| `reports/` | Resultater fra backtests |
| `docs/` | Oprindelig arkitektur (`ARCHITECTURE.md`) |
| roden | `docker-compose.yml` og `schema.sql` (lokal database), `.env` (nøgler, aldrig i Git) |

## Sådan kører du (fra projektmappen)

```
docker compose up -d                                   # start databasen
python3 ingestion/build_house_trades.py 2021-2026      # hent/læs indberetninger -> data/house_trades.csv
python3 backtest/backtest_delay.py                     # skip-regel -> reports/backtest_report.txt
python3 backtest/backtest_members.py                   # top-medlemmer og udvalg -> reports/members_report.txt
python3 tests/test_alpaca.py                           # tjek en forbindelse
```

## Status (7. november 2026)

**Data:** 23.177 aktiehandler fra 190 medlemmer, 2021-2026, læst fra Husets officielle PDF'er.
Ca. 14 medlemmer indleverer kun på papir (scannet) og mangler, bl.a. Rohit Khanna og Michael McCaul.
Senatet er ikke med.

**Resultater indtil videre (overnormalt afkast mod S&P 500 over 3 måneder, efter omkostninger):**
- At kopiere alle køb slår ikke markedet (ca. -0,5 % i kalibrering, ca. 0 % i det rene testår).
- Skip-regel på kursstigning: ingen grænse forbedrer resultatet pålideligt.
- Indberetninger efter 45 dage klarer sig dårligst (ca. -2,6 %).
- At følge de historisk bedste medlemmer virker ikke stabilt: god historik forudsiger ikke gode næste handler.
- Udvalgsrelevante køb ser dårligere ud, men resultatet er domineret af to medlemmer og forstyrret af,
  at S&P 500 blev trukket af tech, mens fx finans gik dårligere.

**Næste mulige skridt:**
1. Mål afkast mod sektorindeks (fx XLF for banker) i stedet for S&P 500, så sektoreffekter fjernes.
2. Tekstgenkendelse (OCR) af de scannede indberetninger, eller en anden kilde til papirindleverere og Senatet.
3. Kun hvis en regel holder i den rene test: koble `fund/` til data og kør paper trading i flere måneder.

**Forbehold:** Ikke juridisk eller finansiel rådgivning. En rigtig fond kræver registrering og
KYC/AML-afklaring med en advokat, før der tages imod penge fra andre.
