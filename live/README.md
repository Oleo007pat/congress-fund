# Papirfonden (live paper fund)

Hver hverdag finder `python3 -m live.run_daily` nye indberetninger (PTR) fra Repræsentanternes Hus, anvender den låste regel i `live/strategy.json` og handler på brugerens **Alpaca PAPER-konto** (fiktive penge). Samtidig skrives fondens udvikling mod SPY til `docs/data/live.json`, som den statiske side læser. Kørslen sker via GitHub Actions (`.github/workflows/paper_fund.yml`).

Papirfond med fiktive penge. Ikke investeringsrådgivning.

## Filer

| Fil | Indhold |
|---|---|
| `live/run_daily.py` | Hovedprogram (sikkerhed, nye indberetninger, signaler, ordrer, optegnelser, site-JSON) |
| `live/strategy.json` | Den låste regel (udskiftelig uden kodeændringer) |
| `live/strategy.py` | Indlæsning og validering af reglen, hash, dansk beskrivelse |
| `live/house.py` | House Clerk: indeks, nye filings, PDF-download, parsing (genbruger `ingestion/`) |
| `live/broker.py` | Alpaca paper (konto, ur, ordrer, aktiver) og kursdata (genbruger `prices/alpaca_client.py`) |
| `live/signals.py` | Referencekurs, skip-regel, størrelse, lofter (genbruger `src/backtest/engine.py`) |
| `live/execution.py` | Handelsvinduer, idempotente ordrer, fills, lots, exits |
| `live/member_filter.py` | Valgfri medlemsfilter (hook, slået fra) |
| `live/site.py` | Bygger `docs/data/live.json` |
| `data/live/state.json` | Startdato, start-egenkapital, sete doc_ids, lots, afventende ordrer |
| `data/live/history.csv` | Én række pr. kørselsdato: date, equity, cash, n_positions, spy_close, spy_close_date |
| `data/live/trades_log.csv` | Alle signaler (taken/skipped/ignored + årsag) og alle ordrer og fills |
| `data/live/parsed_trades.csv` | Alle læste handler fra fondens indberetninger (til dubletter og medlems-salg) |
| `data/live/cache/` | Hentede PDF'er og udtrukket tekst. Committes aldrig (`.gitignore`) |

## Sådan køres det lokalt

```
pip install -r live/requirements.txt
# .env i projektmappen (aldrig i Git):
#   ALPACA_KEY_ID=...  ALPACA_SECRET_KEY=...  ALPACA_BASE_URL=https://paper-api.alpaca.markets
python3 -m live.run_daily --dry-run --start-date 2026-11-18     # først: ingen ordrer
python3 -m live.run_daily --start-date 2026-11-18               # rigtig kørsel (papirpenge)
```

* **Dry-run** sender ingen ordrer. Alt skrives i en sandkasse (`data/live/dryrun/` og `docs/data/live_dryrun.json`), som starter som en kopi af de rigtige filer. En dry-run kan derfor aldrig forbruge indberetninger eller ændre den rigtige fond. Sandkassen committes ikke.
* `--start-date` bruges kun første gang og låses i `state.json`. Standard er dags dato. Kun indberetninger med filing-dato fra startdatoen og frem handles.
* Første rigtige kørsel afvises, hvis paperkontoen indeholder positioner eller åbne ordrer (kontoen skal være dedikeret og nulstillet). Overstyres med `--allow-nonempty-account` (anbefales ikke).
* Startkapitalen er kontoens egenkapital ved første kørsel. Nulstil paperkontoen til det ønskede beløb først.

Afslutningskoder: 0 ok, 1 uventet fejl, 2 afvist (ikke paper-URL, manglende nøgler, ugyldig strategi, ikke-tom konto), 3 `house_unreachable`, 4 Alpaca-fejl.

Tests: `python3 -m unittest tests.test_live`

## Sikkerhed

