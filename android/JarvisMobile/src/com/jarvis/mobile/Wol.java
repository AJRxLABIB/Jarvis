package com.jarvis.mobile;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;

/** Wake-on-LAN: the magic packet, sending it, and checking that the PC really answers afterwards. */
public final class Wol {
    private Wol() {}

    /** "3C:52:82:1A:2B:3C" (or with '-', or none) -> 6 bytes; null if it isn't a usable MAC address. */
    public static byte[] parseMac(String text) {
        if (text == null) return null;
        String hex = text.replace(":", "").replace("-", "").replace(".", "").trim();
        if (hex.length() != 12) return null;
        byte[] mac = new byte[6];
        try {
            for (int i = 0; i < 6; i++) mac[i] = (byte) Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
        } catch (NumberFormatException e) {
            return null;
        }
        boolean zero = true, ff = true;
        for (byte b : mac) { zero &= b == 0; ff &= b == (byte) 0xff; }
        return zero || ff ? null : mac;
    }

    /** 6 x 0xFF, then the MAC 16 times (102 bytes). */
    public static byte[] magicPacket(byte[] mac) {
        byte[] packet = new byte[102];
        for (int i = 0; i < 6; i++) packet[i] = (byte) 0xff;
        for (int i = 0; i < 16; i++) System.arraycopy(mac, 0, packet, 6 + i * 6, 6);
        return packet;
    }

    /** Sends the packet a few times to every target ("host" or "host:port"); returns how many were sent. */
    public static int send(byte[] mac, List<String> targets, int defaultPort) throws Exception {
        byte[] packet = magicPacket(mac);
        int sent = 0;
        Exception last = null;
        DatagramSocket socket = new DatagramSocket();
        try {
            socket.setBroadcast(true);
            for (int round = 0; round < 3; round++) {
                for (String target : targets) {
                    String host = target;
                    int port = defaultPort;
                    int colon = target.lastIndexOf(':');
                    if (colon > 0 && target.indexOf(':') == colon) {
                        host = target.substring(0, colon);
                        try { port = Integer.parseInt(target.substring(colon + 1)); } catch (NumberFormatException e) { continue; }
                    }
                    try {
                        InetAddress address = InetAddress.getByName(host);
                        socket.send(new DatagramPacket(packet, packet.length, address, port));
                        sent++;
                    } catch (Exception e) {
                        last = e;
                    }
                }
                Thread.sleep(150);
            }
        } finally {
            socket.close();
        }
        if (sent == 0 && last != null) throw last;
        return sent;
    }

    /** True when something answers on host:port (Jarvis's phone remote port) within the timeout. */
    public static boolean answers(String host, int port, int timeoutMs) {
        if (host == null || host.trim().isEmpty() || port <= 0) return false;
        Socket socket = new Socket();
        try {
            socket.connect(new InetSocketAddress(host.trim(), port), timeoutMs);
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            try { socket.close(); } catch (Exception ignored) { }
        }
    }

    public static List<String> split(String text) {
        List<String> out = new ArrayList<String>();
        if (text == null) return out;
        for (String part : text.split("[,\\s]+")) if (!part.trim().isEmpty()) out.add(part.trim());
        return out;
    }
}
