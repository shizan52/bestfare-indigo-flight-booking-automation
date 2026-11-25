# Best Fare - security model

Written for the owner. It says what is protected and how, what was attacked on purpose (as a user and as an outsider), what the attacks
found, and - honestly - what no software can prevent.

## 1. What is worth protecting

| asset | why |
|---|---|
| the **offer codes** (Defence / Promotional fare B) | they are the product; anybody who has one can use it without paying |
| the **credits** | they are the money |
| the **program** (the booking engine) | it is the product's know-how |
| the **admin panel** and the **keys** (identity, release signing, master) | whoever has them controls everybody |
| users' **personal data** (traveller names, phones) | stays on the user's own computer; the server never sees it |

## 2. How it is built (the layers)

1. **The server decides, the app obeys.** Credits, blocks, expiry, offers and the offer codes exist only on the server. The credits are taken
   in one database transaction *before* the app is handed the offer code (`begin_booking`); the database itself refuses a negative balance.
   An app that is modified cannot spend a credit it does not have - it simply never receives a code.
   The app is not even told what an offer *is* (who it is for): it receives only the keys of the offers its key may use ("offer1", "offer2"), and the server
   refuses any other offer (`offer_not_allowed`, nothing charged, an event for the admin).
2. **Every answer is signed** by the server's identity key (Ed25519); the app believes nothing else. A fake server, a proxy, an old answer
   replayed, an answer with a changed credit count: all fail the signature (it covers the request's nonce and path).
3. **Every request is signed** by the computer's own key pair (kept sealed by Windows DPAPI) and carries a short-lived session token. A
   stolen token alone is useless; replays are refused (nonce + clock window).
4. **One key = one computer.** The first activation binds the key to that computer's key pair; a second computer is refused, flagged, and the
   admin gets an alert. Optional MAC lock per key. The admin can move a key (reset) or kill it (regenerate).
5. **The program is not in the app you hand out.** A distribution holds only the shell. The program is signed by *you* (the server
   cannot forge one - it does not have your release key), encrypted, and opened in memory with a key the server gives only to an
   activated, active computer. A blocked or expired user gets no key, so the program cannot even start.
6. **Updates cannot be abused:** a build number only goes up (no downgrade to a vulnerable version), a damaged stored copy is fetched
   again and checked again, a version that crashes at start is dropped for the previous one, a zip with dangerous file names is refused.
7. **The admin panel** is plain JS that never writes HTML (text only), has a strict content-security-policy, SameSite cookies + CSRF header,
   passwords hashed with scrypt, lockouts (per address, per account), optional two-step sign-in (a code works once), the password is
   asked again for anything that weakens the login, and owner-only actions (admins, releases, deleting users).
8. **The network:** the apps reach the server over https (production: a Let's Encrypt certificate; for a server without a name the app pins the certificate's public key).
   nginx limits rates and sizes; the service runs as its own user with a sealed-off file system.
   **The admin panel is reachable on the internet on its OWN name** (`bestfare-admin.<domain>`, https only) at the owner's request, and still through the SSH tunnel. What protects it there: scrypt passwords,
   lockouts per address and per account (the server) plus 12 login tries a minute per address (nginx), CSRF header + a SameSite=Strict, HttpOnly, **Secure** cookie (Secure only when the request really came over
   https; the plain-http tunnel keeps working), a strict content-security-policy and HSTS, no search-engine indexing, and optional two-step sign-in (**turn it on: a public login page is only as strong as the password**).
   The panel host serves nothing but the panel; the app host serves no panel; neither serves the other's paths.
9. **The installer is signed too.** The setup program people download, and the one installed apps update themselves with, carries a signature of the developer's release key over
   (version, SHA-256). The server refuses an upload without it (it cannot make one), the app checks it again before it runs anything, only fetches a setup from one fixed address, and never one that is
   not newer than what is installed. The setup unpacks its files only inside the install folder (every name is checked) and replaces the program folder as a whole, putting the old one back on any failure.
10. **The public doors are small:** on the app name `/install` and `/download/BestFare-Setup.exe` (rate-limited per address, no other file can be named), `/api/v1/` for the apps, `/healthz`; on the panel name only `/admin/`.
11. **A stopped key stops the app.** The decision is the server's, taken at the moment of booking: the app asks ONCE when the user presses Book (and when it starts), not over and over. A "blocked", "ended" or
    "sign in again" answer covers the whole window with a lock card carrying the admin's words (menu, form and fare search stop, running bookings are stopped, the booking browser closes) and it holds until the server says
    yes - pulling the cable does not undo it. The server refuses a blocked key's booking, session, download and update key regardless; the lock is what makes the app itself match.

## 3. What was attacked - and what it found

Every item below was tried against the running server / the real app first, and is now a test that keeps failing the build if it ever comes back.
Server: `tests\test_security.py`. App: `bestfare-desktop\tests\test_security_client.py`, `test_tls_pin.py`, `test_updates.py`.
Installer: `tests\test_deploy_plan.py`. The admin panel was also attacked in a real browser (stored script / image / SVG / `javascript:` text in
names and notes: shown as text, nothing ran).

**Found and fixed**

| # | what a hacker could do | fix |
|---|---|---|
| 1 | **Get the offer code again for free**: pay for one booking, then send the same booking reference again and again - the server handed the code back every time without a charge | a reference is good for one open booking, a couple of repeats at most (for a lost answer); afterwards `409` |
| 2 | Share one key between computers by copying the device key | the server notices one key taking turns between internet addresses (flag + alert); one computer per key stays the rule the admin enforces |
| 3 | An **expired** licence could still fetch the program and the key to open it | every program/key/update request checks the licence is usable |
| 4 | Remove or swap the admin's two-step sign-in with only a stolen browser session | needs the password again; the secret is sealed in the database |
| 5 | Use a two-step code again after somebody saw it | a code works once |
| 6 | Guess the admin password from many addresses (one try each) | the account itself locks (15 min) after 12 failures, whoever sends them |
| 7 | Guess the password through a stolen session (change password / switch off 2FA) | 5 wrong tries lock that for 15 min |
| 8 | Learn which admin usernames exist by timing | the same password work is done for unknown names |
| 9 | Fill the server's memory with a body that never says how long it is, or with deeply nested JSON | bodies are read in pieces and cut at 256 KB; nesting is refused cleanly |
| 10 | Make the server crash (500) with huge ids / offsets | clean 404 / clamped |
| 11 | Half-apply a settings change that has one bad value | all or nothing |
| 12 | See the web server's name/version, or lack of headers on a 404 | one fixed `Server` header; the security headers are on every answer |
| 13 | Start unlimited bookings per hour to farm refunds | per-user hourly limit; the daily refund limit is 12 and a long run of failures with no success stops refunds (it was 6 a day and 7 failures in 10, which also stopped honest users on a bad afternoon) |
| 14 | A lost or damaged `master.key` / `identity.pem` made the server quietly invent new ones (every key and app would die) | the server refuses to start and says what is missing; secret files are created private from the first byte; `backup` command |
| 15 | The booking's printed form (a log line, a crash report) showed the **offer code** | the code is not part of `repr()` |
| 16 | Claim a build number with an environment variable to skip a forced update | the setting is gone |
| 17 | A damaged or edited stored program was marked "bad" for ever (the app could not start until the next release) | it is fetched again from the server and verified like the first time |
| 18 | A hand-edited `state.json` (text, huge numbers, `../..`) | only real build numbers are believed |
| 19 | The distribution tool would write any address into a source file, or accept `http://` | strict address check; `https://` required away from this computer |
| 20 | The developer's release key lay on disk in the clear | `tools\protect_key.py` puts a passphrase on it |
| 21 | After the admin pressed **Reset computer**, the OLD computer signed itself in again with the key it still remembered - whoever called first got the key (found by the panel-to-app chain test) | an app that signs in by itself may only use a key that is already bound to it; a moved key needs the key to be typed again (`rebind_needed`) |
| 22 | *(found while building the installer)* the release publishing tool sent the release's content key as raw bytes in an HTTP header, which the server rejected - the very first real publish would have failed | the key is sent as URL-safe base64; the end-to-end installer test publishes a real release through the real admin API |
| 23 | *(found by the end-to-end test)* "Uninstall" never removed the program folder (the helper that deletes it after the program exits had its quotes mangled) | a small batch file with retries; a test reads the script and the end-to-end test checks the folder is gone |

**Attacked and held** (no flaw): SQL injection in every text field of the panel and the app API; stored XSS; path traversal on every file
route (`..%2f`, `%252e`, backslashes, NUL, map files, `.git`, `.env`); CSRF on every changing route; every admin route without a
cookie / with a logged-out or invented cookie; session fixation; plain admin using owner routes; replay of requests; changed request bodies;
a thief's token with another key; forged and expired tokens; token life after block / reset / regenerate; a hundred bookings at once
(never more than the balance); the same booking sent ten times at once (charged once); odd traveller counts (`-1`, `0`, `10`, `"1"`, `True`,
`2**70`, lists); somebody else's booking id; flipping a finished booking to get a refund (or a second refund); failing on purpose (runs out and
is flagged); an unsigned or wrongly signed release; a release signed by someone else; a draft release's file and key; bad key-exchange values;
a fake server; a changed answer; an answer replayed for another request; a vault replaced by someone else's key pair; secrets in logs, in the
vault file, in the database and in the shell that is handed out; a self-made certificate without the pin.
Installer (`tests\test_installer.py`, `bestfare-desktop\tests\test_selfupdate.py`, `test_setup_core.py`): an installer with no signature, another developer's signature, a signature for another version or another
file, one that is not a Windows program, too small, or older than the live one - all refused with nothing stored; a file changed by one byte on the server or on the way (the app refuses it and deletes it);
a server offer with a lower version, a bad hash, a huge or boolean size, or any download address but the fixed one; a plain admin trying to upload / publish / delete; `/download/../identity.pem`,
`%2e%2e`, other file names, and a flood of downloads; a setup payload with `../`, absolute or drive paths (zip-slip); install folders such as a drive root, the Windows folder or Program Files; an update while the app
is open; an update that fails half way (the old program is put back); the update window and "Restart now" never cutting a running booking short.

