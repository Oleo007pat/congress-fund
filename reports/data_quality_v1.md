# Datakvalitetsrapport, backtest v1 (checkpoint)

Dato: 17. november 2026. Branch: `backtest-v1`. Kun Repræsentanternes Hus, indberetninger 2016-01-01 til 2026-11-16.
Backtesten er IKKE kørt. Denne rapport er grundlaget for beslutningen om at køre den.

## 1. Indberetninger

| | Antal | Andel |
|---|---:|---:|
| PTR-indberetninger i Husets indeks 2016-2026 | 7.004 | 100 % |
| Hentet som PDF | 7.004 | 100 % (0 fejlede downloads) |
| Med tekstlag, læst af parseren | 5.312 | 75,8 % |
| Heraf uden nogen advarsel | 5.252 | 98,9 % af de tekstbaserede |
| Scannede/håndskrevne (intet tekstlag) | 1.692 | 24,2 % |

Skabeloner: 2016-skabelonen 1.104, 2018-2022-skabelonen 2.215, 2022-skabelonen 1.993.
De 60 tekstbaserede indberetninger med advarsel er næsten alle "ejer passer ikke med kontooplysninger" (2016-2018).

Scannede pr. år (indberetninger): 2016: 315, 2017: 295, 2018: 297, 2019: 163, 2020: 136, 2021: 115, 2022: 119, 2023: 72, 2024: 57, 2025: 66, 2026: 57.
De kommer fra 107 medlemmer. Flest har Harold Rogers (129), Michael McCaul (127), Rohit Khanna (121), Kurt Schrader (84) og Chuck Fleischmann (80).

## 2. Handler

