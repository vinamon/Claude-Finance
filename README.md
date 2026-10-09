# Claude-Finance

Bot handlowy na koncie **Bybit Demo Trading**. Python, odpalany **lokalnie**
przez `run.py`. Wirtualne środki, zero kontaktu z kontem realnym.

> Nie chodzi na GitHub Actions i nie może: Bybit blokuje geograficznie kraj,
> w którym stoją runnery GitHuba. Workflowy zostały **zarchiwizowane** — nadal
> są w repo i da się je odpalić ręcznie, ale nic nie odpala ich samo.
> Szczegóły w sekcji *GitHub Actions: dlaczego nie* niżej.

---

## Zanim odpalisz: trzy rzeczy, które musisz wiedzieć

**1. Stop loss i take profit siedzą na samym zleceniu, trailing jest
wyłączony.** Bot wysyła zlecenie rynkowe z doklejonym `stopLoss` i
`takeProfit`, więc pozycja ani przez chwilę nie jest bez ochrony. Trailing
stop jest domyślnie **wyłączony** (`ATR_TRAIL_MULT=0`, `TRAILING_STOP_PCT=0`):
trail uzbrajany przy +3×ATR z dystansem 1,5×ATR zabezpiecza tylko +1,5×ATR,
a zwykłe cofnięcie o 1,5×ATR go uruchamia, więc zamykał większość zyskownych
pozycji przed celem 6×ATR (commit 37a7028). Gdyby go włączyć, pamiętaj o dwóch
faktach z dokumentacji Bybit v5:

- `/v5/order/create` w ogóle nie ma pola `trailingStop`. Trailing istnieje
  wyłącznie na `/v5/position/trading-stop`, który działa na już otwartej
  pozycji, więc to **drugie wywołanie**. Jeśli zawiedzie, pozycję i tak chroni
  stop loss z pierwszego.