## 4. What cannot be prevented (be honest with yourself)

1. **A skilled user who has paid for a booking can see that booking's offer code.** It has to reach IndiGo from the user's own computer; the
   app's own Edge window can be inspected with its developer tools. What limits this: the code is given only per paid booking, kept only in
   memory, hidden in the app's screens and logs, and the admin can **change the code** in Settings (when IndiGo gives a new one) and block
   keys. Nothing on a user's computer can make a code that must be typed into IndiGo secret from that user.
2. **The traveller count and the result are reported by the app.** A user who edits the small plain shell could say "1 traveller" for 4, or
   "failed" for a booking that worked. Limits: automatic refunds are capped (12 a day, none for a long run of failures with no success), every case is
   flagged and visible in the panel (bookings per user, refunds denied), and the admin can refund or block. The server cannot ask IndiGo.
3. **A running program can be read from memory** by someone who holds a valid key and is willing to dump it. The encryption keeps the
   program from everyone *without* a licence and from copies of the files; it does not hide it from a licensed expert.
4. **"One computer" is as strong as the computer's key pair.** The key pair is sealed to the Windows account, so copying files to another
   PC does not work, but the owner of the PC can export his own key pair. The MAC lock is reported by the app. The server catches the
   common case (two addresses taking turns) and tells the admin; it does not stop it by itself.
