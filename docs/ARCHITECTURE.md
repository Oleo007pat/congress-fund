# Congress-Tracking Investeringsfond — Systemarkitektur

> **Juridisk forbehold:** Dette er en teknisk arkitektur, ikke juridisk eller finansiel
> rådgivning. Drift af en fond kræver registrering som investeringsrådgiver/forvalter
> og overholdelse af værdipapirlovgivning i relevante jurisdiktioner.

## 1. Overordnet dataflow

```
┌─────────────────┐   ┌──────────────────┐   ┌────────────────────┐
│ Disclosure-kilder│──▶│ Ingestion Layer  │──▶│ PostgreSQL (kerne-DB)│
│ (Senate/House/   │   │ (scraper+parser) │   └─────────┬──────────┘
│  3.-parts API)   │   └──────────────────┘             │
└─────────────────┘                                     │
┌─────────────────┐   ┌──────────────────┐              │
│ Committee-data   │──▶│ Committee Filter │──────────────┤
│ (Congress.gov API)│   └──────────────────┘             │
└─────────────────┘                                     ▼
┌─────────────────┐   ┌──────────────────┐   ┌────────────────────┐
│ Marketdata       │──▶│ Delay/Skip-logik │──▶│ Order Engine        │
│ (Polygon/IEX)    │   │ + Rebalancerings-│   │ (Alpaca/IBKR API)   │
└─────────────────┘   │ model            │   └─────────┬──────────┘
                       └──────────────────┘             │
┌─────────────────┐                                     ▼
│ Bank (Plaid)     │──▶ ACH-indbetaling ──▶ Investor Ledger + Fee Engine
└─────────────────┘
```

## 2. Datakilder

| Kilde | Formål | Bemærkning |
|---|---|---|
| Senate eFD (efdsearch.senate.gov) | Periodic Transaction Reports (PTR) | Kræver PDF/HTML-parsing, ingen officiel API |
| House Clerk Financial Disclosures | PTR for Repræsentanternes Hus | Samme udfordring som Senatet |
| 3.-parts aggregator (fx Quiver Quantitative, Unusual Whales, CapitolTrades) | Struktureret, normaliseret trade-feed | **Anbefalet primærkilde** — sparer scraping/parsing af PDF'er, giver hurtigere opdatering |
| Congress.gov API / ProPublica Congress API | Udvalgsmedlemskab, komité-historik | Officiel, stabil, gratis m. API-nøgle |
| Markedsdata (Polygon.io, IEX Cloud, Alpaca Market Data) | Priser, volumen, volatilitet | Bruges til delay-filter og slippage-modellen |
| Plaid | Bankkonto-forbindelse, ACH-indbetaling/udbetaling | Investor on-boarding |
| Alpaca (eller Interactive Brokers) | Ordre-eksekvering | Broker-API til faktiske handler |

**Anbefaling:** Byg primær ingestion mod en aggregator-API (struktureret JSON) og behold
en fallback-scraper mod de officielle kilder som verifikationslag — rå PDF-parsing er
skrøbeligt og bør ikke være single point of failure.

## 3. Databasedesign (uddrag — fuld skema i `schema.sql`)

- `members` — kongresmedlemmer (id, navn, kammer, stat, parti)
- `committees` / `member_committees` — udvalgsmedlemskab m. start/slut-dato
- `disclosures` — rå indberettede handler (transaktionsdato, indberetningsdato, ticker, beløbsinterval)
- `performance_scores` — løbende beregnet historisk afkast pr. medlem (rullende 3-års vindue)
- `investors` / `investor_ledger` — indskud, hævninger, ejerandele
- `fund_nav` — daglig NAV og enhedspris
- `fees` — månedlig forvaltningsgebyr-log
- `positions` / `orders` — fondens faktiske beholdninger og ordrehistorik

## 4. API-integrationer

- **Plaid**: `/link/token/create` → `/item/public_token/exchange` → `/transfer/*` for ACH.
- **Alpaca**: `/v2/orders` for eksekvering, `/v2/positions` for beholdningsstyring, market data websocket til real-time priser.
- **Congress.gov / ProPublica**: periodisk (dagligt) job der opdaterer `member_committees`.
- **Aggregator-API**: polling hvert 15-60 min for nye PTR-indberetninger.

## 5. Behovsanalyse — Algoritmisk optimering (leverance 4)

Dette er kernen i systemets risiko-styring, og kræver tre uafhængige beslutningslag:

### 5.1 Behov: Optimal rebalanceringsfrekvens
**Problem:** For hyppig rebalancering → transaktionsomkostninger/slippage æder afkast.
For sjælden → fonden "drifter" væk fra kongresmedlemmets faktiske portefølje, og
tracking error stiger.
**Løsning:** EOQ-inspireret model (se `quant/rebalancing_model.py`) der balancerer
forventet drift-omkostning mod transaktionsomkostning og udregner en optimal
rebalanceringsinterval `f*`.

### 5.2 Behov: Håndtering af 45-dages STOCK Act-forsinkelse
**Problem:** Man handler ikke på transaktionsdatoen, men op til 45 dage senere —
prisen kan allerede have bevæget sig markant, hvilket ændrer risk/reward fundamentalt
ift. medlemmets oprindelige handel.
**Løsning:** `trade_logic.py` beregner prisbevægelse siden `transaction_date` og
sammenligner med en tærskelværdi. Hvis bevægelsen overstiger tærsklen, eller
volatiliteten i mellemtiden er unormalt høj, markeres handlen som `SKIP` fremfor
`EXECUTE`.

### 5.3 Behov: Skip-logik ved høj volatilitet/allerede-realiseret gevinst
**Problem:** At følge en handel blindt uden hensyn til nuværende markedsforhold er
ren "stale-signal"-risiko.
**Løsning:** Kombineret filter: `price_drift_pct > θ_price` ELLER
`realized_volatility > θ_vol` ⇒ skip. Tærskelværdier bør kalibreres empirisk
(backtesting) snarere end fastsættes vilkårligt — foreslået startpunkt: θ_price = 8-10%,
θ_vol = 2x 90-dages gennemsnitlig volatilitet for aktivet.

## 6. Kørselscyklus (høj-niveau pseudo-orkestrering)

1. **Daglig**: hent nye disclosures → filtrér på udvalgsrelevans → generér `TradeSignal`
2. **Pr. signal**: kør delay/skip-filter → hvis godkendt, send ordre til Alpaca
3. **Månedlig**: beregn NAV, træk 1bp forvaltningsgebyr, opdater investor-ledger
4. **Kvartalsvis**: genberegn "bedst præsterende medlem" (rullende 3-års performance) og verificér udvalgsmedlemskab
