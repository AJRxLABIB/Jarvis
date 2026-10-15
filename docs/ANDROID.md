# JARVIS Mobile (Android)

Your phone as a secure remote terminal for the JARVIS PC: wake it, see its real state, talk to JARVIS, and -
once you've signed in to Windows the supported way - control it. **It never handles your Windows password.**

Project: `android/JarvisMobile` (package `com.jarvis.mobile`). The older wake-only app `android/JarvisWake`
stays for reference. No Gradle, no libraries: pure Java + the Android SDK.

Build on the PC:

```
powershell -ExecutionPolicy Bypass -File packaging\build_android.ps1
```

(Downloads the official Microsoft OpenJDK 17 and Google Android SDK command-line tools once; signs with the key
in `android\signing` - keep it, updates must use the same key.) Output: `app\remote\assets\JarvisMobile.apk`,
then `packaging\build.ps1 -Install` bundles it. Install it on the phone from **Menu -> Mobile devices -> Install
JARVIS Mobile** (shows the download link, a QR, the version and the APK's SHA-256).

## The one non-negotiable

JARVIS **never**: stores, sends, logs, types or replays your Windows password or PIN; hooks the credential
provider; or bypasses the Windows lock screen. Windows stays the only thing that authenticates the Windows user.
The app shows JARVIS auth and Windows auth as two separate lines and never says "unlocked" unless Windows really
is.

## The experience

```
Laptop OFF -> open JARVIS Mobile -> Wake -> (WoL magic packet) -> waits until the PC answers -> Online
-> Windows Locked -> the app shows the supported sign-in options -> you sign in the supported way -> Ready -> control
```

## Two keys, two authentications

The phone holds two P-256 keys in the Android Keystore (hardware-backed where the phone has a secure element);
neither private key ever leaves the phone, and neither is or could be a Windows password.

* **Base key** (`jarvis_device`): identifies the phone. Signs read-only requests (status, events) and pairing.
* **Control key** (`jarvis_auth`): `setUserAuthenticationRequired(true)` - the Keystore releases it only after
  fingerprint / face / device PIN. **Every command that changes the PC is signed with this key**, so "authenticate
  on the phone, then act" is enforced by the hardware, not just a screen. Enrolled at pairing (or later from
  Settings); set once - replacing it means pairing again.

The PC enforces the same split: `STEP_UP_COMMANDS` in `app/remote/mobile.py` must carry a control-key signature,
and a phone with no control key registered simply cannot run them (`no_auth_key`).

## Pairing

1. PC: **Menu -> Mobile devices -> Pair a new phone** shows a QR (5 minutes, one phone) with a one-time secret and
   the PC's TLS certificate fingerprint.
2. The QR opens `.../mobile/pair`, which hands the app `jarvismobile://pair?host&port&pid&s&fp&pc`.
3. The app makes its keys, sends the public key with an HMAC proof of the secret and a signature, **pins the PC's
   certificate** (`fp`), then enrols the control key.
4. The phone is **pending** until you press **Trust** on the PC and check the fingerprint matches on both screens.

## Security protocol (unchanged from 3.15, extended)

Every command is a signed envelope over `request_id\ndevice_id\ntimestamp\nnonce\ncommand\narguments`. The PC
checks: well-formed -> known device -> not revoked -> (control key for step-up commands) -> timestamp within 2
minutes -> signature -> nonce never used -> trusted -> permission -> fixed argument rules, then runs it through the
same executor as a spoken command (permissions, confirmation, audit). Refusals: 400 malformed, 401 unknown/bad
signature, 403 revoked/pending/not permitted/no_auth_key, 408 expired, 409 replay.

Transport is HTTPS pinned to the PC's certificate (`Api.java`): a stranger on the network can't impersonate the
PC, and if the certificate ever changes the app refuses to connect until you pair again. LAN first; away from home
works over Tailscale when it's enabled on both ends. The app is **not** exposed to the public internet.

## Commands

| Command | Permission | Key | Becomes |
|---|---|---|---|
| hello, status, events, capabilities, auth_options, missions | read | base | status / info |
| chat | chat | base | a message to JARVIS |
| approve | approve | base | the answer to that approval ID |
| register_auth_key | (pairing) | control | enrol the control key |
| emergency_stop | any trusted phone | base | emergency stop (ends remote sessions) |
| lock, sleep, hibernate, restart, shutdown | power | control | power_control |
| wake_device | power | control | wake another PC |
| volume, media, open_app, close_app, window, brightness, open_website, browser | control | control | the matching tool |
| list_files, read_file, get_file, put_file | files | base(read)/control(write) | files inside your safe folders only |
| screen, mouse, key | screen | base(screen)/control(input) | a frame / input, active unlocked session only |

Files stay inside your Desktop / Downloads / Documents and your configured safe roots (same `PathPolicy` as the
desktop). Screen frames and mouse/keyboard work only when Windows is unlocked and the session is active (checked
live) - never on the lock screen - and while capturing, the PC shows SCREEN CAPTURE ACTIVE.

## Windows state & sign-in capability (`app/remote/pc_state.py`, `app/remote/win_auth.py`)

The PC reads - never guesses - its own state and reports it:

* **Lock state**: `WTSQuerySessionInformation` (WTSSessionInfoEx), with `OpenInputDesktop` as a second opinion.
* **Machine state**: OFFLINE / WAKING / BOOTING / JARVIS_STARTING / ONLINE_LOCKED / AUTHENTICATION_AVAILABLE /
  READY / SLEEPING / HIBERNATING / SHUTTING_DOWN / UNKNOWN (the phone adds the pre-boot ones from what it can see).
* **Sign-in capability**: edition (Home can't host Remote Desktop), whether Remote Desktop is on, Windows Hello
  sensors and PIN, TPM, Microsoft vs local account, Entra join. Anything unreadable is reported "unknown".

When Windows is locked the app shows the **supported** options (Remote Desktop where available, otherwise sign in
at the PC) and states plainly when remote sign-in isn't possible on this configuration.

## Sessions, revocation, lost phone

Remote-control sessions are recorded (`SessionStore`): you see the active ones under **Mobile devices** and can end
any of them; they also time out after 15 idle minutes, and end on revoke or emergency stop. Revoking a phone
refuses all its future requests and ends its sessions immediately; a revoked key can't pair again (make a new one).

## Multiple computers

The app holds a list of PCs; pick one to control. Each reports its own capabilities, and the app only shows
controls that PC actually has (no Wake button when Wake-on-LAN isn't set up, no screen when capture is off, etc.).

## Known limitations (honest)

* **No lock-screen bypass exists** and none is attempted. Remote Desktop signs you into your session remotely (the
  PC's own screen stays locked); otherwise you sign in at the PC. Microsoft also documents remote sessions that
  can't be unlocked with some passwordless methods - the app detects and states this rather than pretending.
* **Remote Desktop** needs a supported Windows edition (Pro/Enterprise/Education) with it switched on.
* **Wake-on-LAN over the internet** needs your own router forwarding or a VPN (Tailscale); many networks don't
  allow it. The app never claims the PC is on until it actually answers.
* **Push notifications**: the app polls for events/approvals while open and raises them; true background push
  (FCM) is not included because it needs Google Play services and a Gradle build. Approvals still reach you while
  the app is open.
* **Key hardware backing** is reported by the app and is not remotely attested; treat it as a hint.
* Screen streaming is still-frame + tap-to-click, not live video (kept light on battery and bandwidth).