5. **If the release-signing key and an owner login are both stolen**, a hostile program could be pushed to every user. The key is not on the
   server, it can have a passphrase, and publishing needs an owner login (with two-step sign-in on). Keep one copy offline.
6. **If the server itself is taken over** the attacker can read the database (the keys are sealed with `master.key`, which sits beside it),
   mint credits and read the programs. They cannot push code (no release key) and cannot read users' traveller data (it is not there).
7. **The server is one small machine.** If it is down, nobody can start a booking (by design: no credit, no code). Take a backup now and then.
8. **The setup is not "code-signed" in Microsoft's sense.** That needs a paid certificate from a certificate authority. Until you have one, Windows SmartScreen shows "Unknown publisher" the first
   time a person runs the downloaded file (the download page explains: More info → Run anyway). Updates made by the installed app itself do not show it. (Your own developer-key signature, which this
   system checks, protects the *updates*; it cannot make Windows trust the *first* download.)
9. **Production uses the name `192-3-164-209.sslip.io`**, a public DNS service that maps such names to that IP. If that service were ever down, apps could not reach the server until it is back - a domain you
   own removes this dependency (see the README: one DNS record, one deploy command, one required setup; installed apps move over by themselves). The Let's Encrypt certificate renews itself (cron, twice a day).

## 5. Habits that keep it safe

* Turn on two-step sign-in for every admin; give admins the *admin* role, not *owner*.
* Never paste `secrets\*`, `har.txt`, `server.md`, `telegram.md`, `vps-access.txt` anywhere. Never put them in a release (the build tool refuses).
* Block a key the moment it looks shared (Dashboard → Flagged). Regenerate a key that was posted publicly.
* When a user leaves: block, then delete. When an admin leaves: disable the admin (their sessions end at once).
* After IndiGo changes a step: **Maintenance mode** on → fix → publish with `--live --required` → maintenance off.
* Back up (`deploy_cloud.py --backup`) after the first users exist and before every server update; keep a copy of `identity.pem` offline.

## 6. Run the attacks again

```
cd f:\indigo\bestfare-cloud   && ..\bestfare-desktop\.venv\Scripts\python.exe -m pytest tests
cd f:\indigo\bestfare-desktop && .venv\Scripts\python.exe -m pytest tests
```