* Kørslen **afvises**, medmindre `ALPACA_BASE_URL` er præcis `https://paper-api.alpaca.markets` (værten kontrolleres nøjagtigt; `/v2` tillades; look-alike værter, `http`, brugernavn i URL og andre porte afvises). Kontrollen sker før noget netværkskald, og `Broker` kontrollerer den igen.
* Nøgler læses kun fra miljøet (lokalt via `python-dotenv`), printes eller logges aldrig, og fejlbeskeder renses for nøgleværdier.
* Kun kontanter: ingen gearing, ingen shortsalg (salg er altid begrænset til lottets antal og positionens ledige antal).

## Hvad der sker hver kørsel

1. Tjek paper-URL, indlæs strategi og state. Hent konto, ur og positioner fra Alpaca.
2. Synkroniser tidligere ordrer: fyldte køb bliver til lots (indgangsdato = fill-dato, fill-kurs fra Alpaca, planlagt exit = indgang + N måneder); fyldte salg lukker lots.
3. Hent House-indekset for indeværende år (også sidste år i januar). Vælg `FilingType = P` med filing-dato ≥ startdato og **før** handelsdagen, ordren sigter mod, som ikke er set før. Download og parse hver ny PDF med bagudrettet parser (`ingestion/`). Scannede/uden tekst logges "ikke læsbar" og springes over (DocID der starter med 8 eller 9 hentes ikke engang).
   * Hvis house.gov ikke kan nås eller blokerer: `last_run.status = "house_unreachable"`, exitkode 3, **ingen ordrer** (heller ikke exits).
4. Signaler og ordrer (se nedenfor). 5. Exits. 6. Optegnelser og site-JSON.

### Regel (v1, låst)

Støtte: aktier og ETF'er, alle ejere. Udelukket: short-relateret, option-lignende, ikke-diskretionære (nøgleordslisten i `src/backtest/engine.py`), dubletter og mistænkelig dato (`date_suspect`) — alt via backtestens funktioner (`include_decision`, `finalize_trades`, `select_buy_signals`, `select_sales`, `amount_weight`, `skip_decision`).

* **Skip kun ved stigning:** køb springes over, hvis kursen er steget **mere end X %** (default 10) siden lukkekursen på medlemmets handelsdag (eller sidste gyldige lukkekurs før den, højst 7 dage gammel). Et fald giver aldrig skip. Præcis X tages.
* **Størrelse (beløbsvægtet):** `egenkapital × 1 % × clip(beløbets midtpunkt / 8.000,5; 0,25; 10)`. 8.000,5 er medianen af midtpunkterne for backtestens 19.413 beholdte køb (`strategy.json` angiver kilden). Ens vægtning: 1 % fast. Loft: højst 10 % af egenkapitalen pr. position (aktie, inkl. eksisterende position og afventende ordrer). Kontantgrænse: ordren plus 2 % buffer til åbningskurs-gap skal kunne være i kontanterne; ellers skaleres ned (logges), og er der ikke nok, springes signalet over.
* **Ordrer:** market-on-open (`opg`) til næste børsåbning (Alpacas ur/`next_open`; på helligdage målrettes næste handelsdag). `client_order_id = cf-{doc_id}-{hash af rækken}`. Før hver ordre tjekkes state, Alpaca-ordrer (på `client_order_id`) og dubletfejl, så en genkørsel aldrig køber to gange; en eksisterende ordre "adopteres" i state.
  * `orders.mode = auto` (default): prøv brøkdels-notional-ordre med `opg`; afvises den pga. tidsgyldighed/brøkdele, huskes det i `state.capabilities.notional_opg = false`, og ordren sendes som **hele aktier** (`floor(beløb / kurs)`, mindst 1). `whole_shares` og `notional` kan sættes fast i `strategy.json`.
