# Start the "trusted" Edge that the booking bot drives (run this on the server, in the logged-in desktop session).
# - A normal, human-launched Edge: no automation flags, so IndiGo's bot protection treats it as a real browser.
# - Remote debugging is bound to 127.0.0.1 only (nothing outside this machine can control it).
# - Keep this window OPEN and never reload/close its IndiGo tab. If the PC reboots, log in and run this again.
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$profile = "D:\vps\apps\indigo-bot\cdpprofile"     # keeps the trust cookies between restarts - do not delete
Start-Process $edge -ArgumentList "--remote-debugging-port=9222", "--user-data-dir=$profile", "--no-first-run", "--no-default-browser-check", "https://www.goindigo.in/"
