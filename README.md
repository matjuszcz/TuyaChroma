# TuyaChroma

Program na Windows, który łączy **żarówki RGB Tuya** z **Razer Chroma**: gdy gra wysyła efekty Razer Chroma, wybrane żarówki w pokoju świecą tymi samymi kolorami.

Na co dzień żarówki dalej działają z **Home Assistant**. Gdy włączasz komputer, program startuje razem z Windows i **przejmuje wybrane żarówki na czas efektów z gier**. Potem **oddaje je w stanie sprzed przejęcia**.

## Jak to działa

```
Gra z Razer Chroma ─► Razer Synapse (Chroma SDK) ─► TuyaChroma ─► żarówki Tuya (sieć lokalna)
                                                                  ▲
                         Home Assistant (poza grą) ───────────────┘
```

* **Logowanie kontem Tuya / Smart Life jako zwykły użytkownik.** Wpisujesz kod użytkownika z aplikacji i skanujesz kod QR. Program pobiera listę żarówek i ich klucze lokalne, a potem sam znajduje je w sieci (adres IP i wersję protokołu).
* **Wybierasz żarówki**, którymi ma sterować Razer, oraz dla każdej źródło koloru (klawiatura, mysz, Chroma Link…) i strefę (np. lewa strona klawiatury).
* **Kolory z gier** program odczytuje z usługi Razer Chroma SDK (tak samo jak [Artemis RGB](https://artemis-rgb.com/)) i wysyła do żarówek **lokalnie** (bez chmury, kilka razy na sekundę).
* **Przejęcie i oddanie:**
  * gdy gra zaczyna efekt, program zapamiętuje aktualny stan żarówki (ustawiony np. w HA) i przejmuje ją;
  * gdy gra kończy efekt, wstrzymasz sterowanie, zamkniesz program, uśpisz lub wyłączysz komputer, żarówka **wraca do zapamiętanego stanu** i dalej działa z HA.

### Co jest przechwytywane

| Źródło | Działa? |
|---|---|
| Gry i aplikacje z Razer Chroma (Chroma SDK), np. efekty reagujące na rozgrywkę | ✅ |
| Efekty z profilu Synapse / Chroma Studio (fala, spectrum itp. poza grą) | ❌ Synapse udostępnia je tylko aplikacjom z oficjalnym Razer App ID. Poza grą żarówki wracają do stanu z HA. |

## Wymagania

* Windows 10/11 z **Razer Synapse** (3 lub 4) i modułem **Chroma**. W Menedżerze zadań powinna być widoczna usługa `RzSDKService`.
* Żarówki **RGB** Tuya (Smart Life / Tuya Smart) w tej samej sieci lokalnej co komputer.
* Konto w aplikacji Smart Life lub Tuya Smart, w którym są te żarówki.

## Instalacja

1. Pobierz `TuyaChroma-windows.zip`:
   * z zakładki **[Releases](../../releases)**, jeśli jest wydanie, albo
   * z **[Actions](../../actions)** → ostatnie udane uruchomienie *Build TuyaChroma (Windows)* → *Artifacts* (wymaga zalogowania w GitHub).
2. Rozpakuj go do stałego folderu, np. `C:\Program Files\TuyaChroma` albo `C:\TuyaChroma`.
3. Uruchom `TuyaChroma.exe`. Program nie ma podpisu cyfrowego, więc Windows SmartScreen może go zablokować. Kliknij wtedy *Więcej informacji → Uruchom mimo to*.

## Pierwsze uruchomienie

### 1. Konto Tuya

Zakładka **Konto Tuya**:

1. W aplikacji Smart Life: **Ja → ⚙ Ustawienia → Konto i bezpieczeństwo → Kod użytkownika** (*User Code*).
2. Wpisz ten kod i kliknij **Pokaż kod QR**.
3. Zeskanuj kod QR aplikacją (ikona skanera w prawym górnym rogu ekranu głównego) i potwierdź logowanie.

Program pobierze urządzenia i przez około 10 s poszuka ich w sieci. Konto służy tylko do odczytania kluczy, a sterowanie odbywa się wyłącznie lokalnie. Program nie zapisuje tokenów logowania. Klucze żarówek są zapisywane zaszyfrowane (Windows DPAPI), w pliku `%APPDATA%\TuyaChroma\config.json`.

> Program korzysta z tego samego logowania kodem QR co integracja Tuya w Home Assistant. Dlatego aplikacja Smart Life pokaże je jako autoryzację **„Home Assistant”**. Twoja dotychczasowa integracja Tuya w HA działa dalej.

### 2. Wybór żarówek

Zakładka **Żarówki**:

* kliknij w kolumnę **✓** przy żarówkach, którymi ma sterować Razer;
* zaznacz żarówkę, żeby ustawić **źródło koloru**, **strefę** i **jasność**, a potem kliknij **Zapisz**;
* **Testuj (mignij)**: żarówka mignie na czerwono, zielono i niebiesko, a potem wróci do poprzedniego stanu;
* **Szukaj w sieci**: ponownie znajduje adresy IP (np. po zmianie routera).

Wskazówki:
* **Chroma Link** to kanał, który wiele gier ustawia specjalnie dla oświetlenia otoczenia.
* Dwie lampy można rozdzielić strefami, np. lewą na *Lewa strona*, a prawą na *Prawa strona*.

### 3. Ustawienia

* **Uruchamiaj razem z Windows** (domyślnie włączone): program startuje po zalogowaniu do Windows, zminimalizowany do zasobnika obok zegara.
* **Gdy żadna gra nie wysyła efektów:** *wróć do stanu sprzed przejęcia* (domyślnie, czyli sterowanie z HA) albo *zostaw ostatni kolor z gry*.

## Na co dzień

* Program działa w **zasobniku** (zielona ikona obok zegara). Zamknięcie okna chowa je do zasobnika.
* Menu ikony: **Otwórz**, **Wstrzymaj sterowanie** (żarówki od razu wracają do HA) albo **Włącz sterowanie**, **Zakończ**.
* Kropka w oknie programu: 🟢 efekt z gry jest aktywny, 🔵 program czeka na grę, 🟡 wymaga uwagi (np. Synapse nie działa albo nie wybrano żarówek).

## Uwagi o Home Assistant

* **Oficjalna integracja Tuya** (przez chmurę) działa bez zmian. W czasie efektu z gry polecenia z HA zostaną po chwili nadpisane kolorami z gry. Po grze żarówka wraca do stanu sprzed przejęcia.
* Jeśli masz **automatyzacje HA**, które często zmieniają te żarówki, w czasie gry będą się „przepychać” z programem. Możesz np. wyłączać je, gdy komputer jest włączony.
* **Lokalne integracje** (LocalTuya, Tuya Local) mogą w czasie działania programu pokazywać żarówkę jako niedostępną. Część żarówek Tuya przyjmuje tylko jedno połączenie lokalne naraz.

## Rozwiązywanie problemów

Logi znajdziesz w *Ustawienia → Otwórz folder z logami* (`%APPDATA%\TuyaChroma\log.txt`).

| Problem | Rozwiązanie |
|---|---|
| „Razer niedostępny: Shared memory … not found” | Synapse nie działa albo moduł Chroma nie jest zainstalowany. Sprawdź, czy działa `RzSDKService`. |
| Program czeka na grę, a gra ma efekty | Gra nie używa Razer Chroma albo ma je wyłączone w swoich ustawieniach. |
| Żarówka: „nie znaleziono w sieci” | Kliknij *Szukaj w sieci*. Jeśli nie pomoże, wpisz IP ręcznie (znajdziesz je w routerze lub aplikacji Smart Life). Komputer i żarówki muszą być w tej samej sieci. |
| Żarówka: błąd połączenia | Kliknij *Testuj*. Wyłącz lokalne integracje tej żarówki w HA. Po ponownym sparowaniu żarówki w aplikacji zaloguj się ponownie, bo zmienia się wtedy jej klucz. |
| „nieobsługiwana (brak koloru RGB)” | To żarówka tylko z białym światłem, więc nie może pokazywać kolorów. |
| Logowanie QR się nie udaje | Kod użytkownika musi pochodzić z tej samej aplikacji, którą skanujesz. Kod QR jest ważny około 3 minut. |

## Rozwój

```bash
pip install -r requirements.txt pytest
pytest
python -m tuyachroma        # uruchomienie z kodu źródłowego
```

Exe budowany jest w GitHub Actions (`.github/workflows/build.yml`). Wydanie powstaje po wypchnięciu tagu `v*`.

Licencje: odczyt danych Razer Chroma bazuje na [RazerSdkReader](https://github.com/diogotr7/RazerSdkReader) (MIT). Szczegóły w [NOTICE](NOTICE).
