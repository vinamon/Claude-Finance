# Claude-Finance

Bot handlowy na koncie **Bybit Demo Trading**. Python, odpalany **lokalnie**
przez `run.py`. Wirtualne środki, zero kontaktu z kontem realnym.

> Nie chodzi na GitHub Actions i nie może: Bybit blokuje geograficznie kraj,
> w którym stoją runnery GitHuba. Workflowy zostały **zarchiwizowane** — nadal
> są w repo i da się je odpalić ręcznie, ale nic nie odpala ich samo.
> Szczegóły w sekcji *GitHub Actions: dlaczego nie* niżej.

---

## Zanim odpalisz: trzy rzeczy, które musisz wiedzieć

**1. Trailing stop to DWA wywołania, nie jedno.** Bybit v5 nie pozwala dopiąć
trailing stopa do zlecenia przy jego tworzeniu. `/v5/order/create` przyjmuje
`stopLoss` i `takeProfit`, ale nie ma pola `trailingStop` w ogóle. Trailing
istnieje wyłącznie na `/v5/position/trading-stop`, który działa na już
otwartej pozycji. Bot robi więc: market buy z SL/TP → osobny strzał z
trailingiem. Jeśli drugi zawiedzie, pozycja i tak jest chroniona stop lossem z
kroku pierwszego.

**2. `trailingStop` to DYSTANS CENOWY, nie procent.** Dokumentacja Bybit mówi
dosłownie: *"Trailing stop by price distance"*. Wysłanie `1.5` z myślą "1.5%"
ustawi trail 1.5 USDT, co na BTC oznacza stop praktycznie w cenie rynkowej.
W configu podajesz ułamek (`TRAILING_STOP_PCT = 0.015`), a bot sam mnoży go
przez cenę wejścia. `activePrice` to cena aktywacji: do jej osiągnięcia
trailing śpi.

**3. Wyjście dziedziczy ramę czasową wejścia.** `EXIT_TIMEFRAME` jest celowo
nieustawione. Wyjście używa **tego samego wskaźnika i tych samych okresów** co
wejście, więc liczenie go na krótszej ramie nie jest symetrycznym wyjściem,
tylko wielokrotnie bardziej nerwowym. Zmierzone na żywo: SMA50/200 czyta 258
godzin historii na 1h i 4 godziny na 1m, czyli różnicę 65-krotną. Krótka rama
wyjścia zamyka pozycje, które trend wejściowy nadal popiera, i płaci za to
prowizję.

Tym, co pilnuje pozycji między przebiegami, są i tak SL, TP oraz trailing po
stronie giełdy. Logika wyjścia w bocie to dodatek, nie zabezpieczenie.

**4. Trailing: aktywacja musi być >= dystans.** Bybit ustawia pierwszy trigger
trailingu na (aktywacja - dystans). Przy 5% i 3% ląduje on 2% **nad** wejściem
i uzbrojenie trailingu blokuje zysk. Odwrotnie ląduje pod wejściem i zamienia
trailing we wczesną stratę, która strzela zanim zadziała stop loss.
`config.validate()` odmawia uruchomienia przy złej kombinacji.

---

## Struktura

| Plik | Rola |
|---|---|
| `config.py` | klucze z env, symbole, parametry strategii, `dummy_mode`, temat ntfy |
| `signals.py` | trzy strategie, wejścia i wyjścia. Czyste funkcje, zero zleceń |
| `executor.py` | zamiana decyzji na zlecenia. Zero logiki strategii |
| `notify.py` | pushe przez ntfy.sh |
| `exchange.py` | budowa klienta ccxt przypiętego do hosta demo |
| `main.py` | przebieg: pozycje zamknięte → wyjścia → wejścia |
| `scripts/test_connection.py` | krok 1: połączenie i saldo demo |
| `scripts/test_ntfy.py` | krok 2: sam push |
| `run.py` | runner lokalny, przełącznik pętla/jeden przebieg |
| `.github/workflows/` | **zarchiwizowane** — bez harmonogramów, tylko ręcznie |

---

## Uruchomienie, po kolei

### 1. Klucze demo

1. Zaloguj się na **mainnet** Bybit (nie testnet).
2. Przełącz się na **Demo Trading** — to osobne konto z własnym user ID.
3. Najedź na awatar → **API** → wygeneruj klucz **będąc w trybie Demo**.
4. Uprawnienia: odczyt + handel kontraktami.

