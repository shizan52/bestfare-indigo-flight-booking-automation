# Best Fare — desktop app

IndiGo bookings with a promotional fare (Offer 1 or Offer 2 - the codes live on the server), run on the user's **own Windows computer**
— the desktop twin of the Telegram bot in `../indigo-bot`. Same engine, same PNR procedure; the coupon rules are the
extension's own `core.js`, built in (see below).

## Run it yourself — `run.py`

The easy way is the launcher **outside** this folder, `f:\indigo\run.py` — it only passes everything on to the real
`run.py` in this folder (same options, same exit code). This folder's own `run.py` keeps working by itself too, so the
folder can be copied to another computer alone. From any folder: `python f:\indigo\run.py --demo`.

```powershell
cd f:\indigo                  # (or f:\indigo\bestfare-desktop — the same commands work in both)
python run.py                 # open Best Fare (real bookings on IndiGo)
python run.py --demo          # PRACTICE MODE: every screen works (fares, bookings, every failure), nothing is sent to IndiGo
python run.py --debug         # the developer's view: the technical story in Activity, a VISIBLE browser window, full logs
python run.py --check         # quick check of this computer: Python, packages, Edge, folders, IndiGo busy-pause
python run.py --check --browser   # ... and start the booking browser, load IndiGo's home page, read what IndiGo sees
python run.py --tests         # all automated tests
python run.py --setup         # (re)install the packages into the app's own .venv
```

The first start creates the app's own Python environment (`.venv`) and installs PyQt6 + aiohttp there — nothing goes into
your normal Python. Needs Microsoft Edge (every Windows 10/11 has it).