- `trailingStop` to **dystans cenowy, nie procent** (*"Trailing stop by price
  distance"*). W configu podajesz ułamek, a bot sam mnoży go przez cenę
  wejścia. Aktywacja musi być >= dystans, bo Bybit stawia pierwszy trigger na
  (aktywacja - dystans); odwrotnie ląduje on pod wejściem.
  `config.validate()` odmawia uruchomienia przy złej kombinacji.

**2. Dźwignia i wielkość pozycji czytane są razem.** `LEVERAGE=15` i
`POSITION_NOTIONAL_USDT=450` w `.env` dają 30 USDT własnego depozytu na
pozycję. Bez tych dwóch linii `config.py` celowo spada do bezpieczniejszych
1000 USDT przy dźwigni 1x — czyli do innego bota. Dotyczy to też powtórki
(*replay*): jeden jej przebieg po cichu policzył się przy 1x i cała tabela
wyników była błędna. Nagłówek powtórki wypisuje nominał i dźwignię — sprawdź
go, zanim przeczytasz cokolwiek dalej.

**3. Wyjście dziedziczy ramę czasową wejścia.** `EXIT_TIMEFRAME` jest celowo
nieustawione. Reguła wyjścia używa **tych samych okresów** co wejście, więc
liczenie jej na krótszej ramie nie jest symetrycznym wyjściem, tylko
wielokrotnie bardziej nerwowym. Zmierzone na żywo na dawnej parze średnich:
258 godzin historii na 1h i 4 godziny na 1m, czyli różnica 65-krotna.

Tym, co pilnuje pozycji między przebiegami, są i tak SL i TP po stronie
giełdy. Od decyzji z 2026-10-08 to wręcz jedyne wyjście: `ict` zamyka
wyłącznie stop i cel, a `breakout` (wyłączony) ma regułę wyjścia wyłączoną
(`BREAKOUT_EXIT_LOOKBACK=0`).

---

## Struktura

| Plik | Rola |
|---|---|
| `config.py` | klucze z env, symbole, parametry strategii, `dummy_mode`, temat ntfy, walidacja |
| `signals.py` | strategie `ict` i `breakout`, filtr reżimu, lustrzany wykres dla shortów, wejścia i wyjścia. Czyste funkcje, zero zleceń |
| `executor.py` | zamiana decyzji na zlecenia: `planEntry` (stop i cel), wielkość, dźwignia, znacznik strategii. Zero logiki strategii |
| `notify.py` | pushe przez ntfy.sh |
| `exchange.py` | budowa klienta ccxt przypiętego do hosta demo |
| `main.py` | przebieg: pozycje zamknięte → wyjścia → wejścia |
| `run.py` | runner lokalny, przełącznik pętla/jeden przebieg |
| `scripts/test_connection.py` | krok 1: połączenie i saldo demo |
| `scripts/test_ntfy.py` | krok 2: sam push |
| `scripts/replay.py` | powtórka: przepuszcza historyczne świece przez kod bota i ocenia każdą strategię i stronę |
| `docs/strategies.md` | reguły strategii spisane dla agenta `trader` |
| `tests/` | testy bez sieci, na udawanym Bybicie |
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
tutaj, nadpisuje `config.py` — ale tylko w zarchiwizowanych workflowach
GitHuba i tylko te zmienne, które przekazuje blok `env:` workflowu (a ten jest
nieaktualny, patrz niżej). Na laptopie to samo robi `.env`, i to on jest
panelem sterowania.

Pełna lista z opisem każdego parametru jest w `.env.example` — to jest cały
panel sterowania bota. **Żaden okres, próg, mnożnik ani przełącznik nie jest
zaszyty w kodzie**; wszystko przechodzi przez `config.py`, więc zmiana
zachowania to edycja `.env` i restart, nigdy edycja pliku `.py`.

W skrócie: `DUMMY_MODE`, `AUTOSTART`, `LOOP_INTERVAL_MINUTES`,
`ORDER_BUCKET_SECONDS`, `LOG_DETAIL`, `SYMBOLS`, `STRATEGY`,
`ACTIVE_STRATEGIES`, `SHORT_STRATEGIES`, `MIN_ENTRY_VOTES`,
`MAX_OPEN_POSITIONS`, `REENTRY_COOLDOWN_BARS`, `MAX_OPEN_PER_STRATEGY`,
`STRATEGY_TIMEFRAMES`, `POSITION_NOTIONAL_USDT`, `LEVERAGE`,
`ENTRY_TIMEFRAME`, `EXIT_TIMEFRAME` (pomiń, żeby dziedziczyło),
`SIGNAL_LOOKBACK_BARS`, `REGIME_FILTER`, `REGIME_PERIOD`,
`EXIT_ON_REGIME_BREAK`, `UNKNOWN_OWNER_EXIT`, `BREAKOUT_LOOKBACK`,
`BREAKOUT_EXIT_LOOKBACK`, ustawienia `ict` (`ICT_SWING_BARS`,
`ICT_LIQUIDITY_LOOKBACK_BARS`, `ICT_SWEEP_RECLAIM_BARS`, `ICT_MSS_MAX_BARS`,
`ICT_DISPLACEMENT_MIN_ATR`, `ICT_FVG_MIN_ATR`, `ICT_FVG_MAX_AGE_BARS`,
`ICT_ENTRY_CLOSE_MIN`, `ICT_MAX_CHASE_ATR`, `ICT_KILL_ZONES`,
`ICT_KILL_ZONE_TZ`, `ICT_STOP_REF`, `ICT_STOP_BUFFER_ATR`,
`ICT_STOP_FLOOR_ATR`, `ICT_ALLOW_CAPPED_STOP`, `ICT_MIN_RR`,
`ICT_FALLBACK_TARGET_R`), `RISK_MODEL`, `ATR_PERIOD`, `ATR_STOP_MULT`,
`ATR_TARGET_MULT`, `ATR_TRAIL_MULT`, `ATR_TRAIL_ACTIVATION_MULT`,
`MAX_STOP_FRACTION_OF_LIQUIDATION`, `MIN_STOP_ATR_MULT`, `STOP_LOSS_PCT`,
`TAKE_PROFIT_PCT`, `TRAILING_STOP_PCT`, `TRAILING_ACTIVATION_PCT`,
`WARMUP_MULTIPLIER`, `CANDLE_FLOOR`, `CANDLE_CEILING`,
`CLOSED_LOOKBACK_MINUTES`, `KILL_GRACE_SECONDS`.

Ustawienia usuniętych strategii (np. `EMA_FAST_PERIOD`, `RSI_PERIOD`) nic już
nie robią. Jeśli zostały w `.env`, bot ostrzega o nich przy starcie, zamiast
je po cichu ignorować.

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

Testy samego kodu chodzą bez sieci, na udawanym Bybicie:

```bash
python -m unittest discover -s tests
```

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
pipeline i zobaczyć, że wszystko działa. Dziesięć symboli = dziesięć pozycji
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

`requirements.txt` instaluje tez `tzdata`: Windows nie ma wlasnej bazy stref
czasowych, a `ICT_KILL_ZONES` liczy godziny wedlug czasu nowojorskiego. Po
aktualizacji kodu odpal `pip install -r requirements.txt` jeszcze raz.

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

**Dzialajaca petla nie widzi zmian.** `.env` i kod sa wczytywane raz, przy
starcie. Po kazdej zmianie: `python run.py --kill-all`, potem znowu
`python run.py --loop`.

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

Ani Ctrl+C, ani `--kill-all` nie rusza otwartych pozycji: stop loss i take
profit siedza na Bybicie i dzialaja niezaleznie od tego, czy cokolwiek chodzi
na laptopie.

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

Nic i to jest w porzadku. **Stop loss i take profit siedza na serwerach
Bybita** i dzialaja niezaleznie od tego, czy skrypt chodzi. Tracisz tylko
nowe wejscia (i wyjscie wedlug reguly strategii, jesli jakas je ma). Otwarta
pozycja jest chroniona, po prostu nie jest zarzadzana.

Konsekwencja: przy wylaczonym laptopie pozycja wyjdzie wylacznie przez SL albo
TP. Jesli cena bedzie sie miotac w bok i nie dotknie zadnego z nich, moze
wisiec dlugo.

---

## Strategie

`STRATEGY = "ict" | "breakout" | "multi"`. Przy `multi` bot liczy
**wszystkie** strategie z `ACTIVE_STRATEGIES` w każdym cyklu, każdą dla każdej
strony, po której wolno jej grać, i wchodzi, gdy zgodzi się co najmniej
`MIN_ENTRY_VOTES` z nich — po jednej stronie. Pełne reguły są w
`docs/strategies.md`.

| | Wejście | Wyjście | Znacznik | Na żywo |
|---|---|---|---|---|
| `ict` | cena przebija znany dołek (swing low albo dołek poprzedniego dnia UTC) i zamyka się z powrotem nad nim, zamknięcie nad ostatnim swing high (zmiana struktury), potem **pierwszy powrót** do luki (*fair-value gap*), którą zostawił ten ruch | tylko stop i cel na giełdzie | `i` | **tak**, long i short |
| `breakout` | close powyżej maksimum z `BREAKOUT_LOOKBACK=20` świec | brak reguły (`BREAKOUT_EXIT_LOOKBACK=0`), tylko stop i cel; przy M > 0 close poniżej minimum z M świec (trzymaj M krótsze niż 20, `config.warnings()` ostrzega, gdy jest dłuższe) | `b` | **nie**, wyłączony |

Obie czytają domyślnie **świece 15-minutowe** (`ENTRY_TIMEFRAME=15m`).

Na żywo jest `ACTIVE_STRATEGIES=ict` i `SHORT_STRATEGIES=ict`, każde
ustawienie `ICT_` na wartości domyślnej. To **eksperyment, nie przewaga**: w
powtórce na 90 dniach (niżej) `ict` przegrał po obu stronach. Zostaje na żywo
do pierwszego przeglądu, po około 100 transakcjach na stronę. Sam robi około
37 longów i 34 shorty miesięcznie, czyli około trzech miesięcy, a jeśli
powtórka się sprawdzi, kosztuje około 100 USDT demo miesięcznie, głównie po
stronie shortów.

`breakout` zostaje w kodzie, ale wyłączony: jego long bez reguły wyjścia jako
jedyny przeszedł powtórkę, i to ledwo, a puszczony obok `ict` zabierał mu
symbole — `ict` long spadł ze 111 transakcji do 15 (według tradera dlatego, że
`breakout` long trzymał większość symboli przez większość czasu). Wraca po
przeglądzie `ict` albo wcześniej, jeśli potwierdzi go powtórka na 365 dniach
ze spadającą połową. Jego short przegrał w obu połowach i nie jest w
`SHORT_STRATEGIES`.

> Żadna z nich nie jest **przewagą, którą sami odkryliśmy.** Zakładaj, że
> każda traci po prowizjach, dopóki powtórka i transakcje na żywo nie powiedzą
> inaczej.

### Filtr reżimu: to on sprawia, że kilka strategii naraz ma sens

Strategie kupują różne rzeczy: `breakout` siłę, `ict` dołek pod znanym
minimum, gdy struktura już zawróciła w górę. Puszczone obok siebie bez filtra
potrafią zająć przeciwne stanowiska na tym samym rynku.

`REGIME_FILTER` to rozwiązuje. Long wolno otworzyć tylko **nad** wolną
średnią `REGIME_PERIOD`, short tylko **pod** nią. Wszystkie strategie grają
wtedy z wolnym trendem, różnią się tylko tym, co wyzwala wejście, a long i
short nigdy nie walczą o ten sam symbol. To warunek sprawdzany w każdym
cyklu, a nie przecięcie, na które się czeka: na świecach 15-minutowych linia
to około dwóch dni trendu. Przy włączonym filtrze na danej świecy może wejść
najwyżej jedna strona strategii.

`EXIT_ON_REGIME_BREAK` zamykałby pozycję, gdy cena przejdzie na drugą stronę
tej średniej — i jest **wyłączony**. W dawnej powtórce na 26 dniach
wyłączenie poprawiło wynik w obu połowach próby: ówczesne strategie kupujące
dołki kupowały dokładnie to, co opada w stronę tej linii, więc to wyjście
wyrzucało transakcje tuż przed tym, zanim zaczęły działać. Pozycji `ict` to
wyjście i tak nie dotyczy. Prawdziwą ochroną jest stop loss po stronie
giełdy. Filtr nadal blokuje **nowe** wejścia po złej stronie linii.

### Short to long na odwróconym wykresie

Każda reguła jest napisana raz, dla longów. Short to ta sama reguła czytana
na wykresie odwróconym do góry nogami (każda cena z minusem, high i low
zamienione miejscami). Wybicie 20-świecowego maksimum na odwróconym wykresie
to wybicie minimum na prawdziwym, luka w górę to luka w dół, a ATR się nie
zmienia. Dlatego obie strony nie mogą się rozjechać i nie ma osobnych
ustawień dla shortów.

- `SHORT_STRATEGIES` mówi, które strategie mogą też grać short; puste =
  tylko long. Od 2026-10-08: `ict`.
- Trzymany short zamyka **lustrzana** reguła wyjścia jego strategii; gdy ta
  strategia nie jest aktywna, decyduje `UNKNOWN_OWNER_EXIT`, też na
  odwróconym wykresie. Stronę pozycji bot zawsze czyta z giełdy, nigdy z
  pliku.
- Jedna pozycja na symbol: sygnał na drugą stronę przy trzymanej pozycji jest
  ignorowany i tylko zapisywany w logu (*"holding long, short setup
  ignored"*, widoczne przy `LOG_DETAIL=true`). Bot nigdy nie odwraca ani nie
  zwalcza własnej pozycji.
- Stop shorta siedzi nad ceną, cel pod nią. Przycięcie do likwidacji działa
  tak samo: 3,33% nad ceną przy 15x.
- Linia `OPENED` i push przy otwarciu podają stronę: `long` albo `short`.

### Stop i cel wynikają z układu na wykresie

Dawne strategie wychodziły według wskaźnika, ignorując cenę wejścia: dwa
zamknięcia na trzy robiła reguła wyjścia strategii, zwykle tuż pod kreską,
więc zysk zjadały prowizje. Teraz `ict` sam wskazuje, gdzie jest w błędzie i
dokąd cena zmierza:

- **stop** pod strukturą układu (z buforem `ICT_STOP_BUFFER_ATR`), ale
  przynajmniej `ICT_STOP_FLOOR_ATR` × ATR pod bieżącą ceną;
- **cel** na najbliższym nietkniętym poziomie, który istniał przed
  przebiciem dołka (swing high albo maksimum poprzedniego dnia UTC). Gdy jest
  bliżej niż `ICT_MIN_RR` × ryzyko, transakcji **nie ma**, bo prowizje
  zjadłyby zysk. Gdy takiego poziomu brak, cel to `ICT_FALLBACK_TARGET_R` ×
  ryzyko.

Transakcji też nie ma, gdy bieżąca cena jest na dnie luki lub pod nim albo
wyżej niż `ICT_MAX_CHASE_ATR` × ATR nad jej szczytem, ani gdy przycięcie do
likwidacji wciągnęłoby stop w strukturę. Wszystko to liczy jedna funkcja,
`executor.planEntry`, ta sama na żywo i w powtórce. `breakout` (i wymuszone
wejście testowe) dostaje stop i cel z ATR — patrz *Model ryzyka* niżej.

### Kto otwarł pozycję, ten ją zamyka

Bybit w trybie one-way trzyma jedną pozycję na symbol, więc kilka strategii
nie może trzymać kilku pozycji na tym samym rynku. Przy wejściu zapisywane jest
więc, **która** strategia je otwarła — w `state/owners.json` oraz w
`orderLinkId`, dzięki czemu widać to także w interfejsie Bybita. Wyjścia
pilnuje ta sama strategia: pozycję `ict` zamyka tylko stop albo cel, pozycję
`breakout` jego kanał, jeśli `BREAKOUT_EXIT_LOOKBACK` jest większe od zera.

To nie jest kosmetyka. Zmierzone na pierwszym zestawie kilku strategii
(`trend` i `meanrev`, obie już usunięte): na tym samym wzroście właściciel
`trend` trzymał pozycję, a `meanrev` w tej samej chwili wychodził — bo dla
niego odbicie już się wydarzyło. Zamykanie pozycji jednej strategii regułą
innej psuje obie naraz.

Gdy właściciel jest nieznany (pozycja otwarta ręcznie, plik stanu utracony
albo strategia już nieaktywna), decyduje `UNKNOWN_OWNER_EXIT`: `any` / `all`
/ `regime`. Ustawione jest `regime`: przy `any` któraś reguła wyjścia prawie
zawsze mówiła „zamknij", więc taka pozycja wylatywała w jednym cyklu, płacąc
tylko prowizję. Plik stanu jest najlepszym staraniem i **nigdy** nie decyduje
o tym, czy pozycja istnieje — tu jedynym źródłem prawdy pozostaje giełda.

**Pozycja `breakout` otwarta przed wyłączeniem `breakout`** ma właśnie
nieznanego właściciela, bo jej strategia nie jest już aktywna. Zostaje więc
na swoim stopie i celu na giełdzie i nigdy nie wyjdzie regułą kanału — celowo:
przełączenie nie ma niczego zamykać tylko za cenę prowizji. Zamknęłoby ją
tylko wyjście przy złamaniu reżimu, gdyby `EXIT_ON_REGIME_BREAK` włączyć.
Zlecenie, które ją otworzyło, ma literę `b`, więc nie liczy się do przeglądu
`ict`.

### Jeden zegar: 15 minut

Bot ma być szybki: dużo wejść dziennie, a nie księga swingowa czekająca dniami
na sygnał z wykresu godzinowego. Dlatego wszystkie strategie czytają
domyślnie te same **świece 15-minutowe** (`ENTRY_TIMEFRAME=15m`, a
`STRATEGY_TIMEFRAMES` i `MAX_OPEN_PER_STRATEGY` są puste). Nawet maksimum i
minimum poprzedniego dnia `ict` składa ze świec 15-minutowych.

**Osobne zegary nadal są możliwe, ale niosą ze sobą regułę.**
`STRATEGY_TIMEFRAMES` daje strategii własną ramę (np. `breakout:1h`), więc bot
może prowadzić jednocześnie księgę swingową i szybką, w tym samym cyklu i na
tym samym koncie. Gdy tylko ramy się różnią, `MAX_OPEN_PER_STRATEGY` staje się
konieczny: reguła 15-minutowa odpala wielokrotnie częściej niż godzinowa i
zajmuje wszystkie symbole, zanim wolna do któregoś dojdzie, więc „szybko **i**
dziennie" po cichu zamienia się w „tylko szybko". Tak było na starym układzie
z dwoma zegarami.

Ten sam problem jest też na jednym zegarze: jedna pozycja na symbol oznacza,
że strategia trzymająca symbol blokuje na nim wszystkie inne. Tak `breakout`
zabrał symbole `ict` w powtórce. Gdyby obie miały znów chodzić razem, trader
proponuje `MAX_OPEN_PER_STRATEGY=breakout:4` — niezmierzone, bo powtórka gra
każdy symbol osobno.

Filtr reżimu liczony jest **osobno dla każdej strategii, na jej własnej
ramie**. Na świecach 15-minutowych linia 200-okresowa to około dwóch dni
trendu, na godzinowych około ośmiu.

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

Zmierzone na czterdziestu symbolach handlowanych wtedy na świecach
godzinowych: 2×ATR wahało się od **1,24% na BTC do 27% na najdzikszym alcie**
— różnica dwudziestokrotna. Na dzisiejszych dziesięciu płynnych symbolach, na
świecach 15-minutowych, stop 3×ATR to w medianie od 0,78% (BTC) do 2,92%
(NEAR), ale w gwałtownym okresie NEAR doszedł do 11%. Żadna pojedyncza
wartość `LEVERAGE` tego nie obsłuży, więc zabezpieczenie działa per
transakcja:

| Ustawienie | Co robi |
|---|---|
| `MAX_STOP_FRACTION_OF_LIQUIDATION=0.5` | przycina stop do połowy dystansu do likwidacji |
| `MIN_STOP_ATR_MULT=1.0` | **odrzuca transakcję**, gdy po przycięciu stop byłby węższy niż 1×ATR |

Drugie jest równie ważne jak pierwsze. Stop wciśnięty w zwykły ruch świecy to
nie ochrona, tylko zaplanowane wyjście, które następna świeca uruchomi
przypadkiem. Na symbolu o ATR równym 13% ceny nie istnieje stop, który
jednocześnie mieści się w likwidacji przy 15x i cokolwiek znaczy — i uczciwą
odpowiedzią jest nie brać tej transakcji.

Dlatego wyższa dźwignia to nie „więcej zysku", tylko więcej odrzuconych
transakcji. Sprawdzone: przy 30x (900 USDT nominału, te same 30 USDT
depozytu) ta sama powtórka na 90 dniach nie przepuściła niczego nowego, a
odrzuceń z powodu przyciętego stopu było dla `ict` 34 zamiast 6 po stronie
longów i 48 zamiast 2 po stronie shortów; `breakout` long z 10-świecowym
wyjściem dostał 84 odrzucenia za zbyt ciasny stop zamiast 0 (86 bez reguły
wyjścia). Shorty `ict` straciły przy 30x mniej, bo przycięcie odrzuciło ich
najszersze stopy, ale longi `ict` straciły więcej w pierwszej połowie, więc
według zasady „osobno dla każdej strony" to żaden zysk — filtr szerokości
stopu, jeśli w ogóle, powinien być osobnym ustawieniem, a nie dźwignią.
Właściciel zrezygnował z 30x i zostało 15x.

### Stop liczony od bieżącej ceny, nie od ostatniej świecy

Sygnały liczone są na zamkniętych świecach i dawniej stop też był liczony od
zamknięcia ostatniej z nich — czyli od ceny sprzed nawet całej świecy. Rynek
w tym czasie nie czeka.

Złapane na tym koncie 2026-09-15:

```
ARB   zamkniecie swiecy   0.14991
      stop loss           0.14491   (3,3% pod zamknieciem, tak mialo byc)
      wypelnienie         0.14703   (rynek juz 1,9% nizej)
      -> stop tylko 1,4% pod wejsciem, trafiony po 3 minutach
```

Kolejne wejścia z tym samym stopem wypełniały się coraz bliżej niego, ostatnie
dokładnie na nim (cała sekwencja jest w *Jeden sygnał, jedna transakcja*).
Potem Bybit zaczął odrzucać zlecenia
(*"StopLoss ... should lower than base_price"*).

Teraz bot tuż przed wejściem pyta giełdę o **cenę ostatniej transakcji**
(ticker) i od niej liczy wszystko: wielkość pozycji, stop, cel, przycięcie do
likwidacji, próg 1×ATR, a dla `ict` także to, czy cena nie uciekła z luki.
Ceny wypełnienia użyć się nie da, bo SL i TP są doklejone do samego
zlecenia, więc muszą być znane, zanim ono powstanie — dzięki temu pozycja ani
przez chwilę nie jest bez ochrony. Bieżąca cena to najbliższe uczciwe
przybliżenie.

Co się **nie** zmieniło: sygnały i ATR nadal liczone są wyłącznie na
zamkniętych świecach. Zmieniło się tylko to, od czego mierzony jest stop.

**Nie ma ceny, nie ma transakcji.** Jeśli ticker nie poda użytecznej ceny, bot
pomija wejście i zapisuje powód w logu. Celowo nie wraca do zamknięcia świecy,
bo to właśnie ta nieaktualna cena narobiła szkód. To nie jest błąd przebiegu:
następny cykl zapyta ponownie. Co innego, gdy samo zapytanie o ticker się
wysypie (sieć, giełda nie odpowiada): wtedy ten symbol kończy się błędem, jak
przy każdym innym nieudanym zapytaniu do giełdy, i przebieg jest czerwony.

W logu wejścia widać obie ceny i to, o ile rynek zdążył się ruszyć, np.
`@~0.147030 live, last close 0.149910 (-1.92%)`.

### Jeden sygnał, jedna transakcja

Sygnał wejścia żyje przez `SIGNAL_LOOKBACK_BARS` świec, bo bot budzi się co
kilka minut i nie może przegapić zdarzenia. Skutek uboczny: pozycja wybita
stopem w jednym cyklu zostałaby w następnym kupiona **jeszcze raz, na tym
samym sygnale**. Zasada „jedna pozycja na symbol" tego nie powstrzyma — pilnuje,
żeby nie było dwóch pozycji naraz, a nie czterech po kolei.

Złapane na tym koncie 2026-09-15: ARB, jeden sygnał usuniętej już strategii
`trend`, za każdym razem ten sam stop 0.14491.

| Wejście | Wypełnienie | Wybite stopem |
|---|---|---|
| 20:13:29 | 0.14703 | 20:16:27 |
| 20:18:24 | 0.14507 | 6 s później |
| 20:20:23 | 0.14555 | 10 s później |
| 20:22:25 | 0.14491 | w tej samej sekundzie |

Po czwartym razie Bybit zaczął odrzucać zlecenia.

Dlatego `REENTRY_COOLDOWN_BARS=3`: po **każdym** zamknięciu pozycji na
symbolu — stop loss, take profit, wyjście strategii, zamknięcie ręczne — nic
nie wejdzie w ten symbol przez tyle świec strategii, która chce wejść. Na
świecach 15-minutowych to 45 minut, dla `breakout:1h` trzy godziny. Trzy to
tyle samo, co `SIGNAL_LOOKBACK_BARS`, więc zanim bot znów może wejść w ten
symbol, sygnał stojący za poprzednią transakcją wypada z okna: **każdy sygnał
jest grany raz**. W dawnej powtórce na 26 dniach ta przerwa dała około 5
punktów w spadającej połowie i 35 w rosnącej. `0` ją wyłącza.

- Czasy zamknięć bot bierze **z historii Bybita**, a nie z pliku na laptopie,
  więc restart ani druga kopia bota ich nie skasuje. Te same dane zasilają
  powiadomienia o zamknięciach. Okno odczytu samo się wydłuża, gdy przerwa
  sięga dalej wstecz niż `CLOSED_LOOKBACK_MINUTES`, a linia
  `closed-position check` w logu podaje okno faktycznie odczytane.
- Wstrzymane wejście widać w logu jako `re-entry cooldown, N minute(s) left`,
  a zaraz za tym sam sygnał — da się później ocenić, czego chciała strategia.
- Gdy historii nie da się odczytać, bot zapisuje w logu
  `re-entry cooldown unavailable this cycle` i przez ten jeden cykl handluje
  bez przerwy. Jedno nieudane zapytanie nie jest warte zatrzymania bota.

### Skąd się wzięła lista dziesięciu symboli

Dziesięć kontraktów krypto o największym obrocie 24h, odczytanych wprost z
giełdy 2026-09-24: BTC, ETH, XRP, SOL, ZEC, NEAR, HYPE, DOGE, 1000PEPE, BCH.
Dziesięć zamiast dawnych czterdziestu: na świecach 15-minutowych dziesięć
symboli dawało czterem ówczesnym strategiom około 25 wejść dziennie, a
najpłynniejsze rynki to te, na których zlecenie rynkowe wypełnia się
najbliżej ceny, od której liczono stop. Ranking zmienia się z dnia na dzień —
lepiej go co jakiś czas zmierzyć na nowo, niż ufać tej liście w
nieskończoność.

Bybit ma **762** wieczyste kontrakty USDT i pobranie wszystkich nie wchodzi w
grę: jeden cykl trwałby około 14 minut, czyli prawie całą 15-minutową świecę,
więc każdy cykl działałby na świecy, której już nie ma.

Surowy ranking po obrocie **nie jest samym krypto**. Siedzą w nim
tokenizowane akcje i surowce — 2026-09-24 w pierwszej dwunastce były SOXL,
ropa (CL) i złoto (XAU), wcześniej także AAPL, TSLA, MSTR, SKHYNIX i XAG —
które chodzą wedle innego zegara i innej logiki niż cokolwiek, pod co te
strategie budowano. Nic w metadanych API ich nie odróżnia: `contractType`
jest identyczny, `fetch_currencies()` nic nie zwraca na hoście demo, a handel
idzie 24/7, więc test „martwych świec" też ich nie wyłapuje. Zostały więc
wykluczone z nazwy, a wszystko, czego tożsamości nie dało się ustalić na
pewno, po prostu pominięto zamiast zgadywać.

### Model ryzyka: ATR zamiast sztywnych procentów

Dotyczy wejść bez układu na wykresie, czyli `breakout` i wymuszonego wejścia
testowego (`ict` liczy stop i cel z wykresu, patrz wyżej). `RISK_MODEL=atr`
wylicza stop i cel z ATR, czyli z realnej zmienności danego instrumentu.
Zmierzone na żywo w tej samej chwili: ATR14 to 0,48% ceny na BTC i 1,02% na
XRP — ponad dwukrotna różnica. Sztywne 5% jest więc na jednym rynku za
ciasne, a na drugim za szerokie, i to rynek decyduje na którym. Gdy ATR nie
da się policzyć, kod sam wraca do wartości procentowych.

Stop to **3×ATR**, cel **6×ATR**. Podręcznikowe 2×ATR to stop na świecach
dziennych; świeca 15-minutowa to w porównaniu głównie szum i potrzebuje
więcej miejsca. W dawnej powtórce na 26 dniach 3/6 wygrało z 2/4 w obu
połowach próby.

### Powtórka: jak bot sprawdza strategię, zanim zagra nią na żywo

`scripts/replay.py` przepuszcza historyczne świece 15-minutowe przez **ten sam
kod**, którym bot gra na żywo: te same reguły wejścia i wyjścia, ten sam
`planEntry` do stopu i celu. Wypełnienie na otwarciu następnej świecy, stop i
cel sprawdzane na jej zakresie (gdy świeca dotknie obu, wygrywa stop),
prowizja 0,055% i poślizg 0,02% na każde wypełnienie.

```bash
python scripts/replay.py                       # każda strategia i strona osobno, 90 dni
python scripts/replay.py --multi               # ACTIVE_STRATEGIES i SHORT_STRATEGIES razem, jak na żywo
python scripts/replay.py --set ICT_MIN_RR=2    # jedna zmiana wobec bazy
python scripts/replay.py --wave 1 --jobs 4     # przygotowany zestaw wariantów
```

**Zasada zachowania.** Transakcje dzielone są na dwie połowy w kalendarzowym
środku okna, według czasu wejścia. Wiersz przechodzi, gdy jego suma % jest na
plusie w jednej połowie i nie na minusie w drugiej. Zmiana zostaje tylko
wtedy, gdy poprawia wynik w **obu** połowach — jedna dobra połowa to rynek,
nie przewaga. Ustawienie `ICT_` musi pomóc i longom, i shortom `ict`, każdej
stronie w obu połowach, liczone osobno, bo w rynku idącym w jedną stronę
wszystko, co obcina przegrywającą stronę, wygląda dobrze. Ustawienie, które
czytają obie strategie (np. `REGIME_PERIOD`, `REENTRY_COOLDOWN_BARS`,
`LEVERAGE`), nie może przy tym pogorszyć longów `breakout` w żadnej połowie.
Obok sumy % trzeba czytać średnie R: zaliczenie przy średnim R na zero lub
poniżej to zasługa doboru symboli, nie sygnału.

Zasadą jest, że strategia, która nie przechodzi, jest usuwana z kodu, a
strona, która nie przechodzi, wypada z `SHORT_STRATEGIES`. `ict` to świadomy
wyjątek właściciela: gra na żywo jako eksperyment do przeglądu po około 100
transakcjach na stronę.

Czego powtórka (jeszcze) nie robi: gra każdy symbol osobno, więc nie zna
limitów pozycji między symbolami, i nie porównuje zmian z losowym usunięciem
tylu samo transakcji. Zawsze ustaw `LEVERAGE` i `POSITION_NOTIONAL_USDT`
(patrz punkt 2 na górze).

### Skąd te ustawienia: powtórka na 90 dniach

Od 2026-07-09 do 2026-10-07 UTC, dziesięć symboli, świece 15-minutowe,
prowizja 0,055% + poślizg 0,02% na wypełnienie, 450 USDT nominału przy 15x,
każda strategia i strona osobno. Wyniki to zsumowane procenty z pojedynczych
transakcji (USDT = suma % × 4,5). **Obie połowy rosły** (dziesięć symboli
średnio +31% i +33%), więc żaden werdykt nie widział jeszcze spadającego
rynku: „przeszło" znaczy tylko „działało na dwóch rosnących rynkach".

| Strategia | Strona | 1. połowa | 2. połowa | Werdykt |
|---|---|---|---|---|
| `ict` | long | -0,15% | -17,74% | nie przeszło |
| `ict` | short | -12,27% | -39,59% | nie przeszło |
| `pullback` | long | +14,37% | -18,69% | nie przeszło |
| `pullback` | short | -7,56% | +0,17% | nie przeszło |
| `trend` | long | -71,40% | +7,68% | nie przeszło |
| `trend` | short | -53,15% | -47,30% | nie przeszło |
| `breakout` (wyjście 10 świec, ówczesne domyślne) | long | +15,23% | -60,89% | nie przeszło |
| `breakout` (wyjście 10 świec, ówczesne domyślne) | short | -95,57% | -221,94% | nie przeszło |
| `breakout`, `BREAKOUT_EXIT_LOOKBACK=0` (domyślne od 3e8c699) | long | +69,86% | +13,85% | **przeszło** |

Decyzja właściciela z 2026-10-08, na podstawie odczytu tradera:

- **`ict` na żywo po obu stronach**, wszystkie ustawienia domyślne, jako
  eksperyment do pierwszego przeglądu (wyżej, w *Strategie*). Żaden wariant
  `ICT_` ani `REGIME_PERIOD` nie pomógł obu stronom `ict` w obu połowach,
  więc żaden nie wszedł.
- **`pullback` i `trend` usunięte z kodu** (-50 i -542 USDT przez 90 dni,
  obie strony razem).
- **`breakout` w kodzie, wyłączony, z `BREAKOUT_EXIT_LOOKBACK=0`.** Ta zmiana
  poprawiła wynik w obu połowach (+54,6 i +74,7 punktu). Transakcji było
  przy tym o około 28% mniej (pozycje trwają dłużej), ale losowe usunięcie
  tylu samo transakcji dałoby tylko około -4,5 i +16,0 — zysk jest w wyniku
  transakcji, które zostały. Mimo to wynik jest cienki. W przeliczeniu na
  jednostkę ryzyka jest lekko ujemny w obu połowach (średnie R -0,00 i
  -0,05); zysk w sumie % biorą nieliczne zmienne symbole z najszerszymi
  stopami. ZEC, PEPE i NEAR zrobiły +89,9 punktu, więcej niż całe +83,7;
  pozostałe siedem razem straciło 6,2, sam DOGE -28,9. W każdej połowie
  największe obsunięcie było większe niż zysk tej połowy: około 419 USDT przy
  +314, potem około 491 przy +62.
- **Wielkość: 15x, 450 USDT nominału (30 USDT depozytu), najwyżej 10
  pozycji.** Najwyżej 4 500 USDT nominału naraz, czyli 9% z 50 000 na koncie
  demo.

Wcześniej ustawienia dobierała dawna powtórka na 26 dniach do 2026-09-24:
cztery ówczesne strategie, tylko long, bez poślizgu i bez trailingu. Jej
pierwsza połowa spadała. Z niej pochodzą trzy świece przerwy przed ponownym
wejściem, wyłączone wyjście przy złamaniu reżimu i stop 3×ATR / cel 6×ATR;
każda z tych zmian pomogła w obu połowach i nadal obowiązuje. Trzy z jej
czterech strategii są już usunięte, więc nie jest to już dowód na to, co gra.

**Dlaczego wejścia to zdarzenia, a wyjścia to stany.** Wejście szuka
*zdarzenia* (wybicia, powrotu do luki) w ostatnich `SIGNAL_LOOKBACK_BARS`
zamkniętych świecach — wejście dlatego, że warunek "nadal obowiązuje",
wchodziłoby w kółko. Wyjście patrzy na *aktualny stan*, bo bot budzi się co
kilka minut i gubi większość świec; wyjście zdefiniowane jako pojedyncza
świeca przecięcia prędzej czy później zostałoby przegapione i zostawiło
pozycję bez opieki. "Czy jestem teraz po złej stronie wskaźnika" przegapić
się nie da.

Bot liczy sygnały **wyłącznie na świecach zamkniętych** — ostatnia świeca z API
jest w trakcie formowania i jej close to po prostu bieżąca cena, która migocze.

---

## Zasady, na których to stoi

- **Giełda jest jedynym źródłem prawdy.** Żadnego stanu lokalnego o pozycjach.
  Runner jest jednorazowy, więc lokalny obraz "co mam" byłby zgadywaniem.
- **Jedna pozycja na symbol.** Przed wejściem bot pyta Bybit, czy już czegoś
  nie trzyma.
- **`orderLinkId` per (symbol, strategia, kubełek czasu).** Kubełek trwa
  `ORDER_BUCKET_SECONDS`, domyślnie tyle, co odstęp między cyklami.
  Ponowienie wewnątrz tego samego kubełka trafia na ten sam ID i zostaje
  odrzucone — dokładnie o to chodzi, bo retry po niejednoznacznym timeoucie
  nie może otworzyć drugiej pozycji. Następny cykl to nowy kubełek.
- **Litera w `orderLinkId` mówi, która strategia otworzyła pozycję**, np.
  `cf-ETHUSDT-i-…`: `i` = `ict`, `b` = `breakout`, `c` = zamknięcie przez
  bota. Litery `pullback` i `trend` (`p`, `t`) są zarezerwowane i nigdy nie
  trafią do nowej strategii, żeby stare zlecenia w historii Bybita nie
  zostały przypisane nie tej regule. Litery `meanrev` i `scalp` (`m`, `s`)
  jeszcze nie są — nowej strategii lepiej ich nie dawać.
- **Strategia to wyjmowalny klocek.** Reguły, ustawienia, wpis w
  `.env.example`, opis w `docs/strategies.md` i testy — usunięcie strategii
  to usunięcie klocka, a jej ustawienia trafiają na listę wycofanych, żeby
  stara linia w `.env` dawała ostrzeżenie.
- **Wielkość zlecenia zaokrąglana W DÓŁ** do `qtyStep`. Poniżej `minOrderQty`
  bot odmawia zamiast po cichu podbić pozycję.
- **Dźwignia:** błąd 110043 (*"leverage not modified"*) jest połykany, każdy
  inny leci dalej.
- **Tryb one-way** (`positionIdx = 0`), nie hedge.
- **Błąd = czerwony run.** Kod wyjścia niezerowy, żeby było widać w apce.

### `POSITION_NOTIONAL_USDT` to nominał, nie depozyt

To `qty * cena`. Margin, który realnie blokujesz, to mniej więcej
`nominał / dźwignia`. Nominał 450 przy dźwigni 15 = 30 USDT depozytu.
Podniesienie dźwigni **nie zmienia** wielkości pozycji, tylko to, jak blisko
siedzi likwidacja.

---

## Powiadomienia

Push leci przy: otwarciu pozycji, wykryciu zamknięcia, wyjściu ze strategii,
błędzie krytycznym.

Zamknięcia bot wykrywa odpytując `/v5/position/closed-pnl` za ostatnie
`CLOSED_LOOKBACK_MINUTES`. Żeby to samo zamknięcie nie waliło ci w telefon na
każdym runie przez cały okres okna, lista już zgłoszonych ID leży w
`state/notified.json`. Plik trzyma dokładnie te zamknięcia, które zwrócił
ostatni odczyt — starsze wypadły z okna i nie wrócą. To **czysta kosmetyka**
— zgubiony plik kosztuje duplikat powiadomienia, nigdy duplikat transakcji. Jeśli chcesz to wyłączyć,
ustaw `NOTIFIED_STATE_FILE=""`.

### Dlaczego pozycja się zamknęła

Każde zamknięcie mówi, co je spowodowało: w logu na końcu linii
`CLOSED ...` (`why=...`), a w telefonie w ostatniej linijce
powiadomienia (`why: ...`). Nie trzeba już grzebać w historii zleceń Bybita.

| `why:` | Co się stało |
|---|---|
| `stop loss` | zadziałał stop loss ustawiony przy wejściu |
| `take profit` | cena doszła do celu |
| `trailing stop` | zadziałał trailing stop (tylko gdy jest włączony; domyślnie nie jest) |
| `liquidation` | **likwidacja**: giełda sama zamknęła pozycję i zabrała cały depozyt |
| `bot exit` | bot zamknął pozycję, bo reguła strategii kazała wyjść (przychodzi wtedy też osobny push "Exit signal" z powodem) |
| `closed outside the bot (...)` | zamknięte poza botem, np. ręcznie w aplikacji Bybita; w nawiasie Bybit podaje, skąd przyszło zlecenie (`CreateByClosing` to przycisk zamknięcia pozycji); bez nawiasu, jeśli tego nie poda |
| `unknown` | nie udało się tego sprawdzić |

Skąd bot to wie: likwidację Bybit zaznacza wprost w rekordzie zamknięcia
(`execType=BustTrade`). W pozostałych przypadkach bot raz dopytuje historię
zleceń o zlecenie, które zamknęło pozycję, i patrzy, kto je wysłał. Zlecenia
bota mają w `orderLinkId` jego prefiks (`ORDER_LINK_PREFIX`, domyślnie `cf`),
więc da się je odróżnić od twoich ręcznych. Te wartości sprawdzono na
prawdziwych zleceniach z konta demo, nie tylko w dokumentacji.

Każde zamknięcie jest sprawdzane tylko raz, przy pierwszym zgłoszeniu. Jeśli
sprawdzenie się nie uda, powiadomienie i tak przychodzi, tylko z
`why: unknown` — brak powodu nie może zjeść informacji o zamknięciu.

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
