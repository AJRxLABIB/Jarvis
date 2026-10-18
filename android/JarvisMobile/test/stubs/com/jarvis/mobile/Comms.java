package com.jarvis.mobile;

import android.content.Context;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/** Test stub: the shape PhoneActions uses (the real Comms needs Android's contacts / SMS / telecom). */
public final class Comms {
    public static final Set<String> ACTIONS = new HashSet<String>(Arrays.asList(
            "call", "end_call", "send_sms", "read_messages", "find_contact"));
    static Object[] run(Context c, String action, Map<String, Object> args) { return new Object[]{true, "stub"}; }
}
