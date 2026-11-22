# Resumé: Congress-fonden, backtest v1

*Status 17. november 2026*

## Hvad projektet er

Projektet undersøger, om man kan slå S&P 500 ved at kopiere de aktiekøb, som medlemmer af Repræsentanternes Hus indberetter efter STOCK Act (PTR-indberetninger). Der handles kun på information, der var offentlig:
- vi køber til åbningskursen dagen efter indleveringen;
- nogle køb springes over, hvis kursen allerede er steget mere end X %;
- positionen sælges efter en fast periode, eller når medlemmet selv sælger.

Der er kun tale om forskning og paper trading. Der er ingen rigtige penge involveret.

**Resultat:** strategien slår ikke SPY out-of-sample.
- Den valgte konfiguration (hold 6 mdr., beløbsvægtet, X = 10 %) gav +63,9 % mod SPY's +66,0 % i 2022-01-03 til 2026-11-16.
- Sharpe var 0,56 mod 0,69, og alfa −0,3 % om året (t = −0,06).
- Skip-reglen har ingen robust værdi, og valget af konfiguration er reelt uafgjort.
- Detaljer: `reports/backtest_v1.md`.

## Status pr. trin

| Trin | Status |
|---|---|
| Data (hentning) | Færdig. 7.004 PTR-indberetninger 2016–2026, alle hentet. Kørt fra brugerens terminal. |
| Parser | Færdig for tekstbaserede indberetninger: 5.312 (75,8 %), 51.791 handler, 46 tests. De 1.692 scannede (24,2 %) er ikke læst. |
| Kurser | Færdig. Alpaca SIP, justeret for split og udbytte. 2.848 af 3.418 tickere koblet; 94,8 % af købene har kurs ved indgang. |
| Backtest | Færdig (runde 2). 136 kombinationer, forudregistreret kriterium, kontinuerlig out-of-sample-portefølje, 42 tests. |
| Bias-review | Færdig, 2 runder. Runde 2: "Ingen alvorlige fejl"; resterende fund er dokumenteret som begrænsninger. |
| Rapport | Færdig: `reports/backtest_v1.md`, figurer og `reports/site_data.json`. |
| Hjemmeside | Udkast i `site/index.html`. Resultatafsnittet mangler og skal bygges på `reports/site_data.json`. |
| Senatet | Udskudt. eFD blokerede automatisk adgang 17/11-2026. |

## Vigtige beslutninger

**Faste beslutninger (fra planen)**
- **Afgrænsning:** kun Repræsentanternes Hus, indberetninger 2016–2026, kun aktie- og ETF-køb (`include_v1`), alle ejere.
- **Indgang:** åbningskurs første handelsdag efter `filing_date`. Medlemmets handelsdato bruges aldrig til at handle på.
- **Skip-regel:** spring kun over ved stigning over X. Gitter X = 0–30 % i trin af 2, plus "ingen".
- **Exit og vægtning:** exit hold 3, 6 eller 12 mdr. eller når medlemmet sælger. Lige vægt eller beløbsvægt.
- **Omkostninger og benchmark:** 0,1 % pr. side. SPY som benchmark.
- **Udvælgelse:** in-sample 2016–2021 og out-of-sample fra 2022. Kriteriet er højeste in-sample-Sharpe med positioner lukket 31/12-2021 og mindst 200 handler (`src/backtest/config.py`).
- **Primært mål:** den kontinuerlige portefølje fra 2022-01-03 (review runde 1, F1).

**Brugerens valg undervejs**
- **Downloads** kørte fra brugerens terminal, fordi Claudes miljøer ikke kan nå House Clerk og Alpaca.
- **Opus i stedet for Fable 5.2:** Fable 5.2 blev erstattet af Opus som backtester og bias-reviewer, fordi Fable kræver forbrugskreditter.
- **Stikprøve af scannede filer:** stoppet efter 14 af 20 filer.
- **Bias-review:** begrænset til 2 runder (17/11-2026).
- **Senatet:** forsøgt og udskudt.

## Hvor ligger filerne

| Sti | Indhold |
|---|---|
| `ingestion/` | Hentning (`collect_house_raw.py`, `collect_senate_raw.py`) og parser (`parse_house_ptr.py`, `house_ptr_parser.py`) |
| `prices/` | Kurshentning (`fetch_prices.py`) og dækningskontrol (`coverage.py`) |
| `src/backtest/` | Backtest-motoren: `config.py` (regler og kriterium), `engine.py`, `portfolio.py`, `analysis.py`, `run.py` |
| `data/` | Rå PDF'er og indeks (`raw/`), handler (`processed/trades.csv`), kurser (`prices/`) |
| `results/` | Alle backtest-resultater; `ASSUMPTIONS.md` forklarer hver regel |
| `review/` | Bias-review runde 1 og 2, kontrolscripts og `check_report_numbers.py` |
| `reports/` | `backtest_v1.md`, `data_quality_v1.md`, `figures/`, `site_data.json`, `make_figures.py` |
| `tests/` | Unit tests (parser, hentning, kurser, Senat-hentning, backtest) |
| `site/` | Hjemmeside (udkast) |
| `backtest/`, `README.md` | Ældre, udforskende backtests fra før v1 (se review F8) |

