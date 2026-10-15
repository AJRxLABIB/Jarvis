# Jarvis on your phone

The phone remote is a private web page served by Jarvis on your PC. Your phone talks to the PC directly - there
is no cloud server in between.

## 1. Switch it on (once)

1. Jarvis -> Menu -> Settings -> Devices & remote: tick **Phone remote on my home network** and
   **Phone remote over https**. Save.
2. Windows may ask whether Jarvis may use the network: allow **Private networks**.

## 2. Pair your phone (once per phone)

1. Phone and PC on the **same Wi-Fi**.
2. Jarvis -> Menu -> **Phone remote**. A window shows a **QR code**, the address and a **6-digit pairing code**.
3. Scan the QR code with the phone camera (or type the address, e.g. `https://192.168.1.5:8765/`).
4. The browser warns about the certificate once (it is Jarvis's own self-signed certificate):
   **Advanced -> Proceed**. The fingerprint shown in the Jarvis window lets you check it's your PC.
5. Type the pairing code. It works once, for 5 minutes. The phone now has its own key.

## 3. One tap away

- Android (Chrome): menu (three dots) -> **Add to Home screen**.
- iPhone (Safari): Share -> **Add to Home Screen**.

It opens full screen like an app, with the Jarvis icon.

## 4. Away from home (optional)

1. Install **Tailscale** (free) on the PC and on the phone, and sign in with the same account on both.
2. Jarvis -> Settings: tick **Phone remote also over my Tailscale network**.
3. Menu -> Phone remote now shows a second QR code ("Away") with the PC's Tailscale address (100.x.y.z). Pair or
   just open it from the phone. Only devices signed in to your Tailscale account can reach it.

Do **not** forward the port on your router instead: that would put the page on the open internet.

## 5. Check it works

- On the phone page the status line shows *online*, the PC's battery and Jarvis's version.
- Type "what time is it" or hold the mic button and speak. The answer appears on the phone and on the PC.
- A question that needs approval (e.g. "empty the recycle bin") shows **Allow / Deny** on the phone.

## Honest limits

- The page works **while it's open**. Phones stop web pages in the background, so it can't stay running all the
  time or ring when closed. Keep the home-screen icon; opening it reconnects in a second.
- Jarvis must be running on the PC (Settings -> Start with Windows).
- Turning the PC **on** from the phone needs Wake-on-LAN plus another device at home that is on (another Jarvis PC,
  the router's WoL page, or a WoL app on the phone while on the home Wi-Fi). A web page can't send that packet.
- Emergency controls -> "phone remote off" closes it at once; only the PC window can turn it back on.

## 6. Wake your PC from the phone (Jarvis Wake app, Android)

Jarvis Wake is a small app of our own (no other app needed). It sends the Wake-on-LAN "magic packet" and then
checks that the PC really answers - it never says "on" unless Jarvis replied.

**Build it once** (on the PC, downloads the official Java + Android tools the first time, ~400 MB):

    cd D:\Jarvis; .venv\Scripts\activate
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install -Android

**Install and set up:**

1. Jarvis -> Menu -> Phone remote: scan the **Wake app (Android)** QR code with the phone (same Wi-Fi).
2. The phone downloads JarvisWake.apk. Open it; Android asks to allow installing apps from the browser once
   (Settings -> Install unknown apps -> Chrome -> Allow). Play Protect may warn because the app isn't from the
   Play Store: choose "Install anyway". It is signed with your own key (android\signing on the PC).
3. Open the phone remote page (the first QR code), then tap **Set up the wake app**. The app opens with this PC's
   name, wired MAC address, home broadcast address, IP and port filled in.
4. Test: put the PC to sleep, open Jarvis Wake, press **WAKE UP MY PC**. It shows "Your PC is ON - Jarvis
   answered" or, after 2.5 minutes, that it didn't answer.

**What the PC must have** (Jarvis checks it: say "can you wake this PC"):

- An **Ethernet cable**. Laptops almost never wake over Wi-Fi.
- "Wake on Magic Packet" on in the network adapter (Device Manager -> the Ethernet adapter -> Advanced / Power
  Management -> allow this device to wake the computer).
- Waking from **sleep/hibernate** works on most PCs. From a full **shutdown** it also needs "Wake on LAN" in the
  BIOS/UEFI and Windows' Fast Startup off - many laptops can't.

## 7. Waking from another network (mobile data, another Wi-Fi)

The packet has to get into your home network through your router. Pick one:

- **Your router's own remote-wake feature** (many ASUS, TP-Link, Fritz!Box, Huawei routers have "Wake-on-LAN" in
  their app or web page). Easiest when available.
- **Port forwarding**: in the router, give the PC a fixed address (DHCP reservation) and forward **UDP port 9** to
  it (some routers let you forward to the broadcast address x.x.x.255, which works better). Put your router's
  public IP or a DDNS name in Jarvis -> Settings -> "Wake from outside", then tap "Set up the wake app" again.
  - If your router's WAN address differs from the address sites like "what is my IP" show, your internet provider
    uses CGNAT: nothing from outside can reach your router, so this can't work (ask the provider for a public IP,
    or use the router's cloud wake feature).
  - When the PC sleeps long, some routers forget where it is and drop the packet. A static ARP entry in the router
    fixes that, if it has the option.
- **Checking it's on from outside** needs Tailscale (section 4): with it, the app also checks the PC's Tailscale
  address. Without it the app can send the signal but can't confirm it from outside - it will say so.