> Klucze z konta realnego **nie zadziałają** na `api-demo.bybit.com`, i o to
> chodzi. `exchange.py` dodatkowo twardo sprawdza, czy każdy URL wskazuje na
> host demo, i wywala się z błędem, jeśli nie.

Wirtualne środki dosypujesz w UI Demo Trading.

### 2. Temat ntfy

Temat ntfy to jedyna kontrola dostępu, jaką ma ntfy.sh: kto zna nazwę, ten
czyta twoje powiadomienia. Wygeneruj długi losowy ciąg:

```bash
python -c "import secrets; print('cf-' + secrets.token_urlsafe(24))"
```

Zainstaluj apkę **ntfy** (Android/iOS) → *Subscribe to topic* → wklej nazwę.
Nigdy nie commituj tematu ani nie wypisuj go w logach (bot tego nie robi).

### 3. GitHub Secrets

*Settings → Secrets and variables → Actions → Secrets*:

| Secret | Co to |
|---|---|
| `BYBIT_API_KEY` | klucz z trybu Demo Trading |
| `BYBIT_API_SECRET` | sekret z trybu Demo Trading |
| `NTFY_TOPIC` | twój losowy temat |

### 4. GitHub Variables (opcjonalne, ale wygodne)

*Settings → Secrets and variables → Actions → Variables*. Cokolwiek ustawisz
tutaj, nadpisuje `config.py` — możesz przestrajać bota bez commita:

Pełna lista z opisem każdego parametru jest w `.env.example` — to jest cały
panel sterowania bota. **Żaden okres, próg, mnożnik ani przełącznik nie jest
zaszyty w kodzie**; wszystko przechodzi przez `config.py`, więc zmiana
zachowania to edycja `.env` i restart, nigdy edycja pliku `.py`.