## Sådan køres det igen (fra projektmappen)

```
python3 ingestion/collect_house_raw.py 2016-2026   # hent indeks og PDF'er (kræver adgang til House Clerk; kan genoptages)
python3 ingestion/parse_house_ptr.py               # læs alle PDF'er -> data/processed/trades.csv (evt. --max-seconds 140)
python3 prices/fetch_prices.py                     # hent kurser fra Alpaca (ALPACA_KEY_ID og ALPACA_SECRET_KEY i .env)
python3 prices/coverage.py                         # dækningskontrol -> data/prices/coverage_*.csv
python3 -m src.backtest.run                        # hele backtesten -> results/ (ca. 4 minutter)
python3 reports/make_figures.py                    # figurer og reports/site_data.json
python3 review/check_report_numbers.py             # kontrollér tallene i rapporten mod results/
```

Tests:

```
python3 -m unittest tests.test_backtest            # 42 tests
python3 -m unittest tests.test_house_ptr_parser    # 46 tests
python3 -m unittest tests.test_collect_house_raw   # 19 tests
python3 -m unittest tests.test_prices              # 12 tests
python3 -m unittest tests.test_collect_senate_raw  # 38 tests
```

Hentningen (de to første trin og kurserne) kræver netværksadgang til House Clerk og Alpaca. Den har kun kunnet køre fra brugerens Mac.

## Kendte huller

- **Scannede indberetninger:** 24,2 % af indberetningerne mangler. De er skævt fordelt mod 2016–2018 og mod meget aktive handlere som McCaul, Khanna og Rogers.
- **Senatet** er ikke med.
- **Survivorship:**
  - 1.065 køb uden kurs er faldet ud;
  - afnoterede aktier lukkes til sidste kurs, hvilket er optimistisk ved konkurs (SIVB);
  - stresstest med −100 %: out-of-sample +49,8 %.
- **Skip-reglen** bruger udbyttejusterede kurser, og Sharpe er beregnet med rf = 0.
- **34 tickerskift** behandles som afnoteringer.
- **Filteret for ikke-diskretionære køb** blev skrevet efter runde 1 og har gunstig effekt (review runde 2, F1).
- **Koncentration i få medlemmer:** DelBene har 19 % af vægten in-sample.
- **Pseudo-out-of-sample:** 2021–2026 var set i tidligere udforskende backtests (F8).
- **Datafejl:**
  - ejerfeltet er forkert for ca. 561 handler i 2022-skabelonen;
  - 324 ændringsindberetninger har ingen fundet original.

## Næste skridt

1. **Senatet via en anden kilde.** Spørg brugeren først om kilde, vilkår og pris.
2. **Scannede indberetninger.** Brugeren beslutter, om ca. 6.600 sider skal læses, og med hvilken model. Derefter køres v1 igen med samme kriterium.
3. **Valg af medlemmer kun ud fra tidligere information.** Fx rullende rangering af medlemmer efter deres afsluttede handler, med kriteriet låst i git, før der regnes.
4. **Hjemmesidens resultatafsnit** bygges på `reports/site_data.json`.

## Hjemmeside og live papirfond (tilføjet 17/11-2026)

- **Offentlig side:** https://oleo007pat.github.io/congress-fund/ (GitHub Pages fra `main` / `docs`).
  - `docs/index.html` er fondssiden: strategi, resultater, live portefølje, risiko, investering og FAQ.
  - `docs/research.html` er den detaljerede research-side.
- **Byg siden igen:** `python3 site/build.py` læser `site/src/` (home.html, research.html, site.css m.fl., på engelsk) og `results/` og skriver `docs/`. Ret aldrig i `docs/*.html` direkte.
- **Offentlig adresse:** https://ekklesia-capital.vercel.app (Vercel, root directory `docs`). Besøgsstatistik: PostHog EU, uden cookies.
- **Live papirfond:** `live/` køres hver hverdag kl. 03:17 UTC (backup 05:17) af `.github/workflows/paper_fund.yml` på Alpaca PAPER.
  - Reglen ligger i `live/strategy.json`: hold 6 mdr., beløbsvægtet, X = 10 %.
  - Botten committer `data/live/` og `docs/data/live.json` hver dag.
  - **Klik derfor altid Sync i VS Code, før du ændrer noget.**
- **GitHub-secrets:** `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` og `ALPACA_BASE_URL` er sat. Workflow permissions: read and write.
- **Test af de bedste medlemmer:** `reports/member_selection_v1.md`. Resultatet er +89,6 % i testperioden, men ikke signifikant og domineret af to medlemmer.