Data lives in `%LOCALAPPDATA%\BestFare`: `bestfare.db` (history + settings), `bestfare-demo.db` (practice history),
`edge-profile\` (the app's private browser profile), `edge-port.txt`, `logs\bestfare.log`.
`debug.flag` (an empty file there), `BESTFARE_DEBUG=1` or `run.py --debug` switch debug mode on.

## What the user sees

* **New booking** — four cards: ① Trip (From / To boxes that suggest as you type — city, old names like Bombay or Vizag,
  airport name or code, from IndiGo's own airport list; the travel date opens a **calendar with IndiGo's lowest fare of every
  day**, flight-less days greyed, the cheapest day starred; the form moves on by itself From → To → calendar → first
  traveller), ② Travellers, ③ Contact, ④ **Offer — one at a time** (Offer 1 or Offer 2, never both).
* **Choose your flight** — after *Book now*, once IndiGo's flights are in, the right-hand panel (where the PNR will later
  appear) lists **every flight** with its brief details — departure → arrival (+1 when it lands the next day), flying time,
  *Non-stop* / *1 stop · via city*, flight number — and **its lowest fare** (with *Offer: save ₹…* where the offer applies
  to that fare, *Regular fare* where it does not; *₹… each* for groups). Sorted *Lowest fare* first (or *Earliest*), *Non-stop only*
  filter, the lowest fare is marked and ready. The traveller taps a flight and presses **Continue**; the booking then goes
  ahead with exactly that flight (and IndiGo's record is checked to show it). The list waits 5 minutes (the fares stay
  valid only a few minutes), the clock runs on the panel, *Cancel this booking* holds nothing. Nearby airports (Hindon,
  Navi Mumbai, Dixon…) are never listed. The same list is in practice mode.
* **My bookings** — every PNR (and the flight) with *Pay before*, a **live countdown** (a PNR is held 30 minutes; red in the last 5 minutes),
  and *Expired* once it has ended.
* **Activity** — the story in plain words. There is no Help page.
* After the PNR the app checks **IndiGo's own record of the booking** (names, route, date, phone, email, offer) against what
  was typed and says so ("✓ Checked with IndiGo") — or warns before the traveller pays.

## Fast, and calm on a slow computer or a slow connection

Measured on a real booking (good connection): ~120 s, of which **41 s were the page's warm-up** before the first call and **61 s the
payment page**; the IndiGo calls themselves were ~19 s. What was changed — never by making the booking more "robotic":

* **The warm-up happens while the traveller fills the form.** IndiGo wants ~22–38 s of natural mouse/scroll activity on the page
  before its first call. Once the fare calendar has started the browser, the engine keeps the page alive in 4-second pieces
  (`engine._keep_warm_loop`) until it has had ~36 s, and keeps the browser open while somebody is typing
  (`MainWindow.eventFilter` → `engine.keep_open`, at most 8 minutes after it was opened). Book now then needs only a few seconds
  (`warmup_for()`: the same total activity, partly done already). A cold browser still gets the full 22–38 s. It never starts a browser.
* **Pauses count the time that has already passed** (`IndigoSession.human_pause`): a slow answer, or the traveller choosing a
  flight, is itself the pause — the spacing between two calls is never shorter than before, only never longer than needed.
* **The payment page waits for what it shows, not blindly** (`psp_flow`): ~10 s of fixed `sleep`s are gone; the QR "settle" wait and
  the HDFC re-check (the live-found flake fixes) are unchanged. After the page is left, the booking record is read up to 3 times
  quickly before the whole payment page is ever repeated (`_read_record`).
* A booking cancels the fare calendar's work (it never queues behind it); the pop-up guard (the bank window) polls only
  during the payment page; `engine._log_timing` writes where the time of each booking went (debug / log).
* **Slow connection** (`indigo-bot/speed.py`): every IndiGo call is timed against what it normally takes; the waits that depend on
  the network (a call's limit, the gateway's pages, the home page, the work-time limit) grow with it, at most 3× (the work limit 2×) —
  a fast connection is never made to wait. After two slow calls the traveller reads once: "The internet connection looks slow —
  this may take a little longer." The progress panel says "taking a little longer than usual … nothing is charged" after 2½ minutes.
* **Slow computer** (`browser.py`): Edge may take up to 90 s to start (told after 12 s: "Getting ready — this is taking a little
  longer on this computer"); one missed answer never makes a healthy Edge "dead" (it is asked twice — "dead" means its window is
  closed and restarted), a crashed Edge is noticed at once; IndiGo's home page is waited for up to 90 s on a slow line; the flight
  list is built once and only re-ordered when sorted (7 ms instead of ~115 ms for 64 flights).
* A hang cannot freeze the window: the engine runs in its own thread, every wait has a limit, *Stop this booking* always works,
  the whole booking is stopped after 7 minutes of working time (2× on a slow line; the time spent choosing a flight is not counted).

## When IndiGo does not answer: the app handles it itself — no timer, no lock

Older versions answered one refusal by IndiGo (a blocked call, an "Access Denied" page) with a **lock-out**: "IndiGo is busy, try again in 15 minutes" —
the whole app refused Book, the timer doubled with every repeat (15 → 30 → 60 → 120 min) and survived a restart. Its "blocks in a row" counter was never
reset by a success on disk, so agents who open the app all day inherited it and single refusals grew into hours. **That mechanism is gone**
(`Pace.lockout = False`; a leftover saved timer is deleted at the first start). Instead the engine deals with a refusal by itself, gently and bounded
(`engine._until_it_works`, `RECOVERY`):

* **Only while nothing is chosen or held** (token / search refused, the home page "denied", a page that does not answer, a session that would not start)
  the booking starts over — the traveller loses nothing. Step 1: the same page keeps moving like a person for 26–40 s, then asks again with a new session.
  Step 2: Edge is closed, left alone 25–40 s, started fresh, 32–46 s of activity. Step 3: 95–140 s rest, fresh again, 38–54 s. An explicit HTTP 429 doubles the
  rests. Worst case the booking gives up after about 5–6 minutes; **Stop** works at any moment.
* **Later** (the flight is chosen) it never starts over: `ExtensionSession.call` repeats just that one call after a calm moment, up to twice — the fare hold
  only when the request provably never reached IndiGo (429/401/403), adding the travellers and reading the booking back whatever the refusal was.
* **No internet** is not IndiGo's refusal: the engine waits for the connection (up to 2 minutes, three times) and spends nothing.
* The screen shows one calm line ("IndiGo is taking a little longer than usual — trying again for you."), the step list goes back to "Getting ready", the
  elapsed line already says "taking a little longer than usual" after 2½ minutes. No countdown, no minutes, no "wait".
* If it still does not work: "IndiGo is not responding well right now, so this booking could not be completed. Nothing was booked or charged — please press
  Book again." The credit is returned by the server, nothing is locked, and the next press of Book starts at once — only a little more patiently (longer
  warm-up for 20 minutes, `_cautious_until`).
* **A new installation** (IndiGo has never answered one of its searches: the first booking of 6 of 7 users was refused) warms up 30–46 s instead of 22–38 s and
  asks the fare calendar for the first time after 18–26 s (`_seasoned`, `WARM_CAUTIOUS`); it is "known" after the first answered search.
* **What the server is told** (the admin sees it in the user's Bookings, the traveller never does): a few words like `worked on try 2 (fetch@search)` or
  `4 tries (429@search): …` — how often IndiGo had to be asked again, and what was refused. This is how to see whether it works in the field.
* Tuning lives in constants at the top of `engine.py` (`RECOVERY`, `RATE_LIMIT_REST_FACTOR`, `NET_WAIT_S`, `CAUTION_S`, `WARM_CAUTIOUS`) and in
  `extension.py` (`LATE_RETRIES`, `LATE_TRIES`, `LATE_CALM_S`). Tests: `tests/test_self_healing.py`.

## Nothing leaks how it works

The screens, the Activity page, the error messages and the log file say only what a traveller cares about. Nothing about
tokens, searches, a browser, pages, gateways, the extension. The log file holds only the same customer story
(`app.py` filters everything technical out unless debug mode is on). `tests\test_ui.py`, `test_engine.py`, `test_logging.py`
fail if a technical word appears in what the customer can read.

The **booking browser** is never started when the app opens — only when the fare calendar or a booking needs it. It works
out of sight (its window lives off-screen at -32000,-32000; measured on Edge 156: the page still sees `visible`, clicks arrive
as trusted events, screenshots work; a guard moves any new pop-up window away within ~0.1 s) and closes by itself a few
seconds after a booking, or 90 s after the last fare request. `--debug` shows it.

## The coupon extension is built in

`ext\core.js` is the Indigo extension's own rules file (version 1.5.0), copied byte for byte from
`F:\card\indigo-dist\Indigo-1.5.0\core.js` (= `F:\card\indigo\core.js`). `extension.py` runs it **inside the booking page** and
lets it rewrite the two requests the extension rewrites (flight search, fare hold) and read IndiGo's answer — nothing is
installed in any browser, no icon, no panel. If the page cannot run it, the bot's Python port of the same rules takes over
(`../indigo-bot/coupons.py`, tested against the same file). To use a newer extension: replace `ext\core.js`, run the tests.

## Light, and fits every screen

**Weight (measured on a normal PC, Edge 156).** The whole Qt window is ~75 MB, builds in 0.11 s and uses 0 % CPU when nothing happens. A window made of HTML would need a
browser engine: an empty Edge app-window alone is 15 processes and ~670 MB (WebView2 / QtWebEngine are the same Chromium) - so an HTML interface is *heavier*, not lighter,
and the app stays Qt. The two real weights were reduced instead:

* **Shadows.** A `QGraphicsDropShadowEffect` on a card makes Qt re-draw and re-blur the WHOLE card whenever anything inside it changes (a blinking cursor, a hover, the pulsing
  step list). `widgets.shadow()` now only notes the wish; `shadows_for(host, *cards)` puts a `ShadowLayer` (shadows.py) behind them, which paints a cached nine-slice picture
  and re-draws itself only when a card moves, changes size, appears or disappears. Same look (compared pixel by pixel with the old effect: mean difference < 1/255 - a test),
  a blinking cursor in a card costs 0.6 % of a core instead of 2.9 %. **Never put a graphics effect on a widget again** (`tests/test_layout_fit.py` checks).
* **The booking Edge.** In its first ~15 seconds Edge runs six helper pages of its own (built-in extensions, ~270 MB, 14 processes in total); a booking never touches them.
  `browser.trim()` closes them over the debugging connection (only targets of type background_page / service_worker whose address is `chrome-extension://`): 8 processes,
  ~410 MB instead of ~673 MB at the start. (Afterwards Edge itself lets them go, so the steady ~390 MB is the same - the saving is the start-up peak.) The launch flag that switches
  them off (`--disable-component-extensions-with-background-pages`) is NOT used: it changes what a web page sees (speech voices 321 -> 3).