W skrócie: `DUMMY_MODE`, `AUTOSTART`, `LOOP_INTERVAL_MINUTES`, `SYMBOLS`,
`STRATEGY`, `ACTIVE_STRATEGIES`, `MIN_ENTRY_VOTES`, `MAX_OPEN_POSITIONS`,
`POSITION_NOTIONAL_USDT`, `LEVERAGE`, `ENTRY_TIMEFRAME`, `EXIT_TIMEFRAME`
(pomiń, żeby dziedziczyło), `SIGNAL_LOOKBACK_BARS`, `REGIME_FILTER`,
`REGIME_PERIOD`, `EXIT_ON_REGIME_BREAK`, `UNKNOWN_OWNER_EXIT`,
`EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD`, `ADX_PERIOD`, `ADX_MIN`, `RSI_PERIOD`,
`RSI_OVERSOLD`, `RSI_OVERBOUGHT`, `MEANREV_EXIT_SMA_PERIOD`,
`BREAKOUT_LOOKBACK`, `BREAKOUT_EXIT_LOOKBACK`, `RISK_MODEL`, `ATR_PERIOD`,
`ATR_STOP_MULT`, `ATR_TARGET_MULT`, `ATR_TRAIL_MULT`,
`ATR_TRAIL_ACTIVATION_MULT`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`,
`TRAILING_STOP_PCT`, `TRAILING_ACTIVATION_PCT`, `CLOSED_LOOKBACK_MINUTES`.

`SYMBOLS` jest listą po przecinku, w formacie ccxt:
`BTC/USDT:USDT,ETH/USDT:USDT`

### 5. Testy

Lokalnie, w aktywnym venv:

```bash
python scripts/test_connection.py   # krok 1: saldo demo, specyfikacja symboli
python scripts/test_ntfy.py         # krok 2: push na telefon
```

`test_connection.py` wypisuje ścieżkę interpretera, diagnozę configu,
rozwiązany host i przesunięcie zegara, a kody błędów Bybita tłumaczy na
przyczyny. To pierwsze miejsce, do którego warto zajrzeć, gdy coś nie działa.

<details>
<summary>Historycznie: workflow smoke-test</summary>

Był to sposób na przetestowanie kluczy przyciskiem, bez terminala. Krok 1
**zwraca dziś 403** z runnera GitHuba, bo Bybit blokuje geograficznie kraj, w
którym te runnery stoją — niezależnie od poprawności kluczy. Krok 2 (push
ntfy) nie dotyka Bybita i nadal działa.

*Actions → smoke-test → Run workflow →* `both` / `connection` / `ntfy`.

</details>

### 6. Pierwszy przebieg

```
python run.py --force-entry
```

Przy `DUMMY_MODE=true` (domyślnie) `--force-entry` **wymusza wejście na każdym
skonfigurowanym symbolu**, ignorując rynek — po to, żeby przepchnąć cały
pipeline i zobaczyć, że wszystko działa. Dziewięć symboli = dziewięć pozycji
z jednego polecenia, więc na próbę zostaw w `SYMBOLS` jeden.

Flaga jest **jednorazowa**: w trybie pętli dotyczy tylko pierwszego cyklu.

Kiedy działa: ustaw `DUMMY_MODE=false` i bot zacznie słuchać strategii.

<details>
<summary>Historycznie: pierwszy run z telefonu przez Actions</summary>

Tą ścieżką **już nie da się handlować** — runnery GitHuba dostają od Bybita
403. Zostaje w dokumentacji na wypadek, gdyby workflowy kiedyś wróciły do
użytku na maszynie w kraju, który Bybit obsługuje.

Apka GitHub → repo → **Actions** → workflow **trade** → **Run workflow**.
Przycisk *Run workflow* działa wyłącznie dla workflowów leżących na gałęzi
domyślnej, więc najpierw merge, potem przycisk.

</details>

---

## Uruchomienie na laptopie

Bybit geoblokuje runnery GitHuba (stoja w Wirginii, USA). Na twoim wlasnym
komputerze w Polsce tego problemu nie ma. Ponizsze dziala tak samo na Windows,
macOS i Linuksie.

### Wymagania

**Python 3.10 lub nowszy** (ccxt tego wymaga). Sprawdz:

```
python --version
```

### Instalacja

```
git clone https://github.com/vinamon/Claude-Finance
cd Claude-Finance
pip install -r requirements.txt
```

Skopiuj `.env.example` na `.env` i uzupelnij. `.env` jest w `.gitignore`,
wiec nigdy nie trafi do repo.

### Przelacznik autostartu

> **`AUTOSTART` nie jest autostartem systemu.** Nazwa jest mylaca. Decyduje
> wylacznie o tym, co robi samo `python run.py`. Nic w tym projekcie nie
> rejestruje sie w Windowsie ani w macOS — **po restarcie komputera bot sam
> nie wstanie.** Zeby wstawal, trzeba dopiero dodac skrot w folderze
> Autostart (`shell:startup`), zadanie w Harmonogramie zadan albo usluge.

| Polecenie | Co robi |
|---|---|
| `python run.py` | jeden cykl i koniec |
| `python run.py --loop` | krazy co `LOOP_INTERVAL_MINUTES` do Ctrl+C |
| `python run.py --loop --interval 1` | to samo, co minute |
| `python run.py --force-entry` | jeden cykl, ktory otwiera pozycje testowa |
| `python run.py --kill-all` | **ubija kazda dzialajaca kopie bota** i konczy |

W `.env` ustawiasz zachowanie domyslne:

```
AUTOSTART=true      # samo "python run.py" zaczyna krazyc
AUTOSTART=false     # samo "python run.py" robi jeden cykl
```

Flagi z linii polecen zawsze wygrywaja z `.env`.

`LOOP_INTERVAL_MINUTES` mozna zmieniac swobodnie. Decyzje zapadaja na
**zamknietych** swiecach, wiec czestsze odpytywanie oznacza tylko szybsze
zauwazenie zamknietego slupka, nigdy inna decyzje. `--interval` przestawia
przy okazji dlugosc kubelka `orderLinkId`, zeby te dwie rzeczy nie rozjechaly
sie w czasie.

Harmonogram siedzi w samym skrypcie, nie w cronie ani Harmonogramie zadan
Windows. Jeden mechanizm zamiast trzech zaleznych od systemu, i widzisz
odliczanie do nastepnego cyklu na wlasne oczy.

### Jak zatrzymac bota

Ctrl+C w oknie, w ktorym chodzi. A kiedy tego okna juz nie ma albo kopii jest
kilka:

```
python run.py --kill-all
```

Znajduje kazdy proces Pythona uruchamiajacy `run.py` **tego** projektu, prosi
grzecznie, czeka `KILL_GRACE_SECONDS` i dopiero potem ubija na twardo.

**Dwie kopie naraz to realny problem**, nie teoria. Podwojnych pozycji nie
otworza — kazdy cykl pyta gielde, co jest trzymane, a `orderLinkId` odrzuci
duplikat. Ale obie pisza do `state/owners.json` i wygrywa ta, ktora zapisze
pozniej, wiec mozesz zgubic informacje, **ktora strategia otworzyla pozycje**.
Wyjsciem pokieruje wtedy `UNKNOWN_OWNER_EXIT` zamiast wlasciwej reguly. Do
tego kazdy push dostaniesz dwa razy. Dlatego `--loop` ostrzega, gdy wykryje
juz dzialajaca kopie.

Ani Ctrl+C, ani `--kill-all` nie rusza otwartych pozycji: stop loss, take
profit i trailing siedza na Bybicie i dzialaja niezaleznie od tego, czy
cokolwiek chodzi na laptopie.

### Pierwsze uruchomienie, po kolei

```
python scripts/test_connection.py    # 1. czy klucze i host demo dzialaja
python scripts/test_ntfy.py          # 2. czy push dochodzi na telefon
python run.py --once                 # 3. przebieg bez wchodzenia w rynek
python run.py --once --force-entry   # 4. wymuszone wejscie testowe
python run.py --loop                 # 5. dopiero teraz automat
```

### VS Code

W `.vscode/launch.json` sa gotowe konfiguracje pod F5, w tej samej kolejnosci
co wyzej. Wybierasz z listy w panelu Run and Debug, nie musisz nic wpisywac.

### Co sie dzieje po zamknieciu laptopa

Nic i to jest w porzadku. **Stop loss, take profit i trailing stop siedza na
serwerach Bybita** i dzialaja niezaleznie od tego, czy skrypt chodzi. Tracisz
tylko wyjscie wedlug strategii i nowe wejscia. Otwarta pozycja jest
chroniona, po prostu nie jest zarzadzana.

Konsekwencja: przy wylaczonym laptopie pozycja wyjdzie wylacznie przez SL, TP
albo trailing. Jesli cena bedzie sie miotac w bok i nie dotknie zadnego z
nich, moze wisiec dlugo.

---

## Strategie

`STRATEGY = "trend" | "meanrev" | "breakout" | "scalp" | "multi"`. Przy `multi` bot
liczy **wszystkie** strategie z `ACTIVE_STRATEGIES` w każdym cyklu i wchodzi,
gdy zgodzi się co najmniej `MIN_ENTRY_VOTES` z nich. Żaden inny plik się nie
zmienia.

| | Wejście | Wyjście | Parametry |
|---|---|---|---|
| `trend` | EMA szybka przecina wolną od dołu, potwierdzone przez ADX | szybka poniżej wolnej | `EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD`, `ADX_PERIOD`, `ADX_MIN` |
| `meanrev` | krótkie RSI spada do strefy wyprzedania **w trendzie wzrostowym** | close wraca nad krótką SMA, albo RSI dochodzi do wykupienia | `RSI_PERIOD`, `RSI_OVERSOLD`, `RSI_OVERBOUGHT`, `MEANREV_EXIT_SMA_PERIOD` |
| `breakout` | close powyżej maksimum z N świec | close poniżej minimum z **M** świec, M < N | `BREAKOUT_LOOKBACK`, `BREAKOUT_EXIT_LOOKBACK` |
| `scalp` | close pod dolną wstęgą Bollingera, **rama 15m** | powrót do środkowej wstęgi | `BB_PERIOD`, `BB_STDEV`, `BB_LOOKBACK_BARS` |

> To **podręcznikowe systemy z opublikowaną historią, nie przewagi, które sami
> odkryliśmy.** Zakładaj, że każda traci po prowizjach, dopóki twój własny
> backtest nie powie inaczej.

### Filtr reżimu: to on sprawia, że trzy strategie naraz mają sens

Podążanie za trendem i powrót do średniej to filozoficzne przeciwieństwa:
jedna kupuje siłę, druga słabość. Puszczone obok siebie bez filtra, wejście
jednej jest wyjściem drugiej.

`REGIME_FILTER` to rozwiązuje. Żadna strategia nie może kupić poniżej wolnej
średniej `REGIME_PERIOD`, więc powrót do średniej staje się **"kup dołek
W trendzie wzrostowym"** — czyli tą dobrze udokumentowaną wersją, a nie
łapaniem spadającego noża. Wszystkie trzy ciągną wtedy w tę samą stronę i
różnią się tylko tym, co wyzwala wejście.

`EXIT_ON_REGIME_BREAK` domyka to z drugiej strony: gdy cena wraca pod tę
średnią, pozycja jest zamykana niezależnie od tego, która strategia ją
otworzyła. Powód, żeby w ogóle być długo, zniknął.

### Kto otwarł pozycję, ten ją zamyka

Bybit w trybie one-way trzyma jedną pozycję na symbol, więc trzy strategie nie
mogą trzymać trzech pozycji na tym samym rynku. Przy wejściu zapisywane jest
więc, **która** strategia je otwarła — w `state/owners.json` oraz w
`orderLinkId`, dzięki czemu widać to także w interfejsie Bybita. Wyjścia
pilnuje ta sama strategia.

To nie jest kosmetyka. Zmierzone na tym samym wzroście: właściciel `trend`
trzyma pozycję, a `meanrev` w tej samej chwili wychodzi — bo dla niego odbicie
już się wydarzyło. Zamykanie pozycji mean-reversion regułą trend-followingu
psuje obie strategie naraz.

Gdy właściciel jest nieznany (pozycja otwarta ręcznie, plik stanu utracony),
decyduje `UNKNOWN_OWNER_EXIT`: `any` / `all` / `regime`. Plik stanu jest
najlepszym staraniem i **nigdy** nie decyduje o tym, czy pozycja istnieje —
tu jedynym źródłem prawdy pozostaje giełda.

### Dwa zegary naraz: swing i scalping

`STRATEGY_TIMEFRAMES` daje każdej strategii własną ramę czasową, więc bot
prowadzi jednocześnie **księgę dzienną i szybką**, w tym samym cyklu i na tym
samym koncie. Trzy reguły czytają świece godzinowe, `scalp` czyta 15-minutowe.

`scalp` to powrót do średniej na wstęgach Bollingera: kupuje zamknięcie
rozciągnięte pod dolną wstęgą, wychodzi gdy cena wróci do środka. Zasługuje na
miejsce obok pozostałych, bo odchylenie standardowe mierzy coś, czego nie
mierzy żadna z nich — **jak daleko obecny ruch leży poza normalną zmiennością
tego rynku**. Dlatego ta sama reguła działa na BTC i na memecoinie bez
strojenia.

Zmierzone trzymanie pozycji: `scalp` minuty do ~2 godzin, reguły godzinowe
kilka do kilkunastu godzin.

**`MAX_OPEN_PER_STRATEGY` jest tym, co czyni to prawdziwym, a nie pozornym.**
Bez tego szybka strategia zjada wszystkie sloty, zanim wolne w ogóle do
któregoś dojdą: reguła 15-minutowa na czterdziestu rynkach odpala
wielokrotnie częściej niż godzinowa, więc „szybko **i** dziennie" po cichu
zamienia się w „tylko szybko".

Filtr reżimu liczony jest **osobno dla każdej strategii, na jej własnej
ramie**. Na świecach godzinowych linia 200-okresowa to około ośmiu dni trendu,
na 15-minutowych około dwóch. Skalper nie ma po co być blokowany widokiem
ośmiodniowym, a reguła swingowa nie ma po co być wpuszczana dwudniowym.

### Stop za ceną likwidacji to nie jest stop

**Najważniejsza rzecz, jakiej nauczyło nas testowanie na żywo.** Dźwignia
stawia likwidację mniej więcej `100/dźwignia` procent od wejścia — 6,67% przy
15x. Stop dalej niż to **nigdy nie zadziała**: giełda zamknie pozycję
pierwsza, zabierze cały depozyt i doliczy opłatę likwidacyjną.

Złapane na tym koncie, nie w teorii:

```
ARB   wejscie     0.15130
      stop loss   0.14061   (-7.07%)
      LIKWIDACJA  0.14264   (-5.72%)   <- wyzej niz stop
