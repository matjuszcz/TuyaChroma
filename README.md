# TuyaChroma

Program na Windows, który łączy **żarówki RGB Tuya** z **Razer Chroma**: gdy gra wysyła efekty Razer Chroma, wybrane żarówki w pokoju świecą tymi samymi kolorami.

Na co dzień żarówki dalej działają z **Home Assistant** (albo z aplikacji Tuya). Gdy włączasz komputer, program startuje razem z Windows i **przejmuje wybrane żarówki, dopóki działa Razer Synapse**:

* **w grze z Razer Chroma** żarówki pokazują kolory z gry;
* **bez gry** odtwarzają wybrany efekt, np. **Wave** jak w Twoim profilu Synapse;
* **po wyłączeniu komputera** (albo zamknięciu Synapse lub programu) **wracają do stanu sprzed przejęcia** i dalej działają z HA.

## Jak to działa

```
Komputer włączony:  gra z Chroma ─► Razer Synapse ─► TuyaChroma ─► żarówki Tuya (sieć lokalna)
                    (bez gry: TuyaChroma odtwarza efekt, np. Wave)
Komputer wyłączony: Home Assistant / aplikacja Tuya ───────────────► żarówki Tuya
```

* **Logowanie kontem Tuya / Smart Life jako zwykły użytkownik.** Wpisujesz kod użytkownika z aplikacji i skanujesz kod QR. Program pobiera listę żarówek i ich klucze lokalne, a potem sam znajduje je w sieci (adres IP i wersję protokołu).
* **Wybierasz żarówki**, którymi ma sterować Razer, oraz dla każdej źródło koloru (klawiatura, mysz, Chroma Link…) i strefę (np. lewa strona klawiatury).
* **Kolory z gier** program odczytuje z usługi Razer Chroma SDK (tak samo jak [Artemis RGB](https://artemis-rgb.com/)) i wysyła do żarówek **lokalnie** (bez chmury, kilka razy na sekundę).
* **Gdy Razer działa, ale nie ma gry** (do wyboru w *Ustawieniach*):
  * **Pełna synchronizacja**: program odtwarza wybrany **profil efektów** jednocześnie na **wszystkich urządzeniach Razer Chroma** i na żarówkach. Wszystko świeci identycznie i równo. Gdy uruchomisz grę z Chroma, gra przejmuje urządzenia i żarówki. Po zamknięciu programu urządzenia Razer wracają do profilu z Synapse.
  * **Profil tylko na żarówkach**: urządzenia Razer zostają przy profilu z Synapse, a żarówki odtwarzają podobny efekt (domyślnie). Nie jest on zsynchronizowany z Razerem co do sekundy.
  * **Oddaj żarówki do HA** albo **zostaw ostatni kolor z gry**.

* **Przejęcie i oddanie:**
  * przy przejęciu program zapamiętuje aktualny stan żarówki (ustawiony np. w HA albo w aplikacji Tuya);
  * gdy wyłączysz lub uśpisz komputer, zamkniesz Synapse lub program albo wstrzymasz sterowanie, żarówka **wraca do zapamiętanego stanu** i dalej działa z HA.

### Profile efektów (jak w Synapse / Chroma Studio)

Zakładka **Profile efektów** to lista profili, podobna do profili w Synapse. Każdy profil to efekt plus kolory, tryb kolorów (jeden, dwa, losowe), kierunek, prędkość, gęstość i jasność. Podgląd na żywo pokazuje efekt na klawiaturze, myszy, podkładce, słuchawkach, keypadzie, Chroma Link i żarówkach. Aktywny profil (★) przełączysz też z menu ikony w zasobniku (*Profil efektu*).

| Efekt | Opis |
|---|---|
| **Wave** | fala tęczy w wybranym kierunku (w prawo, w lewo, w dół, w górę) |
| **Spectrum Cycling** | wszystko razem przechodzi przez kolory tęczy |
| **Breathing** | oddychanie: jeden kolor, dwa na zmianę albo losowe |
| **Static** | stały kolor |
| **Wheel** | tęczowe koło kręcące się wokół środka urządzenia |
| **Starlight** | migające gwiazdy (gęstość, kolory) |
| **Fire** | ogień od dołu urządzenia |
| **Reactive** | podświetla naciśnięty klawisz; pozostałe urządzenia błyskają |
| **Ripple** | fala rozchodząca się od naciśniętego klawisza |
| **Ambient Awareness** | kolory z ekranu na urządzeniach i żarówkach |

**Obsługiwane urządzenia Razer:** program wysyła efekt do wszystkich sześciu kategorii Chroma SDK, w pełnej siatce diod każdej z nich. Dla nowszych urządzeń używa rozszerzonych układów:

| Kategoria | Siatka | Przykłady |
|---|---|---|
| klawiatura | 6×22, rozszerzona **8×24** (dodatkowe diody: podświetlenie spodu, boki) | BlackWidow, Huntsman, Ornata, DeathStalker, Cynosa, laptopy Blade |
| mysz | 9×7 (z bocznymi paskami) | DeathAdder, Basilisk, Viper, Naga, Cobra |
| podkładka | 15, rozszerzona **20** stref | Firefly, Goliathus Chroma, Strider Chroma, stacje: Base Station, Mouse Dock |
| słuchawki | 5 | Kraken (V3/V4, Kitty), BlackShark, Barracuda z Chroma |
| keypad | 4×5 | Tartarus, Orbweaver |
| Chroma Link | 5 | głośniki Nommo i Leviathan, mikrofony Seiren z Chroma, Core X Chroma, stacje Thunderbolt Chroma, kontroler ARGB, podstawki i podkładki chłodzące Chroma, urządzenia partnerów z Chroma Connect |

Jeśli starsze urządzenie albo starsza wersja Synapse nie obsługuje rozszerzonego układu, program sam przełącza się na podstawowy.

**Klawiatury 60% i TKL** (np. **Huntsman Mini**): Chroma SDK ma jedną siatkę 6×22 dla wszystkich klawiatur, a mniejsze klawiatury mają klawisze tylko w jej części. Program rozpoznaje układ z wykrytej klawiatury (albo z ustawienia *Układ klawiatury Razer*). Dzięki temu:
* efekty (np. cała tęcza w Wave) rozkładają się na klawiszach, które istnieją;
* strefy żarówek (*lewa*, *środek*, *prawa*) dotyczą klawiszy Twojej klawiatury, a nie np. nieistniejącej klawiatury numerycznej.

**Przykład zestawu:** Huntsman Mini (klawiatura, układ 60%), Basilisk V3 (mysz), Mouse Bungee V3 Chroma i Chroma ARGB Controller (taśmy i wentylatory, przez Chroma Link). Program wysyła efekt do wszystkich kategorii, więc obejmuje każde z tych urządzeń. Zastrzeżenia od strony Synapse:
* Mouse Bungee V3 według zgłoszeń użytkowników czasem ignoruje efekty Chroma także w samym Synapse;
* Chroma ARGB Controller u niektórych osób pokazuje w Chroma tylko 4 z 6 podłączonych wentylatorów.

**Czego Chroma SDK nie obejmuje** (dotyczy też gier):
* **Kontrolery sterowane osobną aplikacją**, np. *Razer Wolverine V2 Chroma*. Jego podświetlenie ustawia się w aplikacji „Razer Controller Setup for Xbox”, nie w Synapse, więc żaden program Chroma nim nie steruje.
* **Lampy i taśmy Razer Aether** konfigurowane w aplikacji *Razer Gamer Room*. Nie jest pewne, czy po dodaniu ich do Synapse przyjmują efekty z Chroma SDK.
* **Ogólna zasada:** jeśli urządzenie zmienia się razem z profilami w Synapse i reaguje na gry z Chroma, zadziała też z TuyaChroma.

**Uwagi:**
* *Reactive* i *Ripple* odczytują, **który** klawisz naciśnięto (tylko jego pozycję, nic nie jest zapisywane), tak samo jak Synapse. Program robi to tylko przy tych dwóch efektach.
* *Ambient Awareness* kilka razy na sekundę robi pomniejszoną kopię ekranu, żeby wyliczyć kolory. Nic nie jest zapisywane ani wysyłane.
* W *Ambient* każde światło pokazuje **swoją część ekranu**: kilka świateł dzieli pulpit na pasy od lewej do prawej (w kolejności z listy w zakładce *Światła*), a światło ze strefą *Lewa strona*, *Środek*, *Prawa strona*, *Góra* lub *Dół* – tę część. Liczą się kolory, które się wyróżniają; ciemne tło i pasek zadań prawie nie przyciemniają świateł. Uwaga: gry w trybie pełnoekranowym na wyłączność bywają dla Windows niewidoczne (czarny obraz) – wtedy pomaga tryb „okno bez ramki”.
* W *Synapse → Chroma → Chroma Apps (Aplikacje)* musi być włączona obsługa aplikacji Chroma, a TuyaChroma nie może być tam wyłączony.

### Co jest przechwytywane

| Źródło | Działa? |
|---|---|
| Gry i aplikacje z Razer Chroma (Chroma SDK), np. efekty reagujące na rozgrywkę | ✅ |
| Efekty z profilu Synapse / Chroma Studio (wave, spectrum itp. poza grą) | ❌ nie da się ich odczytać: Synapse udostępnia je tylko aplikacjom z oficjalnym Razer App ID. Zamiast tego w trybie **pełnej synchronizacji** program sam odtwarza profil na urządzeniach Razer i żarówkach (równo). W trybie *tylko na żarówkach* żarówki odtwarzają podobny efekt. |

## Wymagania

* Windows 10/11 z **Razer Synapse** (3 lub 4) i modułem **Chroma**. W Menedżerze zadań powinna być widoczna usługa `RzSDKService`.
* Żarówki **RGB** Tuya (Smart Life / Tuya Smart) w tej samej sieci lokalnej co komputer.
* Konto w aplikacji Smart Life lub Tuya Smart, w którym są te żarówki.

## Instalacja

1. Pobierz `TuyaChroma-windows.zip` z **[najnowszego wydania](../../releases/latest)**.
2. Rozpakuj go do stałego folderu **w swoim profilu użytkownika**, np. `%LOCALAPPDATA%\TuyaChroma` (wpisz to w pasek adresu Eksploratora) albo `C:\TuyaChroma`. **Nie** używaj `C:\Program Files`: zapis tam wymaga uprawnień administratora, więc automatyczna aktualizacja nie mogłaby podmienić programu.
   Jeśli uruchomisz program prosto z archiwum (WinRAR/7-Zip rozpakowują go wtedy do folderu tymczasowego), sam zaproponuje instalację w `%LOCALAPPDATA%\TuyaChroma`. Wystarczy kliknąć *Tak*.
3. Uruchom `TuyaChroma.exe`. Program nie ma podpisu cyfrowego, więc Windows SmartScreen może go zablokować. Kliknij wtedy *Więcej informacji → Uruchom mimo to*.

## Pierwsze uruchomienie

### 1. Konto Tuya

Zakładka **Konto Tuya**:

1. W aplikacji Smart Life: **Ja → ⚙ Ustawienia → Konto i bezpieczeństwo → Kod użytkownika** (*User Code*).
2. Wpisz ten kod i kliknij **Pokaż kod QR**.
3. Zeskanuj kod QR aplikacją (ikona skanera w prawym górnym rogu ekranu głównego) i potwierdź logowanie.

Program pobierze urządzenia i przez około 10 s poszuka ich w sieci.

> **Zapora Windows:** przy pierwszym wyszukiwaniu Windows zapyta, czy zezwolić programowi TuyaChroma na dostęp do sieci. Zaznacz **sieci prywatne** i kliknij *Zezwalaj*. Bez tego program nie znajdzie żarówek (nasłuchuje ich komunikatów w sieci lokalnej).

Konto służy tylko do pobrania listy żarówek i kluczy, a sterowanie odbywa się wyłącznie lokalnie. Klucze żarówek i zapamiętane logowanie są zapisane zaszyfrowane (Windows DPAPI) w pliku `%APPDATA%\TuyaChroma\config.json`.

> Program korzysta z tego samego logowania kodem QR co integracja Tuya w Home Assistant. Dlatego aplikacja Smart Life pokaże je jako autoryzację **„Home Assistant”**. Twoja dotychczasowa integracja Tuya w HA działa dalej.

### 2. Wybór żarówek

Zakładka **Światła**:

* kliknij w kolumnę **✓** przy żarówkach, którymi ma sterować Razer;
* zaznacz światło, żeby ustawić **źródło koloru**, **strefę** i **jasność** – wybory zapisują się same (pola wpisywane: IP, wersja i korekta opóźnienia – przyciskiem **Zapisz**);
* **Mysz, podkładka, słuchawki, keypad jako źródło** (strefa *Całość*): jedno światło pokazuje środek urządzenia (np. logo myszy), kilka świateł na tym samym urządzeniu dzieli je na części od lewej do prawej – fala biegnie przez nie. (Wcześniej każde światło uśredniało całe urządzenie, co przy Wave dawało szary kolor.) Gdy gra świeci tylko częścią urządzenia, światło bierze kolor z całego.
* **Chroma Link jako źródło** (strefa *Całość*): każde takie światło pokazuje swój kanał Chroma Link, w kolejności z listy (1. światło – kanał 1, 2. – kanał 2, … do 5, potem od początku), tak jak urządzenia podłączone przez Chroma Link w Synapse. W grach i profilach każde światło ma wtedy swój kolor zamiast jednej wymieszanej średniej, a fala Wave biegnie przez światła. Gdy gra ustawia tylko pierwszy kanał, wszystkie biorą jego kolor;
* **źródło koloru**: światło ustawione na *Klawiaturę* (cała) jest w profilach efektów jednym punktem fali biegnącej przez wszystkie światła; ustawione na *Mysz*, *Chroma Link* itd. albo na część klawiatury pokazuje w tej samej chwili kolor tego urządzenia Razer (w grach zawsze kolor z gry);
* **Własna fala listwy** (ustawienia światła → *Zapamiętaj efekt z aplikacji…*): wiele listew ma wbudowane efekty, np. falę płynącą wzdłuż listwy, której program nie może narysować sam (listwy Tuya przyjmują przez sieć tylko jeden kolor naraz albo ustawiają segmenty bardzo powoli). Ustaw efekt w aplikacji Tuya / Smart Life, kliknij przycisk i *OK* – program odczyta go z listwy i będzie go włączał w profilach **Wave, Spectrum i Koło** (w innych profilach i w grach listwa dalej dostaje zsynchronizowane kolory). Listwa animuje taki efekt sama, więc porusza się w swoim tempie, **nie równo z urządzeniami Razer** – to opcja dla tych, którzy wolą falę wzdłuż listwy od synchronizacji; domyślnie jest wyłączona. Włączasz i wyłączasz to polem *Własna fala listwy w Wave/Spectrum/Koło*.
* **Tryb**: *Automatycznie* (domyślnie) i *Kolor* – zwykły tryb koloru: światło płynnie przechodzi do nowego koloru swoim przejściem, a program wysyła mu kolor o czas tego przejścia wcześniej, żeby kończyło się razem z urządzeniami Razer. *Music* (szybkie przejścia) i *Natychmiast* (bez przejść) używają danych trybu muzyki – są szybsze, ale mogą zawiesić żarówkę: GU10 „RGB Biuro 2” po 10–20 minutach przestawała przyjmować połączenia (błąd 914, aż do wyłączenia zasilania). Gdy to się zdarzy, program sam wraca dla tego światła do trybu Kolor i pisze o tym w logu.
* **Szybkość zmian**: jak często żarówka dostaje nowy kolor (domyślnie do 7 razy na sekundę; *bardzo szybko* – do 10 razy, płynniej, do wypróbowania na żarówkach, które dobrze to znoszą). Ustaw *średnio* albo *wolno* dla żarówki, która przy płynnych efektach się zawiesza albo restartuje;
* **Testuj (mignij)**: żarówka mignie na czerwono, zielono i niebiesko, a potem wróci do poprzedniego stanu;
* **Szukaj w sieci**: ponownie znajduje adresy IP (np. po zmianie routera).
* **Dobierz ustawienia automatycznie**: program pobiera z konta Tuya listę funkcji każdego urządzenia, sprawdza, co żarówki zgłaszają w sieci i jak się zachowują (opóźnienie, znikanie z sieci w trakcie efektów), i proponuje najlepsze ustawienia, np. wolniejszą *Szybkość zmian* dla żarówki, która się restartuje, albo wyłączenie żarówki tylko z białym światłem. Zmiany stosuje dopiero po potwierdzeniu. Pokazuje też informacje, np. czy listwa ma osobno sterowane diody (RGBIC) i czy żarówka obsługuje tryb Music. Półtorej minuty po każdym starcie program zapisuje ten raport w logu (wpisy „Device report”), bez zmieniania czegokolwiek.

Wskazówki:
* **Chroma Link** to kanał, który wiele gier ustawia specjalnie dla oświetlenia otoczenia.
* Dwie lampy można rozdzielić strefami, np. lewą na *Lewa strona*, a prawą na *Prawa strona*.

### 3. Urządzenia Razer

Zakładka **Urządzenia Razer** pokazuje urządzenia Razer wykryte w komputerze: przez USB, odbiornik bezprzewodowy albo Bluetooth. Przy każdym jest typ, sposób podłączenia i kategoria Chroma, do której trafiają efekty. Lista **odświeża się sama** po podłączeniu lub odłączeniu urządzenia, a do tego jest przycisk **Odśwież listę**. Uwagi przy urządzeniu mówią np., że Wolverine V2 Chroma nie jest obsługiwany przez Synapse.

### 4. Aktualizacja listy żarówek

Po dodaniu, usunięciu lub ponownym sparowaniu żarówki w aplikacji Smart Life kliknij w zakładce *Światła* **Odśwież z konta**. Program pobierze aktualną listę świateł RGB i ich klucze, bez ponownego skanowania kodu QR, a potem znajdzie je w sieci. Ustawienia istniejących żarówek zostają. Żarówki usunięte z konta znikają z listy.

Żeby to działało, program pamięta logowanie do konta Tuya. Jest ono zapisane **zaszyfrowane** (Windows DPAPI, odczyta je tylko Twoje konto Windows). *Konto Tuya → Wyloguj* usuwa zapisane logowanie. Żarówki i ich klucze zostają, a program dalej nimi steruje.

**Logowanie nie wygasa samo:** dopóki program działa, co 30 minut odnawia logowanie w tle (tak jak integracja Tuya w Home Assistant) i od razu zapisuje nowe klucze od Tuya. Sterowanie światłami w ogóle nie potrzebuje konta – działa lokalnie w sieci domowej; konto jest potrzebne tylko do pobrania listy świateł i ich kluczy (*Odśwież z konta*, *Dobierz ustawienia*). Ponowne logowanie kodem QR może być potrzebne tylko wtedy, gdy program długo nie był uruchamiany, albo gdy wylogujesz program w aplikacji Tuya – program wtedy o tym napisze.

### 5. Ustawienia

* **Uruchamiaj razem z Windows** (domyślnie włączone): program startuje po zalogowaniu do Windows od razu w zasobniku, bez otwierania okna, i przejmuje wybrane żarówki, gdy gra wyśle efekt. Wpis autostartu znajdziesz też w *Menedżer zadań → Aplikacje autostartu* jako „TuyaChroma”.
* **Startuj zminimalizowany do zasobnika**: przy autostarcie okno się nie otwiera. Wyłącz, jeśli wolisz widzieć okno po starcie.
* **Gdy Razer działa, ale żadna gra nie wysyła efektów:**
  * *Pełna synchronizacja*: profil na wszystkich urządzeniach Razer i żarówkach;
  * *Profil tylko na żarówkach* (domyślnie);
  * *Oddaj żarówki do HA*;
  * *Zostaw ostatni kolor z gry*.
* **Synchronizacja kolorów żarówek w efektach:**
  * **Dokładna** (domyślnie): urządzenia Razer i wszystkie żarówki liczą efekt z jednego zegara. Każda żarówka wylicza swój kolor dopiero w chwili wysłania, dla momentu, w którym go pokaże (teraz + jej zmierzone opóźnienie), i dostaje nowy kolor tak często, jak pozwala jej *Szybkość zmian* (domyślnie do 7 razy na sekundę). W danej chwili wszystko pokazuje ten sam moment efektu. Po zmianie profilu żarówki przełączają się od razu, razem z urządzeniami Razer.
  * **Oszczędna**: wszystkie żarówki zmieniają kolor razem, ustaloną liczbę razy na sekundę (np. 1). Każda dostaje kolor ze środka taktu, więc od klawiatury różni się najwyżej o pół taktu. Mniej ruchu w sieci, ale kolory są mniej dokładne.
  * Zostają różnice, na które program nie ma wpływu: każdy model żarówki sam płynnie przechodzi do nowego koloru i trochę inaczej oddaje barwy niż diody Razer. Jeśli jakaś żarówka wyraźnie się spóźnia, ustaw jej *Korektę opóźnienia*.

### 6. Opóźnienie synchronizacji

Każda zmiana koloru leci do żarówki przez Wi-Fi, a żarówka sama płynnie przechodzi między kolorami, więc żarówki są trochę w tyle za klawiaturą.

* **Pomiar:** kolumna **Opóźnienie** w zakładce *Światła* pokazuje na żywo, ile każda żarówka potrzebuje na potwierdzenie nowego koloru (uśrednione, pojedyncze „czkawki” Wi-Fi są pomijane).
* **Kompensacja w efektach** (*Ustawienia → Opóźnienie żarówek w efektach*). Efekty liczy program, więc wie z góry, jaki kolor będzie za chwilę, i wysyła go żarówce **z wyprzedzeniem**:
  * *Automatyczna* (domyślnie): wyprzedzenie równe zmierzonemu opóźnieniu każdej żarówki;
  * *Ręczna*: jedno wyprzedzenie dla wszystkich, np. 300 ms;
  * *Wyłączona*.
* **Korekta dla jednej żarówki** (ustawienia światła → *Korekta opóźnienia*): np. taśma LED, która wolniej przechodzi między kolorami, dostaje +200 ms. Wartość ujemna, gdy żarówka wyprzedza klawiaturę.
* **Kolory z gier** zawsze docierają z niewielkim opóźnieniem (zwykle 0,1–0,3 s). Grą steruje klawiatura bezpośrednio, a program widzi kolor dopiero, gdy już na niej jest, więc nie da się go wysłać wcześniej.

## Na co dzień

* Program działa w **zasobniku** (zielona ikona obok zegara). Zarówno minimalizacja (**_**), jak i zamknięcie okna (**X**) chowają je do zasobnika. Program działa wtedy dalej. Całkowicie zamyka go dopiero *Zakończ* w menu ikony.
* Jeśli ikona jest ukryta pod strzałką **^** obok zegara, przeciągnij ją na pasek, żeby była zawsze widoczna.
* Menu ikony: **Otwórz**, **Wstrzymaj sterowanie** (żarówki od razu wracają do HA; wstrzymanie trwa do zamknięcia programu lub restartu komputera) albo **Włącz sterowanie**, **Zakończ**.
* Fala (*Wave*) przechodzi przez żarówki w kolejności z listy w zakładce *Światła*.
* Kropka w oknie programu: 🟢 efekt z gry jest aktywny, 🔵 Razer działa (odtwarzany jest efekt albo program czeka na grę), 🟡 wymaga uwagi (np. Synapse nie działa albo nie wybrano żarówek).

## Uwagi o Home Assistant

* **Oficjalna integracja Tuya** (przez chmurę) działa bez zmian.
* **Gdy program steruje żarówką**, polecenia z HA lub aplikacji Tuya zostaną po chwili nadpisane. Dotyczy to też wyłączenia żarówki: efekt włączy ją z powrotem.
  * Przy *Pełnej synchronizacji* i *Profilu tylko na żarówkach* (domyślnie) program steruje wybranymi żarówkami **przez cały czas, gdy działa Razer**.
  * Przy *Oddaj żarówki do HA* steruje nimi tylko w trakcie gry.
* Żeby sterować światłem z HA przy włączonym komputerze, użyj **Wstrzymaj sterowanie** w menu ikony. Żarówki od razu wracają do stanu sprzed przejęcia.
* **Automatyzacje HA**, które często zmieniają te żarówki, będą się „przepychać” z programem, gdy ten nimi steruje. Możesz je np. wyłączać, gdy komputer jest włączony.
* Jeśli program zostanie przerwany nagle (zanik prądu, zabicie procesu), żarówki zostaną na ostatnim kolorze, ale HA może nimi normalnie sterować.
* **Lokalne integracje** (LocalTuya, Tuya Local) mogą w czasie działania programu pokazywać żarówkę jako niedostępną. Część żarówek Tuya przyjmuje tylko jedno połączenie lokalne naraz.

## Aktualizacje

* **Program** codziennie sprawdza w [Releases](../../releases), czy jest nowsza wersja. Możesz to wyłączyć w *Ustawienia → Aktualizacje*, a sprawdzić ręcznie przyciskiem *Sprawdź teraz*.
  * Gdy jest nowsza wersja, u góry okna pojawia się przycisk **Zaktualizuj do X** (i powiadomienie w zasobniku).
  * Po kliknięciu program pobiera nową wersję, sprawdza jej **sumę kontrolną SHA-256**, podmienia `TuyaChroma.exe` i uruchamia się ponownie. Żarówki na chwilę wracają do HA i są przejmowane z powrotem. Ustawienia, profile i żarówki zostają.
* **Baza urządzeń Razer** (jak rozpoznawać urządzenia, układy klawiatur, uwagi) to plik [`data/razer_devices.json`](data/razer_devices.json) w repozytorium. Program codziennie pobiera jej nowszą wersję, także bez aktualizacji programu. Ręcznie zrobisz to przyciskiem *Urządzenia Razer → Aktualizuj bazę urządzeń*. Nowe modele Razer wystarczy dopisać do tego pliku.

## Samonaprawa

Program sam naprawia to, co da się naprawić bez Ciebie:

| Co się stało | Co robi program |
|---|---|
| Światło straciło połączenie (wyłączone, Wi-Fi) | łączy się ponownie – najpierw szybko, potem co 15 s; przejmuje je, gdy tylko wróci |
| Światło dostało nowy adres IP | po minucie szuka go w sieci i zapisuje nowy adres |
| Światło odrzuca połączenia (błąd 914) przez 3 minuty | pobiera z konta Tuya jego aktualny klucz i sprawdza wersję protokołu; jeśli się zmieniły – stosuje je; jeśli nie – podpowiada, żeby wyłączyć i włączyć światło |
| Światło zawiesza się w trybie Music | przełącza je na tryb Kolor |
| Światło znika z sieci regularnie po tym samym czasie | odnawia połączenie zawczasu |
| Wątek światła albo synchronizacji zatrzymał się przez błąd | strażnik (co 30 s) uruchamia go ponownie |
| Błąd w pętli synchronizacji | oddaje światła, po 5 s wznawia synchronizację |
| Gra przejęła urządzenia Razer / aplikacja w tle | oddaje je grze; aplikację bez efektów po minucie pomija |
| Logowanie do konta Tuya | odnawia je co 30 min |
| Okno nie startuje (Tcl nie gotowy) | próbuje jeszcze dwa razy |

Każdy nieprzewidziany błąd trafia do logu (`log.txt`), a program działa dalej.

## Rozwiązywanie problemów

Logi znajdziesz w *Ustawienia → Otwórz folder z logami* (`%APPDATA%\TuyaChroma\log.txt`).

| Problem | Rozwiązanie |
|---|---|
| „Razer niedostępny: Shared memory … not found” | Synapse nie działa albo moduł Chroma nie jest zainstalowany. Sprawdź, czy działa `RzSDKService`. |
| Pełna synchronizacja: urządzenia Razer się nie zmieniają, status „Chroma SDK niedostępne” | W *Synapse → Chroma → Aplikacje Chroma* włącz obsługę aplikacji i sprawdź, czy TuyaChroma nie jest wyłączony. Upewnij się, że Synapse działa. |
| Status ciągle pokazuje „efekt z gry aktywny (nazwa.exe)”, choć nie grasz | Inny program steruje Razer Chroma (np. Wallpaper Engine, wizualizer audio, inna aplikacja Chroma). Program traktuje go jak grę i przenosi jego kolory na żarówki. Zamknij go albo wyłącz w nim obsługę Chroma. |
| Efekt na klawiaturze 60% (np. Huntsman Mini) rozłożony jak na pełnej klawiaturze | *Ustawienia → Układ klawiatury Razer* ustaw ręcznie na *60%*. W logu (`Razer devices: …`) widać, jak program nazwał wykryte urządzenia. |
| Nie widać ikony obok zegara | Windows 11 chowa nowe ikony pod strzałką **^** obok zegara. Żeby była zawsze widoczna: *Ustawienia Windows → Personalizacja → Pasek zadań → Inne ikony zasobnika systemowego → TuyaChroma: Wł.* Jeśli ikony nie ma nawet pod strzałką, zamknięcie okna nie chowa programu (zostaje zwykłym oknem), a w logu jest wpis „Tray icon…”. |
| Gra działa, a urządzenia Razer i światła dalej pokazują profil programu (pełna synchronizacja) | Od wersji 1.9.4 program, gdy tylko zobaczy działającą grę na liście aplikacji Chroma, oddaje jej urządzenia Razer (w logu: „Chroma apps running: gra.exe”, „Razer devices handed over to the game”, potem „Game effect active”). Jeśli aplikacja przez minutę nie pokaże żadnego efektu (np. program z tapetami, który tylko „wisi” na liście Chroma), program uznaje ją za aplikację w tle, bierze urządzenia Razer z powrotem i dalej ją pomija (w logu: „shows no Chroma effect – a background app”). Jeśli tych wpisów nie ma, gra nie zgłosiła się do Razer Chroma – sprawdź, czy ma włączone efekty Chroma w swoich ustawieniach i czy jest włączona w *Synapse → Chroma → Aplikacje Chroma*. |
| Program czeka na grę, a gra ma efekty | Gra nie używa Razer Chroma albo ma je wyłączone w swoich ustawieniach. |
| Żarówka: „nie znaleziono w sieci” | Sprawdź, czy Zapora Windows zezwala TuyaChroma na dostęp do sieci prywatnej (*Zapora Windows → Zezwalaj aplikacji…*). Kliknij *Szukaj w sieci*. Jeśli nie pomoże, wpisz IP ręcznie (znajdziesz je w routerze lub aplikacji Smart Life). Komputer i żarówki muszą być w tej samej sieci. |
| Żarówka: „wyłączona / poza siecią” | Żarówka nie odpowiada. Jeśli wyłączyłeś ją wyłącznikiem, to normalne: program próbuje się połączyć coraz rzadziej (najrzadziej co 15 s) i przejmie ją sam po włączeniu, gdy tylko połączy się z Wi-Fi (zwykle 10–30 s po włączeniu prądu), a w logu zapisuje tylko jeden wpis. Jeśli jest włączona, a status się nie zmienia, zwykle router dał jej nowy adres IP (np. po restarcie komputera lub routera). Program po minucie sam szuka żarówek w sieci i zapisuje nowe adresy (w logu: „New bulb IPs”). Możesz też od razu kliknąć *Szukaj w sieci*. Żeby to się nie powtarzało, ustaw w routerze stałe IP (rezerwację DHCP) dla żarówek. |
| Aktualizacja: „Windows Defender zablokował podmianę pliku” | Defender czasem bierze programy bez podpisu za podejrzane i na chwilę blokuje plik. Program próbuje kilka razy; jeśli to nie pomoże, spróbuj za kilka minut albo pobierz zip ręcznie. Możesz też dodać folder programu do wykluczeń Defendera. |
| Światło znika z sieci co kilka minut, w różnych odstępach, także przy wolnych zmianach | Światło samo się restartuje – to nie przeciążenie (program to rozpoznaje, wraca wtedy do normalnej szybkości i pisze w logu „the rate is not the cause”). Najczęstsza przyczyna przy listwach i lampkach na USB to słabe zasilanie: podłącz je do osobnej ładowarki 5 V / 2 A (nie do portu USB komputera, huba ani monitora) albo innym kablem. Druga przyczyna to słaby sygnał Wi-Fi w tym miejscu (siła sygnału w aplikacji Tuya: *urządzenie → … → Informacje o urządzeniu*). |
| Światło znika z sieci regularnie, zawsze po tym samym czasie (np. co ok. 3 min) | Niektóre światła (np. listwa Battletron Light Bar) restartują się, gdy jedno połączenie trwa za długo. Program to rozpoznaje po dwóch takich zniknięciach i od tej pory sam odnawia połączenie z tym światłem chwilę przed tą granicą (w logu: „drops off ~… s after connecting – renewing the connection”). Kolory płyną dalej bez restartu światła. |
| Żarówka czasem zatrzymuje się na jednym kolorze | Żarówka przestała potwierdzać kolory (słabe Wi-Fi albo nie nadąża). Program od razu łączy się z nią ponownie i przez chwilę wysyła kolory rzadziej (w logu: „reconnecting”, „sending at most every …”), a potem wraca do normalnej szybkości. Kolor jest też odświeżany co 10 s, więc żarówka, która sama się zrestartowała, szybko go odzyskuje. Pomaga lepszy zasięg Wi-Fi w tym miejscu. Jeśli żarówka na chwilę całkiem znika z sieci (w logu: „dropped off the network while syncing”), to znaczy, że się zawiesza i restartuje, bo dostaje za dużo zmian (zdarza się tanim żarówkom przy płynnych efektach, np. Spectrum). Program sam zwalnia wtedy dla niej wysyłanie do końca działania, a na stałe ustawisz to w jej ustawieniach: *Szybkość zmian → średnio* albo *wolno*. |
| Żarówka: błąd połączenia | Kliknij *Testuj*. Wyłącz lokalne integracje tej żarówki w HA. Po ponownym sparowaniu żarówki w aplikacji zaloguj się ponownie, bo zmienia się wtedy jej klucz. |
| „nieobsługiwana (brak koloru RGB)” | To żarówka tylko z białym światłem, więc nie może pokazywać kolorów. |
| Logowanie QR się nie udaje | Kod użytkownika musi pochodzić z tej samej aplikacji, którą skanujesz. Kod QR jest ważny około 3 minut. |

## Rozwój

```bash
pip install -r requirements.txt pytest
pytest
python -m tuyachroma        # uruchomienie z kodu źródłowego
```

Exe budowany jest w GitHub Actions (`.github/workflows/build.yml`) przy każdej zmianie na `main`.

**Nowa wersja:** zmień `__version__` w `tuyachroma/__init__.py` i wypchnij na `main`. Po przejściu testów GitHub Actions sam opublikuje wydanie `v<wersja>` z plikiem zip i sumą kontrolną, a zainstalowane programy zaproponują aktualizację. Wydanie można też opublikować ręcznie (*Actions → Run workflow → Opublikuj wydanie*) albo tagiem `v*`.

Licencje: odczyt danych Razer Chroma bazuje na [RazerSdkReader](https://github.com/diogotr7/RazerSdkReader) (MIT). Szczegóły w [NOTICE](NOTICE).
