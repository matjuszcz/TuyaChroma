# TuyaChroma

Windows program (Python 3.12, tkinter/ttk, PyInstaller) that mirrors Razer Chroma
(Synapse 4 profiles, game effects, full sync) onto the owner's Tuya RGB lights over
the LAN. Home Assistant normally controls the lights; the program takes them over
while it runs.

## Owner's priorities (do not trade these away)
- **The main goal is synchronization with the Razer devices.** The lights follow the
  keyboard/mouse/Chroma Link in time and color.
- **Razer profiles are key, so smooth transitions matter.** Don't swap smooth fades for
  instant jumps to gain speed (this was tried and reverted).
- Games (Cyberpunk 2077, BF6) must get the Razer devices while they run; after the
  game, the program takes them back.
- Talk to the owner in **Polish**. All UI text is Polish. Code, comments, commits and
  logs are English.
- The owner often asks to "check N times". That means full review passes plus test
  runs, fixing what's found, and releasing.

## The owner's lights
- **RGB Biuro 1-3**: GU10 W505Z2 bulbs. Music mode (DP 28) hung RGB Biuro 2 with
  error 914, so the default method "auto" is colour mode. `_music_hangs` falls back
  to colour. The owner sometimes switches them off by hand; when switched back on,
  they need a moment to join Wi-Fi.
- **Light Bar ("Lampka biurko") / Led Strip**: Battletron (Action/LSC, connected
  through the Tuya app, not LSC). A color change at most every 0.3 s; this floor is
  enforced in `Device.bulb_config` via `advisor.product_hint`. Light Bar drops off
  the network: probably hardware, and the owner is changing its power supply.

## Commands
- Tests (Linux needs xvfb for Tk): `xvfb-run -a python -m pytest -q`. Baseline:
  321 passed, 11 skipped. Run it twice after changes (timing tests).
- Lint: `python -m pyflakes tuyachroma tests`
- GUI screenshot: build a Tk app with test data under xvfb (see tests/test_app.py
  for how to construct `App`).

## Release workflow
1. Bump `__version__` in `tuyachroma/__init__.py` (only place).
2. Commit with the session's trailer lines.
3. Push to the working `claude/...` branch **and** to `main`.
4. CI (`.github/workflows/build.yml`, windows-latest) tests, builds and auto-releases
   tag `v<version>` with `TuyaChroma-windows.zip`.
5. Verify: `python -c "from tuyachroma import updater; print(updater.check_latest())"`.

Other rules:
- No pull requests unless asked.
- Never send the owner's personal data to external services.

## Architecture map (tuyachroma/)
- `engine.py`, `SyncService`: the main loop.
  - Reads Razer frames; `dispatch` sends colors to workers.
  - Game detection: `other_chroma_apps`, `AliveCache`, GAME_PROBATION 60 s for
    background apps, GAME_HOLD 2 s.
  - Reclaim after a game: if Synapse's current_pid isn't ours for 3 s, it calls
    `player.reconnect()` every 5 s, up to 5 tries (logged "Synapse shows: …").
  - `heal()` watchdog every 30 s.
  - Status line: `_set_status(text, level)`; `status_level()` feeds the window's dot.
  - `bulb_colors()`: what each light shows *now*, for the live strip.
  - Precise sync: a worker's color is computed at send time + latency + fade + half
    the hold interval.
  - `link_slot`/`link_color`: Chroma Link splits one LED per light; mouse, headset
    and similar devices split left to right.
  - `ambient_color`: screen regions.
- `bulbs.py`, `BulbWorker` (one thread per light, tinytuya BulbDevice).
  - Errors come back as returned dicts (`check_reply`). OFFLINE {901, 905},
    LINK {902, 914}.
  - Lights answer only when a value changes, so unchanged values are sent nowait.
    Music/DP 28 is never acked, so it is sent nowait.
  - REPLY_TIMEOUT 0.35; COLOUR_FADE_MS 250; heartbeat 8 s (nowait in colour mode,
    waited only in music mode, where it is the only check for a hung light).
  - Colour mode: colors are sent without waiting; one per CONFIRM_EVERY (1 s) waits
    for the answer (waiting for every one cut a 0.1 s rate to ~0.25-0.4 s: visible
    steps on the GU10s, owner's video). All answers are still read: `_drain()` runs
    before every color and after heartbeats (unread answers piling up likely hung
    RGB Biuro 2 with 914 in 1.9.18). `_drain()` turns off tinytuya's retry so an
    empty ack doesn't wait the whole reply timeout.
  - tinytuya returns the FIRST waiting message as a waited call's reply (hence
    `_drain()`), and `_set_values()` resets tinytuya's sticky single-DP fallback
    (`max_simultaneous_dps`).
  - Takeover sets `_in_sync_mode` right after remembering the original state, so a
    lost answer still gets the light given back; `_resend_full` stays set until a
    full (switch + mode) send is confirmed.
  - tests/fake_tuya.py: a fake v3.3 light on localhost for real-protocol tests.
  - Learned per-light state: slow rate, drop age (renew the connection before the
    bulb resets), `rate_not_the_cause`, `_music_hangs`.
  - `update_config` applies changes live, without reconnecting.
- `player.py`: Chroma SDK output. `yielded` means the devices were handed to a game.
- `razer.py`: reads Chroma frames. Control messages (Init/Resume/Active continue;
  UnInit/Default/Suspend/Invalid end) are looked past.
- `render.py`, `profiles.py`: effect renderer (static, spectrum, breathing, wave,
  wheel, ambient, …). `EffectClock` is the shared clock.
- `cloud.py`: Tuya account login (QR).
  - `keep_session_alive` runs every 30 min.
  - Rotated refresh tokens are saved at once via `_token_listener`/`on_token`, so
    the login doesn't expire.
- `advisor.py`: "Dobierz ustawienia" (auto-tune) from device reports.
- `config.py`: Device and Settings dataclasses. `config_rev` migrations. Default
  method "auto"; `effect_sync` "precise".
- `gui.py`: tkinter app.
  - Tabs include "Światła".
  - Editor autosave writes only changed fields (snapshot) and doesn't reselect rows
    on refresh.
  - The live color strip paints every 250 ms, only while the window is shown.
  - Tray via pystray. Error hooks log everything.
  - `new_tk_root` retries Tcl init.
- `theme.py`: clam-based dark (graphite) and light palettes; `use()` picks one
  (setting "theme": auto follows Windows). Only the lights and status colors are
  colorful. The header strip draws glowing rounded pills (`rounded_rect`, `mix`).
  Windows dark title bar via DWM. The layout must fit a 600 px high window
  (test_buttons_visible_in_every_tab).
- `main.py`: entry point, error logging, relaunch after update, self-test.

## Known gotchas
- CI flakes on Windows: Tcl init (hence `new_tk_root` retry), ~15 ms timer
  resolution (keep timing asserts loose), real PIDs (patch `process_alive` in tests).
- A maximized window on Windows reports `root.state() == "zoomed"`. Use
  `_window_shown()`, not `== "normal"`.
- Don't do screen grabs or network I/O on the Tk thread.
- Tuya lights silently ignore too-fast commands. Respect `min_interval` and product
  floors.

## Open items
- Light Bar drop-offs after the power supply change: waiting for the owner's logs.
- GU10 smoothness: the owner may try the speed "bardzo szybko" (0.1 s) and report.