```

Zmierzone na czterdziestu symbolach: 2×ATR waha się od **1,24% na BTC do 27%
na najdzikszym alcie** — różnica dwudziestokrotna. Żadna pojedyncza wartość
`LEVERAGE` tego nie obsłuży, więc zabezpieczenie działa per transakcja:

| Ustawienie | Co robi |
|---|---|
| `MAX_STOP_FRACTION_OF_LIQUIDATION=0.5` | przycina stop do połowy dystansu do likwidacji |
| `MIN_STOP_ATR_MULT=1.0` | **odrzuca transakcję**, gdy po przycięciu stop byłby węższy niż 1×ATR |

Drugie jest równie ważne jak pierwsze. Stop wciśnięty w zwykły ruch świecy to
nie ochrona, tylko zaplanowane wyjście, które następna świeca uruchomi
przypadkiem. Na symbolu o ATR równym 13% ceny nie istnieje stop, który
jednocześnie mieści się w likwidacji przy 15x i cokolwiek znaczy — i uczciwą
odpowiedzią jest nie brać tej transakcji.

### Skąd się wzięła lista czterdziestu symboli

Ranking po obrocie 24h, odczytany wprost z giełdy. Bybit ma **762** wieczyste
kontrakty USDT i pobranie wszystkich nie wchodzi w grę: jeden cykl trwałby
około 14 minut, czyli dłużej niż świeca, którą skalper ma łapać.

Surowa czterdziestka po obrocie **nie jest samym krypto**. Siedzą w niej
tokenizowane akcje i surowce — AAPL, TSLA, MSTR, SOXL, SKHYNIX, XAU, XAG,
ropa — które chodzą wedle innego zegara i innej logiki niż cokolwiek, pod co
te strategie budowano. Nic w metadanych API ich nie odróżnia: `contractType`
jest identyczny, `fetch_currencies()` nic nie zwraca na hoście demo, a handel
idzie 24/7, więc test „martwych świec" też ich nie wyłapuje. Zostały więc
wykluczone z nazwy, a wszystko, czego tożsamości nie dało się ustalić na
pewno, po prostu pominięto zamiast zgadywać.

### Model ryzyka: ATR zamiast sztywnych procentów

`RISK_MODEL=atr` wylicza stop, cel i trailing z ATR, czyli z realnej
zmienności danego instrumentu. Zmierzone na żywo w tej samej chwili: ATR14 to
0,48% ceny na BTC i 1,02% na XRP — ponad dwukrotna różnica. Sztywne 5% jest
więc na jednym rynku za ciasne, a na drugim za szerokie, i to rynek decyduje
na którym. Gdy ATR nie da się policzyć, kod sam wraca do wartości
procentowych.

**Dlaczego wejścia to zdarzenia, a wyjścia to stany.** Wejście szuka
*przecięcia* w ostatnich `SIGNAL_LOOKBACK_BARS` zamkniętych świecach — wejście
dlatego, że warunek "nadal obowiązuje", wchodziłoby w kółko. Wyjście patrzy na
*aktualny stan*, bo bot budzi się co kilka minut i gubi większość świec;
wyjście zdefiniowane jako pojedyncza świeca przecięcia prędzej czy później
zostałoby przegapione i zostawiło pozycję bez opieki. "Czy jestem teraz po złej
stronie wskaźnika" przegapić się nie da.

Bot liczy sygnały **wyłącznie na świecach zamkniętych** — ostatnia świeca z API
jest w trakcie formowania i jej close to po prostu bieżąca cena, która migocze.

---

## Zasady, na których to stoi

- **Giełda jest jedynym źródłem prawdy.** Żadnego stanu lokalnego o pozycjach.
  Runner jest jednorazowy, więc lokalny obraz "co mam" byłby zgadywaniem.
- **Jedna pozycja na symbol.** Przed wejściem bot pyta Bybit, czy już czegoś
  nie trzyma.
- **`orderLinkId` per (symbol, 5-minutowy kubełek).** Ponowienie wewnątrz tego
  samego runu trafia na ten sam ID i zostaje odrzucone — dokładnie o to chodzi,
  bo retry po niejednoznacznym timeoucie nie może otworzyć drugiej pozycji.
  Następny run to nowy kubełek.
- **Wielkość zlecenia zaokrąglana W DÓŁ** do `qtyStep`. Poniżej `minOrderQty`
  bot odmawia zamiast po cichu podbić pozycję.
- **Dźwignia:** błąd 110043 (*"leverage not modified"*) jest połykany, każdy
  inny leci dalej.
- **Tryb one-way** (`positionIdx = 0`), nie hedge.
- **Błąd = czerwony run.** Kod wyjścia niezerowy, żeby było widać w apce.

### `POSITION_NOTIONAL_USDT` to nominał, nie depozyt

To `qty * cena`. Margin, który realnie blokujesz, to mniej więcej
`nominał / dźwignia`. Nominał 500 przy dźwigni 5 = 100 USDT depozytu.
Podniesienie dźwigni **nie zmienia** wielkości pozycji, tylko to, jak blisko
siedzi likwidacja.

---

## Powiadomienia

Push leci przy: otwarciu pozycji, wykryciu zamknięcia, wyjściu ze strategii,
błędzie krytycznym.

Zamknięcia bot wykrywa odpytując `/v5/position/closed-pnl` za ostatnie
`CLOSED_LOOKBACK_MINUTES`. Żeby to samo zamknięcie nie waliło ci w telefon na
każdym runie przez cały okres okna, lista już zgłoszonych ID jedzie między
runami w `actions/cache`. To **czysta kosmetyka** — pudło w cache kosztuje
duplikat powiadomienia, nigdy duplikat transakcji. Jeśli chcesz to wyłączyć,
ustaw `NOTIFIED_STATE_FILE=""`.

---

## GitHub Actions: dlaczego nie

Bybit stawia przed swoim API CloudFront i blokuje geograficznie kraj, w
którym stoją runnery GitHuba. To jest **zmierzone, nie zgadnięte**: runner
zgłosił `Azure Region: eastus`, IP w Wirginii, a wszystkie trzy hosty Bybita
(`api-demo`, `api-testnet`, `api.bybit.com`) zwróciły 403 z komunikatem
*"The Amazon CloudFront distribution is configured to block access from your
country"* — na publicznym endpoincie, bez żadnego klucza.

Żadna zmiana uprawnień, kluczy ani liczby ponowień tego nie obejdzie. Regionu
standardowego runnera nie da się wybrać na darmowym planie.

**Nie próbuj obchodzić tego przez proxy ani VPN** — to łamie regulamin Bybita
i naraża konto.

Dlatego bot chodzi z laptopa, a workflowy zostały zarchiwizowane: nie mają
już wyzwalacza `schedule:`, została sama możliwość ręcznego odpalenia
(`workflow_dispatch`). Pliki celowo **nie zostały skasowane** — działają bez
zmian na maszynie w kraju, który Bybit obsługuje.

| Workflow | Stan |
|---|---|
| `trade.yml` | zarchiwizowany. Uwaga: jego lista zmiennych jest nieaktualna, szczegóły w nagłówku pliku |
| `keepalive.yml` | zarchiwizowany, patrz niżej |
| `smoke-test.yml` | tylko ręcznie (nigdy nie miał harmonogramu). Krok 1 zwróci 403 |
| `geo-check.yml` | **bez zmian i nadal użyteczny** — mierzy blokadę w 30 sekund |

Jeśli kiedyś zechcesz je reaktywować, instrukcja krok po kroku siedzi w
nagłówku każdego pliku.

---

## Keepalive — zarchiwizowany

GitHub wyłącza harmonogramy w repo bez commitów przez 60 dni. Sam fakt, że
workflow się odpala, tego licznika **nie** resetuje — liczy się aktywność w
repo. Dlatego `keepalive.yml` raz w tygodniu pushował pusty commit, żeby
podtrzymać cron `trade.yml`.

`trade.yml` nie ma już crona, więc nie ma czego podtrzymywać i keepalive
został wyłączony. Miał też jako jedyny uprawnienie `contents: write`, czyli
prawo pisania do repo — zadanie z takim uprawnieniem chodzące w kółko bez
powodu to niepotrzebne ryzyko.

Reaktywować **wyłącznie** po przywróceniu harmonogramu w `trade.yml`.
Wymaga wtedy: *Settings → Actions → General → Workflow permissions →
**Read and write permissions***.

---

## Bezpieczeństwo

- Repo jest publiczne. Klucze żyją wyłącznie w Secrets, nigdy w kodzie.
- Bot nie wypisuje ani kluczy, ani tematu ntfy.
- `exchange.py` odmawia startu, jeśli którykolwiek URL nie wskazuje na
  `api-demo.bybit.com`.
- `DUMMY_MODE` domyślnie `true`. Handel z sygnałów włącza się świadomie.
