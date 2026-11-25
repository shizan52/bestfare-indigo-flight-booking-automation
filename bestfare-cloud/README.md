# Best Fare cloud

The one server that decides **who may book**: licence keys, credits, the one-computer rule, the offer codes, the admin panel, and the
delivery of new versions of the app. The desktop app (`..\bestfare-desktop`) is a small shell; the program itself arrives from here,
signed by the developer and encrypted, only after a key has been accepted.

```
bfcloud/          the server (aiohttp + SQLite)         tests/     the test suite incl. the red-team suite (test_security.py)
  service.py      all rules: licences, credits, bookings, refunds, releases, admins
  web.py          the HTTP side: /api/v1 (the apps), /admin/api (the panel), /admin/ (the panel's own files)
  crypto.py       Ed25519 signatures, X25519 key wrapping, AES-GCM, scrypt, TOTP, tokens
  bundle.py       the release format (byte-identical copy in the app: tests check it)
  web/            the admin panel (plain JS, no framework, no inline scripts) + the public install page (install.html)
deploy/           service file, nginx files (IP+pin mode and domain mode), certificate tool, installer (deploy_cloud.py)
secrets/          PRIVATE: the server's identity key, the TLS key, the owner login. Never copy, paste or commit.
SECURITY.md       the threat model: what was attacked, what was fixed, what cannot be prevented
```

## Run it here (development)

```
cd f:\indigo\bestfare-cloud
..\bestfare-desktop\.venv\Scripts\python.exe -m bfcloud --data data create-owner boss     # prints a random password once
..\bestfare-desktop\.venv\Scripts\python.exe -m bfcloud --data data serve                 # http://127.0.0.1:8780/admin/
..\bestfare-desktop\.venv\Scripts\python.exe tools_demo_server.py 8791                    # a throw-away server with made-up users, to look around
..\bestfare-desktop\.venv\Scripts\python.exe -m pytest tests                              # all tests
```

## What the admin does (the panel is self-explanatory; this is the short list)

| Want to… | Where |
|---|---|
| give someone a key | **Users → New user** (name, credits, optional expiry, optional MAC lock, **which offers they may use**) → the key is shown once, and can be shown again later (it is logged) |
| choose / change which offer a user may use | New user: tick Offer 1 and/or Offer 2. Later: the user's page → **Edit**. The panel shows what each offer is; **the user sees only "Offer 1" / "Offer 2"**, and only the ones they may use. Filter the list by offer with the chips above it. |
| add / take away credits | the user's page → quick buttons or any number; every change is in the credit history |
| stop someone | **Block**: the server refuses a blocked key's booking, session, download and update key at once. The app asks the server ONE time, at the moment the user presses Book (no repeated polling): a blocked user's window is then covered by a lock card (no search, no form) and nothing starts; going offline does not undo it. **Unblock** and "Check again" opens it |
| let a key move to a new computer | **Reset computer** (the old one stops at once) |
| a key leaked | **Regenerate key** (the old one dies, the computer must activate again) |
| a booking failed and the user lost credits | the user's **Bookings** tab → **Refund** |
| IndiGo changed something | **Settings → Maintenance mode** (nobody can start a booking, nothing is charged), then publish a fix |
| send a message to everybody | **Settings → Message to all users** |
| stop one of the offers | **Settings → Offers → Available** off |
| see misuse | **Dashboard → Flagged**, **Activity** (alerts in red) |

Rules: **1 credit = 1 traveller in 1 booking.** The credits are taken *before* the app is given the offer code; a failed booking gets
them back automatically within limits (12 a day per user, and not for a long run of failures with no success at all - both limits are editable in Settings; a refund the limits held back stays visible to the admin, who gives it back with one click). Every booking in the app shows its credits ("2 used", "returned ✓"), the user is told when the balance changes, and a user's page shows given / used / returned / really spent.

### The buttons on a user's page (each asks first, names the user, and says what happened)