| | Antal |
|---|---:|
| Handler fundet i de tekstbaserede indberetninger | 51.791 |
| Med i v1 (aktier og ETF'er, også ægtefæller og børn) | 41.998 |
| heraf køb | 20.567 |
| heraf salg / delvist salg / bytte | 14.650 / 6.437 / 344 |
| Forskellige tickere i v1 | 3.418 |
| Medlemmer med mindst ét køb i v1 | 193 |

Køb i v1 pr. indleveringsår: 2016: 1.370, 2017: 1.564, 2018: 1.745, 2019: 2.015, 2020: 3.049, 2021: 2.433, 2022: 1.347, 2023: 1.265, 2024: 1.072, 2025: 3.300, 2026: 1.407.
Kalibrering (2016-2021): 12.176 køb. Test (2022-i dag): 8.391 køb.

Holdt ude af v1 (9.793 handler), største årsager:
- ingen ticker (fonde, obligationer, private selskaber og lignende): 7.677
- optioner og warrants: 906
- mistænkelig dato (fx handelsdato efter indlevering eller over 3 år før): 371
- obligations-, krypto- og andre aktivkoder: 264+
- tvivl om aktivtype, holdt ude efter den konservative regel: 139
- dubletter fra ændringsindberetninger: 514, og handler slettet af senere indberetning: 33

Beløb: alle handler har et læst beløbsinterval. 457 er præcise beløb, 15 er intervaller, der var delt af et sideskift.

Forsinkelse fra handel til indlevering (køb): median 28 dage, 75 % inden for 41 dage, 90 % inden for 226 dage. 19 % af købene er indleveret senere end STOCK Acts frist på 45 dage.

## 3. Hvor godt er parsningen kontrolleret

- 26 indberetninger er manuelt aflæst fra PDF-billedet og ligger som testeksempler, fordelt på 2016-2025 og alle tre skabeloner (2016: 3, 2017: 4, 2018: 7, 2019: 3, 2020: 5, 2022-2025: 1 hver, plus 9 ældre eksempler holdt op mod deres PDF).
- 46 tests for parseren, 19 for hentningen og 12 for kurserne består, både i cloud-miljøet og på brugerens Mac.
- For 2021-2026 er hver ændring i forhold til den oprindelige parser listet med årsag (214 feltændringer, 9 handler, der før manglede).

## 4. Kurser (Alpaca)

- Feed: SIP, altså det samlede amerikanske børsfeed, ikke kun IEX. Kurserne er justeret for splits og udbytte. SPY har data fra 2016-01-04 til 2026-11-16.
- Tickere koblet til kursdata: 2.848 af 3.418 (83 %): 2.822 direkte, 25 via Alpacas tickerhistorik (asof), 1 via navneskift. 570 tickere har ingen kursdata.
- 741 symboler har data, der slutter før oktober 2026 (formentlig afnoteret eller opkøbt). De er beholdt med deres sidste handelsdag, så backtesten kan håndtere dem.

Dækning for køb i v1, altså andel med kurs på den pågældende dag:

| | Indgang (dagen efter indlevering) | +3 mdr | +6 mdr | +12 mdr |
|---|---:|---:|---:|---:|
| Alle år | 94,8 % | 94,4 % | 93,9 % | 92,6 % |
| Kalibrering 2016-2021 | 93,7 % | | | |
| Test 2022-i dag | 96,4 % | | | |

Laveste årlige dækning ved indgang: 2018-2020 med 92,0-92,5 %, og 2023 med 91,4 % (køb).
311 af 2.599 tickere i køb har ingen kurs overhovedet. Hver handel uden kurs er listet med årsag i `data/prices/unmatched_trades.csv`.

## 5. Kendte huller og begrænsninger

1. **Scannede indberetninger mangler.** 24 % af indberetningerne og en ukendt, men stor andel af handlerne. En stikprøve på 14 scannede filer (læst af Fable 5.2) gav 547 handler. Skønnet for alle 1.692 ligger mellem ca. 8.000 og 98.000 handler, mod 51.791 i de tekstbaserede. Selv det laveste skøn er over planens grænse på 5 %. Det rammer kalibreringsårene hårdest og fjerner nogle af de mest aktive handlere (McCaul, Khanna, Rogers). Det er ikke en tilfældig udeladelse, så resultater for "bedste medlem" og for hele gruppen kan blive skæve.
   Nøjagtighed i stikprøven: to uafhængige læsere var enige om alle 254 handler i de 9 filer, de begge læste. En stikprøve tjekket mod billedet (40 handler) stemte felt for felt.
2. **Survivorship bias i kurserne.** Køb uden kursdata (5,2 % ved indgang) kan ikke indgå. Det er overvejende afnoterede selskaber, og nogle af dem er gået konkurs. Udeladelsen kan gøre resultatet for pænt. Backtesten skal rapportere, hvor mange handler der falder ud, og hvordan positioner i aktier, der afnoteres undervejs, lukkes.
3. **Ejerfeltet i 2022-skabelonen** er forkert for ca. 561 handler (heraf ca. 348 i v1). Der står "selv", hvor PDF'en siger ægtefælle, fælles eller barn. Det påvirker ikke v1, hvor alle ejere er med.
4. **Afkortede beskrivelser i 2022-skabelonen** (mindst 51 handler). En option, der kun er nævnt på beskrivelsens anden linje, kan derfor være taget med som aktie. Det drejer sig om få handler.
5. **Ændringsindberetninger** uden en fundet original (324 rækker) kan ikke markeres som dubletter. Nogle af dem kan derfor tælle dobbelt.
6. **Senatet er ikke med** (besluttet i planen for v1).

## 6. Afvigelser fra planen

- Downloads kørte fra brugerens terminal, fordi Claudes miljøer ikke kan nå House Clerk og Alpaca. Kilderne er de samme som i planen.
- Fable 5.2 kræver forbrugskreditter. Efter brugerens valg (17/11) overtager Opus backtesteren (agent 4) og bias-kontrollen (agent 5). Stikprøven af scannede blev standset efter 14 af 20 filer.
- Kurserne er justeret for splits og udbytte med Alpacas `adjustment=all`, og SPY er derfor tilnærmet total return. Om udbyttejusteringen er fuldstændig, er ikke verificeret.

## 7. Beslutninger før backtesten

1. Skal backtesten køres på de tekstbaserede data, med de scannede som dokumenteret hul?
2. Skal de scannede læses alligevel, og i så fald med hvilken model? Det er ca. 6.600 sider.
