# Device mesh, discovery, notification intelligence, skill cards (round 61)

## Device mesh (`core/device_mesh.py`, tool `device_mesh`)
One list: this PC, other computers (Wake-on-LAN list), paired phones (trust, last check-in, battery, granted permissions, abilities) and unfinished handoffs
waiting on a device. `discover` also scans the local network. Nothing is invented: unreported values stay "unknown".

## Local discovery (`remote/discovery.py`)
Multicast DNS / DNS-SD written in-repo (no package, no cloud). **Off by default**: `LOCAL_DISCOVERY=true` makes this PC announce `_jarvis._tcp.local.` (name,
port, short public-key fingerprint; no secrets). A scan lists JARVIS devices that answer. Finding is never trusting: a device is "paired" only when the
announced id matches a paired phone's key fingerprint (and then only its address is attached), otherwise "NOT paired". Pairing is unchanged (one-time code
+ key exchange). Packets from the network are untrusted: size and record limits, compression-loop cut, strings cleaned, the real sender address is used
instead of any address inside the packet. Strict offline mode allows link-local multicast (224.0.0.0/24, ff02::/16) and nothing wider.
**Unverified here:** the multicast sockets (needs your network; Wi-Fi with client isolation or a firewall blocks it, then a scan honestly finds nothing).
**Not built:** the phone announcing itself / finding the PC (Android `NsdManager`; no JDK here to build or test it).

## Notification intelligence (`core/notification_intel.py`)
Rules label each forwarded phone notification: critical, suspicious (phishing cues, with the reasons), action required, important, routine, promotional;
English, Bangla and Banglish cues. Promotions and repeats are not added to the centre; critical / suspicious go to the security category with high priority;
a deadline phrase adds a "say remind me ..." hint (never creates one). `notifications` action `digest` summarises the unread ones by importance.
Limits: it reads wording, not identity; it never replies, deletes or opens links.

## Skill card (`automation/skill_card.py`, tool `skill_card`)
Read-only page on one saved routine: inputs, tools / apps / abilities needed, risk and failure handling per step, which steps can be undone, version, run
history, and what is NOT recorded (expected result, end-to-end verification). The visual editor is the existing Workflow Studio.

## Blackboard
Parallel plans now use the shared blackboard too (a call that already failed twice is refused instead of retried blindly).

## Modes
New: deep work, study, travel, sleep. A mode only makes Jarvis quieter or more private; it never grants anything.
