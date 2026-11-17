Manuelt tjekkede indberetninger (runde 2). Hver fil {år}_{doc}.checked.txt er skrevet ned ved at se PDF-siderne som billeder
(IKKE kopieret fra parserens output). Hvis PDF og parser er uenige, er det PDF'en, der gælder.
Format: første linje "# nøgle=værdi | ..." (member, state, signed, template, pages, checked), derefter én linje pr. handel:
owner|asset|ticker|kode|type|handelsdato|beskeddato|beløb|subholding|beskrivelse|status
  owner: self/spouse/joint/dependent child;  kode: aktivtype-koden i PDF'en ([ST] -> ST), tom hvis skabelonen ingen har;
  type: buy/sell/partial_sale/exchange;  datoer ISO;  beløb: "1001-15000", "360" (præcist), "1000000-" (åbent "Over");
  linjer, der begynder med #, er kommentarer. Tekstfelter sammenlignes uden hensyn til store/små bogstaver og tegnsætning.
