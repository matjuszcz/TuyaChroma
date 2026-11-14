# Tuya Chroma – integracja Home Assistant

Niestandardowa integracja Home Assistant, która:

* **steruje oświetleniem Tuya lokalnie** (żarówki, taśmy LED – bez chmury, przez [tinytuya](https://github.com/jasonacox/tinytuya)),
* **łączy się z Razer Chroma** (klawiatura, mysz, podkładka, słuchawki, keypad, Chroma Link) przez Chroma SDK REST API z Razer Synapse,
* **synchronizuje kolory** – np. klawiatura i mysz Razer przyjmują kolor i jasność żarówki Tuya (lub dowolnego innego światła w HA).

> 🎮 **Efekty Razer Chroma z gier na żarówkach Tuya** (bez Home Assistant): zobacz program na Windows **[TuyaChroma Bridge](bridge/README.md)**.

## Instalacja

**HACS:** HACS → Integracje → ⋮ → *Niestandardowe repozytoria* → `https://github.com/matjuszcz/TuyaChroma` (kategoria *Integracja*) → zainstaluj *Tuya Chroma* → restart HA.

**Ręcznie:** skopiuj katalog `custom_components/tuya_chroma` do `config/custom_components/` w Home Assistant i zrestartuj HA.

Następnie: *Ustawienia → Urządzenia i usługi → Dodaj integrację → Tuya Chroma*. Kreator pyta, co dodać:

| Opcja | Co tworzy |
|---|---|
| **Razer Chroma (PC z Synapse)** | Jedną encję `light` na każde wybrane urządzenie Razer (np. `light.gaming_pc_keyboard`) |
| **Oświetlenie Tuya (sieć lokalna)** | Encję `light` dla żarówki/taśmy Tuya (kolor HS, temperatura barwowa, jasność) |

Integrację możesz dodać wiele razy: każdą żarówkę Tuya i każdy komputer z Razer osobno.

## Konfiguracja Tuya

Są trzy sposoby dodania świateł Tuya:

### 1. Z konta Tuya / Smart Life (zalecane)

Nie potrzebujesz konta deweloperskiego ani żadnych kluczy.

1. *Dodaj integrację → Tuya Chroma → **Światła Tuya z mojego konta Tuya / Smart Life***.
2. W aplikacji **Smart Life** (lub **Tuya Smart**): *Ja → ⚙ Ustawienia → Konto i bezpieczeństwo → Kod użytkownika* (*User Code*). Wpisz go w HA.
3. HA pokaże **kod QR**. Zeskanuj go aplikacją (ikona skanera w prawym górnym rogu ekranu głównego), potwierdź logowanie i kliknij **Zatwierdź**.
4. Integracja pobiera z konta listę urządzeń z ich **Local Key** i przez ~8 s nasłuchuje w sieci lokalnej, żeby znaleźć ich **adresy IP i wersję protokołu**.
5. Zaznacz urządzenia (światła są zaznaczone domyślnie). Jeśli któregoś nie znaleziono w sieci, HA poprosi o jego IP.
6. Każde urządzenie staje się osobną pozycją integracji. Wersja protokołu jest sprawdzana automatycznie.

Konto Tuya jest używane **tylko podczas dodawania**. Integracja nie zapisuje tokenów logowania, a światła są potem sterowane wyłącznie lokalnie. Logowanie korzysta z tej samej autoryzacji „Home Assistant” co wbudowana integracja Tuya.

### 2. Z wbudowanej integracji Tuya

Jeśli masz już skonfigurowaną oficjalną integrację **Tuya** w HA, w menu pojawi się **Światła Tuya z wbudowanej integracji Tuya**. Klucze zostaną wtedy wzięte z niej, bez ponownego logowania.

### 3. Ręcznie

Wybierz **Oświetlenie Tuya (dane wpisane ręcznie)** i podaj IP, Device ID, Local Key i wersję protokołu (najczęściej 3.3; nowsze urządzenia 3.4/3.5). Dane możesz uzyskać np. przez `python -m tinytuya wizard` (wymaga konta na [iot.tuya.com](https://iot.tuya.com)).

### Uwagi

* Nadaj urządzeniom Tuya **stałe adresy IP** w routerze.
* Wykrywanie w sieci wymaga, by HA odbierał rozgłoszenia UDP (porty 6666, 6667, 7000). Działa to w Home Assistant OS i w Dockerze z `network_mode: host`. W innych przypadkach wpisz IP ręcznie.
* **Zmiana IP, Local Key lub wersji protokołu**: *pozycja urządzenia → Konfiguruj*. Local Key zmienia się po ponownym sparowaniu urządzenia w aplikacji. Najprościej wtedy usunąć urządzenie i zaimportować je z konta jeszcze raz.
* Urządzenia Zigbee/BLE podłączone przez bramkę nie mają własnego klucza lokalnego i nie są obsługiwane.

Obsługiwane są typowe żarówki i taśmy Tuya (DP 1–9 lub 20–28; RGB, RGB+CCT, CCT, ściemniane). Typ jest wykrywany automatycznie.

## Konfiguracja Razer Chroma

Wymagany jest Windows z **Razer Synapse** i włączonym modułem **Chroma Connect / Chroma SDK** (*Synapse → Chroma → Connect → Apps → włącz „Chroma Apps"*).

Chroma SDK nasłuchuje tylko na `localhost` (port `54235`, a sesja dostaje kolejny port, zwykle `54236`). Jeśli Home Assistant działa na innym urządzeniu (Raspberry Pi, NAS itp.), udostępnij te porty w sieci LAN. Na PC z Synapse uruchom **PowerShell jako administrator** (podstaw adres IP swojego komputera w LAN):

```powershell
$ip = "192.168.1.50"   # IP tego komputera w sieci LAN
foreach ($port in 54235..54240) {
  netsh interface portproxy add v4tov4 listenaddress=$ip listenport=$port connectaddress=127.0.0.1 connectport=$port
}
netsh advfirewall firewall add rule name="Razer Chroma SDK (Home Assistant)" dir=in action=allow protocol=TCP localport=54235-54240 remoteip=localsubnet
```

Test z innego komputera: `curl http://192.168.1.50:54235/razer/chromasdk` powinien zwrócić JSON z wersją SDK.

Cofnięcie zmian: `netsh interface portproxy reset` i usunięcie reguły zapory.

W kreatorze HA podaj IP komputera, port `54235` i wybierz urządzenia. Integracja:

* tworzy sesję Chroma SDK i podtrzymuje ją co 10 s (heartbeat),
* po wyłączeniu PC lub restarcie Synapse oznacza encje jako niedostępne, a po powrocie łączy się ponownie i przywraca ostatnie kolory,
* po usunięciu integracji / zatrzymaniu HA zamyka sesję, więc Synapse wraca do efektów z profilu.

> Chroma SDK nie udostępnia aktualnego koloru, więc stan encji Razer jest „zakładany" (optymistyczny) i przywracany po restarcie HA.

## Synchronizacja Tuya ⇄ Razer

Na integracji kliknij **Konfiguruj** i wybierz **Światło źródłowe**. Od tej chwili wszystkie światła tej pozycji odwzorowują jego stan (włącz/wyłącz, kolor, jasność, temperaturę barwową).

* **Razer podąża za Tuya:** skonfiguruj pozycję *Razer Chroma* → źródło `light.lampka_biurko` (Tuya).
* **Tuya podąża za Razer:** skonfiguruj pozycję *Tuya* → źródło `light.gaming_pc_keyboard`.
* Źródłem może być **dowolne** światło w HA (Hue, WLED, grupa świateł, oficjalna integracja Tuya…).

⚠️ Nie ustawiaj synchronizacji w obie strony jednocześnie (A → B i B → A), bo utworzysz pętlę.

## Przykładowe automatyzacje

Klawiatura na czerwono, gdy ktoś dzwoni do drzwi:

```yaml
automation:
  - alias: "Dzwonek – czerwona klawiatura"
    triggers:
      - trigger: state
        entity_id: binary_sensor.dzwonek
        to: "on"
    actions:
      - action: light.turn_on
        target:
          entity_id:
            - light.gaming_pc_keyboard
            - light.gaming_pc_mouse
        data:
          rgb_color: [255, 0, 0]
          brightness: 255
```

Wieczorem przyciemnij lampkę Tuya i ustaw ciepłą barwę:

```yaml
      - action: light.turn_on
        target:
          entity_id: light.lampka_biurko
        data:
          color_temp_kelvin: 2700
          brightness_pct: 30
```

## Rozwiązywanie problemów

Włącz logi debug w `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.tuya_chroma: debug
    tinytuya: debug
```

| Problem | Rozwiązanie |
|---|---|
| Tuya: logowanie kodem QR się nie udaje | Kod użytkownika musi pochodzić z tej samej aplikacji, którą skanujesz. Kod QR jest jednorazowy: po błędzie HA pokazuje nowy. |
| Tuya: urządzenie bez IP po imporcie | HA nie odbiera rozgłoszeń UDP albo urządzenie jest offline. Wpisz IP ręcznie. |
| Tuya: „Nie można się połączyć" | Sprawdź IP, Local Key i wersję protokołu (spróbuj 3.3/3.4/3.5). Zamknij aplikację Smart Life na telefonie – niektóre urządzenia obsługują tylko jedno połączenie lokalne naraz. |
| Razer: „Nie można się połączyć" | Czy Synapse działa i Chroma Apps jest włączone? Czy `curl http://IP:54235/razer/chromasdk` działa z innego komputera? |
| Razer: sesja się tworzy, ale kolory się nie zmieniają | Port sesji jest spoza zakresu 54235–54240: w logach debug znajdziesz `Chroma session created: http://IP:PORT/...` – dodaj ten port do portproxy. |

## Rozwój

```bash
pip install -r requirements_test.txt
pytest
```

Testy symulują Chroma SDK, żarówkę Tuya, logowanie do konta Tuya i rozgłoszenia UDP.