**Screens.** The window opens at a size that fits the screen it is on (1320 x 860 when there is room, smaller when there is not, centred, never bigger than the usable area),
remembers where and how big it was left (setting `window`), and can be made small - down to 820 x 500, or less on a screen that is smaller itself. Everything gives way instead of
being cut off (`BookPage._adapt`, three steps by the page's width: >= 960 / >= 780 / less; the menu becomes an icon rail under 1000 px):
the right column 370 -> 340 -> 320 px, smaller margins and card padding, the traveller's line in one line or two (all travellers alike, decided by the room they have, with
24 px of hysteresis so that a scrollbar appearing cannot flip it back and forth), the date and its quick buttons side by side or under each other, one calendar month on a screen
narrower than 1000 px, the flight-choice / progress / result panels with wrapping text and a "Continue" button that picks the wording that fits. Rules for new widgets: text that can
get long wraps (`label(..., wrap=True)`, or `FlowLabel` for a caption that should stay in one line while it fits); a row of two things is a `WrapRow`; a button with a long label is a
`FitButton`; a widget that offers two arrangements reports the SMALLER minimum size (otherwise the window around it can never get narrow enough for it to give way); after a
style-sheet property change call `repolish()` (it tells the layout). Look at it with `.venv\Scripts\python.exe tools\ui_sizes.py OUT 1920x1000 1366x700 911x480 820x500 [1093x614@1.25]` (pictures of every screen at those sizes, prints what is cut off); `tests/test_layout_fit.py` fails if anything is cut off.

