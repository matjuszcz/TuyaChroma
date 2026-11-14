# TuyaChroma Bridge – efekty Razer Chroma na żarówkach Tuya

Program na Windows, który **przechwytuje kolory efektów Razer Chroma z gier i aplikacji** i na bieżąco wysyła je **lokalnie** (bez chmury i bez Home Assistant) do żarówek i taśm LED Tuya.

Przykład: w grze klawiatura miga na czerwono przy otrzymaniu obrażeń, więc lampka w pokoju też robi się czerwona.

## Jak to działa

* Gry z obsługą Razer Chroma wysyłają efekty do usługi **Razer Chroma SDK** (`RzSDKService`, część Synapse).
* Usługa udostępnia te kolory w pamięci współdzielonej dla „Chroma Emulatora”. Bridge je stamtąd odczytuje, tak samo jak robi to [Artemis RGB](https://artemis-rgb.com/) (biblioteka [RazerSdkReader](https://github.com/diogotr7/RazerSdkReader)). **Razer App ID nie jest potrzebny.**
* Z wybranego urządzenia (klawiatura, mysz, Chroma Link…) i strefy (np. lewa część klawiatury) wyliczany jest jeden kolor dla każdej żarówki.
* Kolor trafia do żarówki przez lokalny protokół Tuya (wersje 3.1–3.5). Żarówki typu B dostają go w **trybie „music”**, przeznaczonym do szybkich zmian.
* Gdy gra przestaje sterować oświetleniem, a także po zamknięciu programu, żarówki **wracają do poprzedniego stanu**.

### Co jest przechwytywane, a co nie

| Źródło | Działa? |
|---|---|
| Gry i aplikacje z Razer Chroma (Chroma SDK), np. efekty reagujące na rozgrywkę | ✅ |
| Aplikacje Chroma działające przez SDK (np. wizualizery audio z Chroma Apps) | ✅ |
| Efekty z profilu Synapse / Chroma Studio (fala, spectrum itp. bez gry) | ❌ (Synapse nie przesyła ich przez SDK; to wymagałoby Razer App ID) |

## Wymagania

* Windows 10/11 z **Razer Synapse 3 lub 4** i zainstalowanym modułem **Chroma** (musi działać usługa `RzSDKService`).
* Żarówki Tuya w tej samej sieci lokalnej co komputer, a do każdej: **Device ID, IP, Local Key** i wersja protokołu.

## Instalacja

1. Pobierz `TuyaChromaBridge-windows.zip`:
   * z zakładki **[Releases](../../releases)**, jeśli jest tam wydanie `bridge-v…`, albo
   * z zakładki **Actions → TuyaChroma Bridge (Windows) → ostatnie uruchomienie → Artifacts**.
2. Rozpakuj go do osobnego folderu, np. `C:\TuyaChromaBridge`.
3. Program nie ma podpisu cyfrowego, więc Windows SmartScreen może go zablokować. Kliknij wtedy *Więcej informacji → Uruchom mimo to*.

## Konfiguracja

Otwórz w tym folderze terminal (PowerShell) i wykonaj:

```powershell
.\TuyaChromaBridge.exe init     # tworzy config.json
.\TuyaChromaBridge.exe scan     # znajduje żarówki Tuya w sieci: id, ip, wersja
```

**Local Key** każdej żarówki:
* jeśli dodałeś żarówki do Home Assistant integracją **Tuya Chroma**: *Ustawienia → Urządzenia i usługi → Tuya Chroma → żarówka → Konfiguruj*, pole *Local key*;
* albo przez `pip install tinytuya` i `python -m tinytuya wizard` (wymaga darmowego konta na iot.tuya.com).

Uzupełnij `config.json` (np. w Notatniku):

```json
{
  "fps": 20,
  "idle": "restore",
  "bulbs": [
    {
      "name": "Lampka biurko",
      "id": "bf0123456789abcdef",
      "ip": "192.168.1.60",
      "local_key": "0123456789abcdef",
      "version": "3.3",
      "source": "keyboard",
      "zone": "all",
      "mode": "vivid",
      "brightness": 1.0,
      "min_brightness": 0.05,
      "method": "auto",
      "min_interval": 0.15
    }
  ]
}
```

| Pole | Znaczenie |
|---|---|
| `name`, `id`, `ip`, `local_key`, `version` | Dane żarówki (`version`: 3.1, 3.3, 3.4, 3.5) |
| `source` | Skąd brać kolor: `keyboard`, `mouse`, `mousepad`, `headset`, `keypad`, `chromalink` |
| `zone` | `all`, `left`, `center`, `right`, `top`, `bottom`, numer diody (np. `3`) lub lista (`[0, 1, 2]`) |
| `mode` | `vivid`: wyraziste kolory, czyli podświetlone klawisze wygrywają z ciemnym tłem (domyślnie); `average`: zwykła średnia |
| `brightness` | Mnożnik jasności 0–1 |
| `min_brightness` | Minimalna jasność zapalonego koloru 0–1, żeby słabe kolory nie wyglądały na zgaszone |
| `method` | `auto`: tryb music, jeśli żarówka go ma (szybszy); `music`; `colour`: zwykły tryb kolorowy |
| `min_interval` | Minimalny odstęp między zmianami w sekundach (0.15 ≈ 6–7 zmian/s). Zwiększ, jeśli żarówka nie nadąża |
| `fps` (globalnie) | Jak często czytać kolory Razera |
| `idle` (globalnie) | `restore`: gdy żadna gra nie steruje, przywróć poprzedni stan żarówek; `keep`: zostaw ostatni kolor |

**Wskazówka:** wiele gier ustawia **Chroma Link** (`"source": "chromalink"`) specjalnie dla oświetlenia otoczenia. Dioda `0` to zwykle kolor główny, a `1–4` dodatkowe. Kilka żarówek można przypisać do różnych stref, np. lewa lampa do `"zone": "left"`, prawa do `"zone": "right"`.

## Uruchomienie

```powershell
.\TuyaChromaBridge.exe test     # każda żarówka mignie na czerwono/zielono/niebiesko
.\TuyaChromaBridge.exe colors   # podgląd kolorów wyliczanych z Razera (bez wysyłania)
.\TuyaChromaBridge.exe          # właściwa praca; Ctrl+C kończy i przywraca żarówki
```

Dodatkowe opcje: `-c ścieżka\config.json` (inny plik konfiguracji), `-v` (szczegółowe logi).

**Autostart:** naciśnij `Win+R`, wpisz `shell:startup` i w otwartym folderze utwórz skrót do `TuyaChromaBridge.exe`.

## Uwagi

* **Home Assistant nie jest potrzebny.** Jeśli te same żarówki są w HA:
  * oficjalna integracja Tuya (chmura) działa bez zmian;
  * lokalne integracje (Tuya Chroma, LocalTuya) mogą w czasie gry pokazywać żarówkę jako niedostępną. Część żarówek Tuya przyjmuje tylko jedno połączenie lokalne naraz, a Bridge trzyma swoje połączenie otwarte.
* Program nie jest oficjalnym produktem Razer. Korzysta z tego samego, nieudokumentowanego mechanizmu co Artemis RGB, więc aktualizacja Synapse może go zepsuć.
* Zbyt częste zmiany mogą przeciążyć tańsze żarówki. W razie „zacinania” zwiększ `min_interval` (np. do 0.3).

## Rozwiązywanie problemów

| Problem | Rozwiązanie |
|---|---|
| `Shared memory for keyboard not found` | Synapse nie działa albo moduł Chroma nie jest zainstalowany (*Synapse → Moduły → Chroma Connect*). Sprawdź w Menedżerze zadań, czy działa `RzSDKService`. |
| Kolory się nie zmieniają, `colors` pokazuje „brak efektu” | Gra nie używa Razer Chroma albo ma je wyłączone w ustawieniach. Sprawdź, czy w czasie gry klawiatura pokazuje efekty z gry. |
| Żarówka: błąd połączenia | Sprawdź IP, Local Key i wersję. Wyłącz na chwilę inne lokalne integracje tej żarówki. |
| Żarówka miga / nie nadąża | Zwiększ `min_interval` albo ustaw `"method": "colour"`. |

## Rozwój

```bash
pip install -r requirements.txt pytest
pytest
```

Testy dekodowania używają prawdziwych zrzutów pamięci z Synapse, a test `test_windows.py` sprawdza pełną ścieżkę Windows (uruchamiany w GitHub Actions na Windows).

Licencje: patrz [NOTICE](NOTICE). Fragmenty pochodzą z RazerSdkReader (MIT).
