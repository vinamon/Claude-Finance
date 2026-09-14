# Claude-Finance

Bot handlowy na koncie **Bybit Demo Trading**. Python, odpalany z harmonogramu
GitHub Actions co 5 minut. Wirtualne środki, zero kontaktu z kontem realnym.

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
| `.github/workflows/smoke-test.yml` | kroki 1 i 2 jako przycisk w Actions |

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

`DUMMY_MODE`, `SYMBOLS`, `STRATEGY`, `POSITION_NOTIONAL_USDT`, `LEVERAGE`,
`STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `TRAILING_STOP_PCT`,
`TRAILING_ACTIVATION_PCT`, `ENTRY_TIMEFRAME`, `EXIT_TIMEFRAME` (pomiń,
żeby dziedziczyło po `ENTRY_TIMEFRAME`),
`SIGNAL_LOOKBACK_BARS`, `SMA_FAST_PERIOD`, `SMA_SLOW_PERIOD`, `RSI_PERIOD`,
`RSI_OVERSOLD`, `RSI_OVERBOUGHT`, `BREAKOUT_LOOKBACK`,
`CLOSED_LOOKBACK_MINUTES`.

`SYMBOLS` jest listą po przecinku, w formacie ccxt:
`BTC/USDT:USDT,ETH/USDT:USDT`

### 5. Testy: przyciskiem, bez terminala

Nie potrzebujesz lokalnego klona ani terminala. Workflow **smoke-test** robi
kroki 1 i 2 za ciebie:

*Actions → smoke-test → Run workflow →* wybierz `both` → *Run*.

Wynik czytasz w logach joba. Klucze nie opuszczają GitHub Secrets. Opcje:

| Wybór | Co sprawdza |
|---|---|
| `connection` | połączenie z demo, saldo, specyfikacja twoich symboli |
| `ntfy` | czy push dociera na telefon |
| `both` | oba, po kolei |

> `smoke-test` pojawi się w zakładce Actions dopiero, gdy workflow znajdzie się
> na gałęzi domyślnej. GitHub pokazuje przycisk *Run workflow* wyłącznie dla
> workflowów z brancha domyślnego. Czyli: najpierw merge, potem przycisk.

<details>
<summary>Jeśli jednak masz lokalnie gita</summary>

```bash
pip install -r requirements.txt

export BYBIT_API_KEY=...
export BYBIT_API_SECRET=...
export NTFY_TOPIC=...

python scripts/test_connection.py   # krok 1: saldo demo
python scripts/test_ntfy.py         # krok 2: push na telefon
```

</details>

### 6. Pierwszy run z telefonu

Apka GitHub → repo → **Actions** → workflow **trade** → **Run workflow**.

> **Kolejność ma znaczenie.** Zarówno cron, jak i przycisk *Run workflow*
> działają wyłącznie dla workflowów leżących na gałęzi domyślnej. Zanim
> cokolwiek odpalisz: ustaw Secrets, potem zmerguj do `main`. Odwrotna
> kolejność znaczy czerwony run co 5 minut na `CONFIG ERROR`, dopóki nie
> dosypiesz kluczy. Przy pustym `SYMBOLS` cron kończy się zielono i nic nie
> robi, więc merge bez ustawionych symboli jest bezpieczny.

Przy `DUMMY_MODE=true` (domyślnie) ten ręczny run **wymusza wejście na każdym
skonfigurowanym symbolu**, ignorując rynek — po to, żeby przepchnąć cały
pipeline. Runy z crona w dummy mode nie robią nic. Trzy symbole = trzy
pozycje z jednego kliknięcia, więc na start ustaw jeden.

Kiedy działa: ustaw `DUMMY_MODE=false` i bot zacznie słuchać strategii.

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

| Polecenie | Co robi |
|---|---|
| `python run.py` | jeden cykl i koniec |
| `python run.py --loop` | krazy co 5 minut do Ctrl+C |
| `python run.py --loop --interval 1` | to samo, co minute |
| `python run.py --force-entry` | jeden cykl, ktory otwiera pozycje testowa |

W `.env` ustawiasz zachowanie domyslne:

```
AUTOSTART=true      # samo "python run.py" zaczyna krazyc
AUTOSTART=false     # samo "python run.py" robi jeden cykl
```

Flagi z linii polecen zawsze wygrywaja z `.env`.

Harmonogram siedzi w samym skrypcie, nie w cronie ani Harmonogramie zadan
Windows. Jeden mechanizm zamiast trzech zaleznych od systemu, i widzisz
odliczanie do nastepnego cyklu na wlasne oczy.

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

Jedna zmienna decyduje: `STRATEGY = "trend" | "meanrev" | "breakout"`.
Żaden inny plik się nie zmienia.

| | Wejście | Wyjście | Parametry |
|---|---|---|---|
| `trend` | SMA szybka przecina wolną od dołu (złoty krzyż) | szybka poniżej wolnej | `SMA_FAST_PERIOD`, `SMA_SLOW_PERIOD` |
| `meanrev` | RSI przecina poziom wyprzedania od dołu | RSI osiąga wykupienie | `RSI_PERIOD`, `RSI_OVERSOLD`, `RSI_OVERBOUGHT` |
| `breakout` | close powyżej maksimum z N świec | close poniżej minimum z N świec | `BREAKOUT_LOOKBACK` |

> Wszystkie trzy to **podręcznikowe punkty startowe do strojenia, nie strategie
> z przewagą.** Zakładaj, że każda traci po prowizjach, dopóki twój własny
> backtest nie powie inaczej.

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

## Keepalive

GitHub wyłącza harmonogramy w repo bez commitów przez 60 dni. Sam fakt, że
workflow się odpala, tego licznika **nie** resetuje — liczy się aktywność w
repo. Dlatego `keepalive.yml` raz w tygodniu pushuje pusty commit.

Wymaga, żeby Actions mogło pisać: *Settings → Actions → General → Workflow
permissions → **Read and write permissions***.

---

## Bezpieczeństwo

- Repo jest publiczne. Klucze żyją wyłącznie w Secrets, nigdy w kodzie.
- Bot nie wypisuje ani kluczy, ani tematu ntfy.
- `exchange.py` odmawia startu, jeśli którykolwiek URL nie wskazuje na
  `api-demo.bybit.com`.
- `DUMMY_MODE` domyślnie `true`. Handel z sygnałów włącza się świadomie.