## How it is built

| file | job |
|---|---|
| `run.py` | what the user starts: sets up `.venv`, then `--demo` / `--debug` / `--check` / `--tests` / the app |
| `app.py` | entry point: customer-safe logging, fonts/theme, single-instance lock, window (real or practice mode) |
| `main_window.py` | the window: sidebar (New booking · My bookings · Activity), form, progress, results, history with live countdowns, activity |
| `widgets.py`, `theme.py` | From/To suggestion boxes, date field + fare calendar, traveller rows, offer tiles, steps, toast; the look; the self-fitting rows (`WrapRow`, `FlowLabel`, `FitButton`) |
| `shadows.py` | the soft card shadows, painted once into a small picture and put behind the cards by a transparent layer (see "Light, and fits every screen") |
| `choice_panel.py`, `flights.py` | the "Choose your flight" panel (cards, sort, non-stop filter, clock) and the plain words for a flight (times, duration, stops, numbers) |
| `forms.py` | input rules (same as the bot), one offer per booking |
| `stations.py`, `stations.json` | IndiGo's own domestic airport list (103 airports, airport names, old names) + matching. Rebuild: `tools\build_stations.py` |
| `fares.py` | the fare-calendar call (IndiGo's own date-picker request) |
| `verify.py` | IndiGo's record of the booking vs. the order |
| `extension.py`, `ext\` | the coupon extension's own core.js, run in the page |
| `store.py` | SQLite history; names/phone/email wiped 2 h after a booking; remembered contact only on request |
| `engine.py` | background thread + asyncio: queue, progress in customer words, fares, IndiGo-busy pause (doubling, survives restarts), repair + one retry, 7-minute working-time limit (the time spent choosing a flight is not counted), the flight-choice wait, on-demand quiet browser |
| `browser.py` | the app's private Edge: start (quiet / visible) / find / repair / tidy / trim Edge's own helper pages / close, pop-up guard |
| `demo.py` | practice mode: a pretend engine, made-up fares, the orange bar to choose the next outcome |
| `selfcheck.py` | `run.py --check` |
| `paths.py` | data folder + where the shared engine lives (`../indigo-bot`: `cdp`, `human`, `iframe_tools`, `coupons`, `indigo_client`, `psp_flow`, `airports`) |

### Edge launch recipe (measured, do not change lightly)

* The server's proven recipe: a human-launched Edge, its own profile, remote debugging on 127.0.0.1 with a **fixed port**
  (first free from 9333, remembered in `edge-port.txt`). `--remote-debugging-port=0` makes Edge report
  `navigator.webdriver = true` (that is how automation tools start it) — IndiGo/Akamai sees that.
* Plus only what a desktop app needs: `--disable-sync --disable-features=msImplicitSignin` (a new profile otherwise signs
  itself into the Windows Microsoft account and turns sync on), `--disable-backgrounding-occluded-windows` with
  `CalculateNativeWinOcclusion` off, and for the quiet window `--window-position=-32000,-32000 --window-size=1280,860`.
* Never Playwright, never `Runtime.enable`, never close Edge's last tab. A brand-new profile books fine (live 2026-11-18).

### IndiGo's own calls, as measured live

* Station search: `GET api-prod-skyplus…/bookingwidgetsearchengine/search?prefix=…` (source of `stations.json`).
* Fare calendar: `POST api-prod-booking-skyplus6e…/v1/getfarecalendar` — up to 60 days per call; the offer code does
  **not** change these fares; days without flights are missing; a route without flights answers "Data is not Available".
* The booking record (`itinerary/v2/Itinerary`, read after the PNR exists) shows the phone and e-mail **masked** — `98****10`,
  `p****t@example.com` (first/last characters, four stars between) — and names no promo code for an on-hold booking. `verify.py`
  compares what is shown (names, gender, route, date, the visible parts of phone/e-mail); the offer is proved by IndiGo's
  fare-hold answer ("you save ₹…"), not by the record. Found live 2026-11-18 (a first version compared whole numbers and
  cried "mismatch" for correct bookings); `tests\test_verify.py` now uses that real shape.
* The search answer for DEL→BOM also lists journeys from/to nearby airports (Hindon, Navi Mumbai): the engine only counts
  the airports that were asked for (`indigo_client.flight_options` / `cheapest`; found live, fixed in the bot too).
* The same answer is the "Choose your preferred flight" list: ~77 journeys for DEL→BOM, each with `designator`
  (local departure/arrival, UTC times), `segments[].identifier` (carrier + flight number), `stops`, and `passengerFares` — one entry
  per fare type (`R` = the offer fare with `promoCode` and `originalFareAmount`, `B`/`J`/`O` plain, `BR`/`BC` business). The lowest
  active fare of a journey is its fare; `totalFareAmount` is the TOTAL for all travellers (2 travellers = 2 × one). A fare calendar
  day with price 0 is not a fare (today, late in the evening). `tools\probe_flights.py` does a search only and saves the answer.

## Tests

```powershell
python run.py --tests                      # 185+ tests: forms, stations, store, engine (fake IndiGo), browser guard, window (offscreen),
                                           # calendar, history timing, practice mode, extension (real core.js in Node), verification, logging
.venv\Scripts\python.exe tools\ui_preview.py OUT           # screenshots of every screen (fake engine)
.venv\Scripts\python.exe tools\smoke_run.py OUT [--demo]   # starts `python run.py` for real, screenshots the real window, closes it
.venv\Scripts\python.exe tools\probe_search.py             # LIVE, no booking: does IndiGo accept this computer right now?
.venv\Scripts\python.exe tools\live_app_test.py OUT --offer offer2   # LIVE: real app, calendar, quiet browser, real PNR (30-min hold)
```

The request payloads (date, route, every passenger, contact, offer code) are checked in `../indigo-bot/tests/test_payloads.py`.
Live tests create real On-Hold bookings. Never run them in bursts: IndiGo slows down a connection that makes many holds in
a short time — and the server bot shares this internet connection.

## Not done yet

* No `.exe` yet (by request). When it is built (PyInstaller, like `F:\project\flight\build_exe.py`), add the engine
  modules from `../indigo-bot` as hidden imports / `pathex`, plus `ext\core.js` and `stations.json` as data files.
* (The licence / credit system exists now — see the next section.)

## Booking tabs, clean forms, saved contacts, offers

* **Tabs** (like a browser's) along the top of *New booking*: one booking per tab, **+ New booking** opens a clean form. While a booking runs you can fill and book another
  in a new tab: it waits its turn ("Waiting for your other booking to finish…") and starts by itself (the engine runs bookings one after the other on the one private browser).
  A tab's title is its route and its state: `●` running, `⏳` waiting, `❓ Choose a flight` (this tab comes to the front: the fares wait only a few minutes), `✓ route · PNR`, `✕`.
  ✕ on a running tab asks, then stops only that booking. A finished booking stays in its tab (copy the PNR any time); coming back to *New booking* after one finds a clean form.
  Nothing is carried over from one booking to the next (no remembered contact, no last route).
* **Saved contacts**: *Saved contacts ▾* in the Contact card lists any number of saved mobile + email pairs (pick one: both boxes fill); *Save this contact for later* names the one typed.
  Kept on this computer (settings `contacts`); an earlier version's single remembered contact becomes the first saved one.
* **Offers**: the traveller sees only *Offer 1* / *Offer 2* (never what they are), and only the ones the admin allowed for the key (the server sends just the keys; one allowed offer = already chosen).

## Children and infants

Each traveller row has **Adult | Child | Infant** buttons, and the card has **+ Adult**, **+ Child**, **+ Infant**. IndiGo's rules, checked on the
travel date (the age counts on the day of the flight): a **child** is 2 to 11 years, takes a seat and needs a **date of birth**; an **infant** is from 3 days
to under 2 years, rides on an adult's lap (one infant per adult) and needs a **name and a date of birth**; from 12 years someone is an adult. At most 6
people take a seat, at most 4 of them children and 4 infants (IndiGo's own passenger picker); infants do not count against the seats. The form says what to change a traveller to when the date of birth does not fit
(`forms.check_dob`). Credits: every adult and child costs 1, an infant costs what the admin sets (Settings → Rules, default 1; 0 = free).
Engine side (shared with the bot, `indigo-bot/indigo_client.py`), all learned from IndiGo itself on 2026-11-19 (4 real PNRs): the search carries `types` ADT / CHD / INFT
(IndiGo's own widget code), the fare hold carries ADT / CHD plus `infantCount`, the child goes into a CHD slot with `info.dateOfBirth` and titles MSTR/MISS, the infant
is written INSIDE the adult who carries it (`infant: {name, gender, dateOfBirth, ...}`) and its `gender` must be TEXT ("Male"/"Female" - IndiGo refuses the number an
adult's gender is; `INFANT_STYLES` holds the other spellings and remembers the one IndiGo accepts). Discount: the offers apply to children too. The infant's fee
(₹2,250 on DEL-BOM) is NOT in the flight list's fares - it is added at the hold, and the app says so (a note under the list, and a note with the total once held).
`verify.py` compares names AND the children's/baby's dates of birth with IndiGo's record (which hides the baby's name). Tests: `tests/test_children.py`, `indigo-bot/tests/test_child_infant.py`. The Telegram bot is still adults-only.

## Licence, credits and updates (the cloud server)

A user's copy signs in with a **key** the admin gives (login window at start). The key is bound to that one computer; the **server**
keeps the credits (1 credit per traveller per booking), takes them *before* the app is given the offer code, and can block a key
at any time. The program itself is not in the copy that is handed out: only a small **shell** is (`trust, device, cloud, licence, shell,
shell_ui, packed, updater, bundle, run`); the rest is published from this workspace and arrives signed and encrypted from the server,
and runs from memory. Everything about the server, the admin panel, publishing, deployment and the threat model is in
`../bestfare-cloud` (README.md, SECURITY.md).

```powershell
python run.py --dev                     # the workspace's own way to work without a server: offer codes from ../indigo-bot/config.py (development only)
python tools\publish_release.py --server http://127.0.0.1:8780 --notes "..." --live        # publish a new version to every app
python tools\protect_key.py                                                               # passphrase on the release-signing key (recommended)
python tools\make_distribution.py OUT --server https://<address> [--pin <PIN>] [--zip]      # the folder you give to users
```

Files: `trust.py` (the only things the app believes: the server's public key, the developer's release public key, the stamped address and
certificate PIN), `cloud.py` (signed requests, verified answers, pinned https), `licence.py` + `gate.py` (the booking gate: credits first),
`updater.py` + `packed.py` + `bundle.py` (verify, keep, open in memory, fall back), `device.py` (this computer's key pair, sealed by DPAPI).
Tests: `test_licence_e2e.py`, `test_gate_engine.py`, `test_licence_ui.py`, `test_updates.py`, `test_distribution.py`, `test_tls_pin.py`,
`test_security_client.py` (they run against a REAL server started in a thread).
Never in a user's copy or in a release: offer codes, `har.txt`, anything from `../bestfare-cloud/secrets` (the build tools refuse).

## The installer, the update window and uninstall (Task 6)

Users do not run Python: they download **BestFare-Setup.exe** (one file, ~77 MB) from the server's `/install` page, run it, and Best Fare is installed for their Windows account
(`%LOCALAPPDATA%\Programs\Best Fare`, start-menu + desktop shortcut, an entry in *Installed apps*). Their data (history, saved contacts, sign-in, the booking browser's profile) lives apart in
`%LOCALAPPDATA%\BestFare` and is never touched by an update.

```
python tools\build_installer.py --server https://<server> --version 1.1.0 --notes "what is new"       build + check + sign   -> dist\BestFare-Setup.exe (+ .json)   (about 2 minutes)
python tools\publish_installer.py --server http://127.0.0.1:8781 --live                               upload through the SSH tunnel; --required = installed apps must update
python tools\e2e_installer.py --setup1 <old>.exe --setup2 <newer>.exe                                  the whole thing with REAL files and a real server (install, first start, self-update, release update, uninstall)
BestFare.exe --uninstall [--silent] [--purge]      BestFare.exe --selfcheck --out F      BestFare-Setup.exe /S --dir X --no-launch      (silent install)
```
How it fits: `entry.py` is BestFare.exe (PyInstaller, one folder); it holds only the SHELL (`trust, device, cloud, licence, shell, shell_ui, packed, updater, bundle, selfupdate, autoupdate, update_ui, setup_core`);
the program arrives from the server after sign-in as before (so every module and library the program imports is listed in the build - `_bf_imports.py` - and `--selfcheck` imports all of them).
`setup_main.py` + `setup_ui.py` + `setup_core.py` + `update_ui.py` are the setup program: wizard (Welcome / Installing / Done / Error), update window (`--update --wait-pid`), silent mode, uninstall window.
`update_ui.py` is the one design (indigo header, rounded card, steps, progress with speed and time left) used by the update window, the "Update ready" card and the wizard.

Two kinds of update, both automatic: a new **release** (the program: downloaded at the start with the update window, or quietly while the app is open and offered as a card) and a new **setup**
(the app itself: the installed app downloads it, checks hash + the developer's signature, closes, the setup swaps the folder and starts Best Fare again). A restart waits for a running booking.
Tests: `tests/test_selfupdate.py`, `test_setup_core.py`, `test_update_ui.py`.