* **Exit:** hold-reglerne sælger lottets antal med market-on-close (`cls`) på/efter planlagt exit-dato (første handelsdag på/efter indgang + N kalendermåneder, som backtesten). `member_sells`: salg med `opg` ved næste åbning efter, at medlemmets salg er indberettet. Er vinduet gået (kørslen for sen), udskydes til næste kørsel.
* Afviste ordrer, afnoterede/suspenderede papirer og tickers uden kurs logges og vælter ikke kørslen.

### Medlemsfilter (hook, slået fra)

`member_filter.enabled = false`. Når det slås til, bruges kun medlemmets **afsluttede, kopierede** handler i et glidende vindue: `score = sum(excess) / (n + K)` (skrumpet middel-overafkast mod SPY efter backtestens omkostningsmodel), og reglen er `score_gt_0` eller `top_percentile`. For få handler giver `skip` (default) eller `allow`. Koden er enhedstestet med en stub-historik; i produktion bygges historikken af fondens egne lukkede lots.

## Afvigelser fra backtesten

Hvor noget var tvetydigt, er den mest konservative læsning valgt.

1. **Indgangs- og sammenligningskurs.** Backtesten køber til *næste åbning* og sammenligner *åbningskursen* med referencekursen. Live skal beslutte *før* åbningen og bruger den **seneste tilgængelige kurs**: seneste daglige lukkekurs (Alpaca daglige bars, `adjustment=all`, feed `sip` til historik; falder permanent tilbage til `iex` ved 403) eller, hvis den er mere end 5 dage gammel, et iex-snapshot. Skip-testen er derfor en forhåndsvurdering; den faktiske åbningskurs kan afvige (gap). Referencekursen er den samme type som i backtesten (justeret for split og udbytte, F7 gælder stadig).
2. **Størrelse.** Backtesten er en kalendertidsportefølje med vægte, der ikke direkte kan handles. Live bruger procent af egenkapital med clip (se ovenfor), ingen rebalancering, kontant-begrænsning, 10 %-loft og evt. hele aktier (afrunding nedad, aktier dyrere end målbeløbet springes over).
3. **Omkostninger.** Backtesten trækker 0,1 % pr. side. Live bruger Alpacas faktiske paper-fills uden ekstra omkostningsmodel.
4. **Indgangsdato og -kurs** er den faktiske fill (dato og `filled_avg_price`), ikke "næste åbning" beregnet bagefter. Exit-datoen regnes fra fill-datoen.
5. **Tidspunkt.** Kun indberetninger med filing-dato **strengt før** handelsdagen (som "strengt efter filing_date" i backtesten). Indberetninger, hvor indgangen ville ligge mere end 7 kalenderdage efter filing-datoen (`entry.max_entry_lag_days`), ignoreres.
6. **Dubletter og ændringer** findes kun mod fondens egne læste indberetninger (ikke mod tiden før startdatoen). Derfor: en *Amended*-række uden kendt original ignoreres (backtesten tog den med); en senere *Deleted*-række mod en allerede afgivet ordre logges, men papirpositionen bevares. Medlemsnavne harmoniseres kun inden for fondens data.
7. **member_sells:** er medlemmets salg allerede indberettet ved signalet, ignoreres købet (backtesten laver en rundtur til 0 minus omkostninger).
8. **Tickere** findes via Alpacas aktivopslag (`prices/ticker_map.candidate_symbols` for skrivemåder), ikke via backtestens kursfiler: ingen `asof`-kobling af gamle tickere og ingen kobling af tickerskift. OTC og ikke-handelbare aktiver ignoreres.
9. **Scannede indberetninger** (24 % i backtesten) læses ikke og logges "ikke læsbar".
10. **Afnotering/halt.** Mangler positionen hos Alpaca ved exit, lukkes lottet uden ordre (`closed_external`); backtesten lukker til sidste kurs.
11. **Dry-run** skriver til en sandkasse (se ovenfor).
12. **Strategi-ændring.** Hashen af `strategy.json` gemmes; ændres filen efter start, registreres det i `state.strategy_changes`, og `live.json` viser `changed_since_start = true`.