| Button | What the server does | What the user's app does |
|---|---|---|
| **Block** ⇄ **Unblock** (the row flips; the list shows *who* blocked, *when*, *why*; Undo for 9 s) | status `blocked`, sessions end; booking, session and the program's key are refused | the window is covered at its next check (every 10 min and ONCE before every booking), Book does nothing, a restart shows only a "stopped" window; after Unblock, "Check again" opens it |
| **Reset computer** (disabled, with the reason, while the key is unused) | the key is free for ONE new computer; the old one is stopped | "Please sign in again"; typing the key opens it; a second computer is refused |
| **Show key** | shows the full key; written to the log (`key_viewed`) | nothing |
| **New key** | the old key dies at once; credits and history stay | "Please sign in again - type the new key"; a restart shows the key window and does not start without a key the server accepts; the old key is refused |
| **Edit** | writes only what really changed; a save that changes nothing writes nothing; only a new MAC lock ends the user's sessions | name/note: nothing happens to them; a MAC lock that is not their computer: stopped |
| **Delete** (owner only) | the user and their history are removed; the log keeps *who deleted whom* | "Please sign in again"; the key is refused |

(The confirmation window once answered "no" to every question, so these buttons did nothing and nobody saw an error. `tests/test_panel_actions_js.py` now runs the panel's own script and clicks each one.)

### Testing the admin <-> app connection

`bestfare-desktop\tests\test_admin_actions_app.py` walks through all of the above with the real app window, over real HTTP, against a throw-away copy of this server. The same walk against the
**production** server - one temporary user (made and deleted by the script), **nothing is booked** (the engine is a stand-in that can book nothing):

    set BESTFARE_ADMIN_USER / BESTFARE_ADMIN_PASS (an owner), then
    python bestfare-desktop\tools\e2e_admin_app.py --admin https://bestfare-admin.shizanvps.me --server https://bestfare.shizanvps.me --released

`--released` = with the program the server hands out (downloaded, signature-checked and run from memory, like a user's computer does it), not the workspace's files; it also proves the release equals the
tested workspace. Mind the server's own protection: **5 wrong keys / wrong MAC addresses / second-computer tries from one internet address in 15 minutes lock that address out for 15 minutes**, and more than
20 sign-ins in 10 minutes are slowed down. One run uses 4 wrong keys, so run it ONCE, then wait 15 minutes (or restart the service). `--full` adds the steps that count as wrong keys - throw-away servers only.

## Updating the app for everyone (Task 3)

```
cd f:\indigo\bestfare-desktop
python tools\publish_release.py --server http://127.0.0.1:8780 --notes "IndiGo added a step" --live            # as soon as the apps check
python tools\publish_release.py ... --live --required                                                          # old versions must update before their next booking
python tools\publish_release.py ... --rollout 10                                                               # 10 % of the computers first (then raise it in the panel)
python tools\publish_release.py ... --dry-run                                                                  # build + check only, upload nothing
```

The release is built from the workspace, checked for secrets, **signed with your private release key (never on the server)**, encrypted,
and uploaded as a draft. In **Updates** you can make it live, withdraw it, mark it required, change the roll-out, or go back (publish the old
code again as a higher build number: a number never goes down). Apps fetch it at the next start or in the background and run it from memory.
An app whose new version crashes at start returns to the previous one by itself.

To protect the release key with a passphrase (recommended): `python tools\protect_key.py`.

## Giving the app to users

```
python tools\make_distribution.py OUT_DIR --server https://<address> [--pin <certificate PIN>] [--zip]
```
A distribution holds only the small shell (no booking code, no offer code). The user types the key; the program is
downloaded, verified and opened in memory.

## The installer and the download link (Task 6)

People get Best Fare from ONE link: `https://<app name>/install` (production: https://bestfare.shizanvps.me/install) (a page with a Download button; the file is `/download/BestFare-Setup.exe`). The setup program is a normal
Windows installer window (Welcome → Installing → Done), per-user (no administrator), with an entry in Windows' *Installed apps* and a start-menu/desktop shortcut.
**Installed apps update themselves**: at sign-in (and every few minutes while open) the server tells the app about a newer *release* (the program) or a newer *setup*
(the app itself: .exe, Qt, Python); the app downloads it quietly, checks the developer's signature, shows a small "Update ready - Restart now" card (never in the middle of a
booking) and comes back as the new version. A *required* update asks in a window that cannot be skipped.

```
cd f:\indigo\bestfare-desktop
python tools\build_installer.py --server https://<server> --version 1.1.0 --notes "what is new"     # -> dist\BestFare-Setup.exe + .json (signed with YOUR release key)
python tools\publish_installer.py --server http://127.0.0.1:8781 --live                              # upload (through the SSH tunnel), check, make it the one people get
```
`--required` makes installed apps update before they go on. The panel's **Updates → Installer** card shows the link (copy button), the versions, publish / withdraw / required.
Only the holder of the release key can make a setup (the server cannot): the app runs a downloaded setup only if its SHA-256 and the developer's signature over (version, hash) are right,
and never one that is not newer than what is installed.

## Production (what is running now)

* The VPS of `vps-access.txt` (RackNerd, 203.0.113.10). It also runs another project's nginx sites (`emt-license`, ports 80/443): they are never edited - every deploy compares a checksum of every other
  nginx file before and after and stops if one differs.
* **The app link: https://bestfare.shizanvps.me/install** (the page people install from; the file is `/download/BestFare-Setup.exe`; the apps talk to `/api/v1/`).
  **The admin panel: https://bestfare-admin.shizanvps.me/** (opens `/admin/`). Both are DNS-only records (grey cloud) pointing at the VPS, one Let's Encrypt certificate for both names (certbot in its own folder
  `/opt/bestfare-certbot`, renewed twice a day by `/etc/cron.d/bestfare-certbot`), in the nginx file `/etc/nginx/conf.d/bestfare-site.conf` on the ordinary port 443 (the server picks the site by the name asked for).
* The first door still works: `https://192-3-164-209.sslip.io:8443` (`/etc/nginx/conf.d/bestfare.conf`), so a copy installed before the move keeps reaching the server and is updated to the new address by the
  required installer.
* Service `bestfare-cloud` (127.0.0.1:8780, own user, sealed). The owner login is in `secrets\OWNER_LOGIN.txt`. The panel is public: turn on two-step sign-in in **My account**.
  The SSH tunnel still works too: `ssh -i <key> -L 8781:127.0.0.1:8780 root@203.0.113.10`, then `http://127.0.0.1:8781/admin/`.
* DNS records are made with `python deploy\cloudflare_dns.py --zone <zone> --ip <ip> --name <name> ...` (it only ADDS; credentials come from the environment, are never printed or saved).
* Adding the public names to a running server: `python deploy\deploy_cloud.py --host <ip> --ssh-key <key> --site bestfare.<domain> --admin-host bestfare-admin.<domain> --yes`
  (it stops before any change if a name does not point at the server yet or is used by another site).

## Putting the server on the VPS

Domain mode (what production uses; a certificate every browser trusts):
```
python deploy\deploy_cloud.py --host <ip> --ssh-key <key> --hostname <name that points at the ip> --open-firewall --dry-run     # read the plan first
python deploy\deploy_cloud.py --host <ip> --ssh-key <key> --hostname <name> --open-firewall
```
It stops before any change when the name does not point at the server or another site already uses it; installs certbot privately (not through apt: the Ubuntu archive may be unreachable from the VPS);
rehearses the certificate first; and ends by proving every other nginx file is byte-for-byte unchanged and that the panel is not reachable from the internet.

IP mode (no name; the app pins the certificate; browsers warn about it, so there is no public download page worth sharing):
1. `python deploy\make_cert.py --host <vps ip>` - makes the certificate for an IP-only server and prints its **PIN**.
2. `python deploy\deploy_cloud.py --host <vps ip> --ssh-key <key> --dry-run` - read the plan; then run without `--dry-run`. (`--open-firewall` only ADDS port 8443.)
3. Build the users' copy with `--server https://<vps ip>:8443 --pin <PIN>`.

Both: `python deploy\deploy_cloud.py ... --backup secrets\backups` after the first users exist and now and then (keep one copy offline); `--update` later replaces the server code only (secrets, database and nginx are not touched).
The owner login is in `secrets\OWNER_LOGIN.txt` (made by the installer, never printed). Change the password and turn on two-step sign-in in **My account**.

## Keys that matter (details in SECURITY.md)

| file | what it is | if lost | if stolen |
|---|---|---|---|
| `secrets\server_identity.pem` (on the server: `data\identity.pem`) | the server's identity; every app trusts its public half | every installed app must be replaced | someone can pose as the server - rotate (new distribution) |
| `%USERPROFILE%\.bestfare-keys\release_signing.pem` | signs the app's program | no more updates: new key, new distribution | they can push code to users **if** they also reach the panel as an owner - rotate |
| `data\master.key` (server) | seals the licence keys and release keys in the database | all keys unreadable | with the database: keys readable |
| `secrets\OWNER_LOGIN.txt` | the panel's first owner | reset on the server | change the password at once |
