# Tuya Chroma – integracja Home Assistant

Niestandardowa integracja Home Assistant, która:

* **steruje oświetleniem Tuya lokalnie** (żarówki, taśmy LED – bez chmury, przez [tinytuya](https://github.com/jasonacox/tinytuya)),
* **łączy się z Razer Chroma** (klawiatura, mysz, podkładka, słuchawki, keypad, Chroma Link) przez Chroma SDK REST API z Razer Synapse,
* **synchronizuje kolory** – np. klawiatura i mysz Razer przyjmują kolor i jasność żarówki Tuya (lub dowolnego innego światła w HA).

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

Potrzebujesz: adres IP urządzenia, **Device ID**, **Local Key** i wersję protokołu (najczęściej 3.3, nowsze urządzenia 3.4/3.5).

1. Załóż darmowe konto deweloperskie na [iot.tuya.com](https://iot.tuya.com), utwórz projekt Cloud i podłącz do niego konto z aplikacji Smart Life / Tuya (instrukcja: [tinytuya – Setup Wizard](https://github.com/jasonacox/tinytuya#setup-wizard---getting-local-keys)).
2. Na komputerze w tej samej sieci:
   ```bash
   pip install tinytuya
   python -m tinytuya wizard   # pobiera Device ID i Local Key
   python -m tinytuya scan     # pokazuje IP i wersję protokołu
   ```
3. Wpisz te dane w kreatorze. Nadaj urządzeniu w routerze stały adres IP.

> Local Key zmienia się po ponownym sparowaniu urządzenia w aplikacji – wtedy usuń i dodaj urządzenie w HA na nowo.

Obsługiwane są typowe żarówki/taśmy Tuya (DP 1–9 lub 20–28; RGB, RGB+CCT, CCT, ściemniane). Typ jest wykrywany automatycznie.

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
| Tuya: „Nie można się połączyć" | Sprawdź IP, Local Key i wersję protokołu (spróbuj 3.3/3.4/3.5). Zamknij aplikację Smart Life na telefonie – niektóre urządzenia obsługują tylko jedno połączenie lokalne naraz. |
| Razer: „Nie można się połączyć" | Czy Synapse działa i Chroma Apps jest włączone? Czy `curl http://IP:54235/razer/chromasdk` działa z innego komputera? |
| Razer: sesja się tworzy, ale kolory się nie zmieniają | Port sesji jest spoza zakresu 54235–54240: w logach debug znajdziesz `Chroma session created: http://IP:PORT/...` – dodaj ten port do portproxy. |

## Rozwój

```bash
pip install pytest-homeassistant-custom-component tinytuya==1.21.1
pytest
```

Testy symulują Chroma SDK oraz żarówkę Tuya.