## Skal verificeres ved første kørsel

Intet af dette har kunnet afprøves uden netværk; formerne følger Alpacas dokumentation.

* Kan GitHub-runnerne nå house.gov (ellers `house_unreachable`)?
* Accepterer Alpaca `opg`/`cls` for **brøkdels-notional**-ordrer? (Forventet nej; `auto` falder tilbage til hele aktier.) Accepteres `cls` for brøkdels-salg? (Ellers falder et sådant salg tilbage til en `day`-ordre.)
* Hvornår lukker Alpaca for `opg`-ordrer (ca. 09:28 ET), og accepteres de på helligdage? En afvist ordre logges som "ignored" med Alpacas fejltekst.
* Har kontoen SIP-historik (ellers bruges `iex`, og referencekurser kan afvige)?
* Svarformer: `/v2/clock`, `/v2/positions` (`qty_available`), `/v2/assets`, `/v2/stocks/snapshots`.
* GitHub-køer kan forsinke cron med minutter; kørslen udskyder selv nye ordrer, hvis åbningsvinduet er gået.
* Sommertid: 12:45 UTC er 8:45 ET om sommeren og 7:45 ET om vinteren; begge er før åbning.

## Skema for `docs/data/live.json`

```
schema_version      1
generated_at        ISO-tid (UTC)
start_date          "YYYY-MM-DD"
start_equity        tal (USD, kontoens egenkapital ved første kørsel)
last_run            { time_utc, status, filings_checked, new_signals, orders_sent, message, dry_run }
                    status: ok | house_unreachable | alpaca_error | error | dry_run
                    filings_checked = nye indberetninger undersøgt i denne kørsel
                    new_signals     = nye købssignaler, der bestod regelfiltrene (før skip/størrelse)
                    orders_sent     = ordrer afsendt og accepteret af Alpaca (køb og salg)
latest_equity       tal
curve               { dates: [...], fund: [...], spy: [...] }   begge normaliseret til 100 ved første punkt
total_return        { fund_pct, spy_pct, excess_pct_points }     siden start
positions           [ { ticker, member, owner, filed, entered, entry_price, current_price, return_pct, planned_exit } ]
decisions           [ { member, ticker, filed, decision, reason } ]   de seneste 25, nyeste først; decision: taken|skipped|ignored
strategy            { name, hash, changed_since_start, description (dansk), config }
disclaimer          "Papirfond med fiktive penge. Ikke investeringsrådgivning."
```

`positions[].current_price` kommer fra Alpacas positioner; `return_pct` er brutto (aktuel kurs / indgangskurs − 1). `planned_exit` er en dato eller "når medlemmet sælger".

## Skema for `trades_log.csv`

Kolonner: `ts_utc, kind (signal|order|fill|info), doc_id, member, owner, ticker, symbol, filing_date, txn_date, trade_type, amount_low, amount_high, decision, reason, ref_close, ref_date, last_price, rise_pct, side, tif, notional, qty, client_order_id, order_id, order_status, filled_qty, fill_price, fill_time, on_site, dry_run`. Fill-rækker (`kind = fill`) tilføjes den dag, Alpaca melder ordren fyldt.

## GitHub: hemmeligheder og indstillinger

* Settings, Secrets and variables, Actions, **Repository secrets**: `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (= `https://paper-api.alpaca.markets`).
* Settings, Actions, General, **Workflow permissions**: "Read and write permissions" (workflowet beder selv om `contents: write`, men repo/organisation kan begrænse det).
* Hvis `main` er beskyttet, skal Actions-botten kunne pushe direkte.
* Settings, Pages: kilde = branch + mappe `/docs`.
* Mapperne `src/`, `ingestion/`, `prices/` og `live/` skal ligge i repoet (workflowet bruger dem); `data/processed/trades.csv` og kursfiler behøves ikke.
