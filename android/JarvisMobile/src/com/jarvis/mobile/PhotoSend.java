package com.jarvis.mobile;

import android.content.ContentResolver;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.net.Uri;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Photo to the PC, only when YOU take it: the system camera app opens (it shows its own preview and shutter), you take ONE photo, this shrinks it (longest side 1600 px,
 * JPEG, camera rotation applied) and sends it to the PC's inbox through the same signed upload as "Share > JARVIS Mobile". The copy the camera app made in
 * Pictures/JARVIS is deleted afterwards. Nothing records in the background and the camera is never opened without a tap.
 */
public final class PhotoSend {
    private PhotoSend() {}

    public interface Done { void finished(String message); }

    static final int MAX_SIDE = 1600, QUALITY = 85;

    /** Decodes with a sample size so a 50 MP photo never needs 200 MB of RAM, rotates by the camera's EXIF note and re-encodes. */
    static byte[] prepare(ContentResolver cr, Uri uri) throws Exception {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        InputStream in = cr.openInputStream(uri);
        if (in == null) throw new java.io.IOException("can't open the photo");
        try { BitmapFactory.decodeStream(in, null, bounds); } finally { in.close(); }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) throw new java.io.IOException("the camera did not save a photo");
        int sample = 1;
        while (Math.max(bounds.outWidth, bounds.outHeight) / (sample * 2) >= MAX_SIDE) sample *= 2;
        BitmapFactory.Options opts = new BitmapFactory.Options();
        opts.inSampleSize = sample;
        in = cr.openInputStream(uri);
        Bitmap bmp;
        try { bmp = BitmapFactory.decodeStream(in, null, opts); } finally { if (in != null) in.close(); }
        if (bmp == null) throw new java.io.IOException("the photo could not be read");
        int rotation = 0;
        InputStream exifIn = cr.openInputStream(uri);
        try {
            if (exifIn != null) {
                int o = new ExifInterface(exifIn).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
                rotation = o == ExifInterface.ORIENTATION_ROTATE_90 ? 90 : o == ExifInterface.ORIENTATION_ROTATE_180 ? 180 : o == ExifInterface.ORIENTATION_ROTATE_270 ? 270 : 0;
            }
        } catch (Exception ignored) { /* no orientation note: leave it as it is */ } finally { if (exifIn != null) exifIn.close(); }
        float scale = Math.min(1f, (float) MAX_SIDE / Math.max(bmp.getWidth(), bmp.getHeight()));
        if (rotation != 0 || scale < 1f) {
            Matrix m = new Matrix();
            if (rotation != 0) m.postRotate(rotation);
            if (scale < 1f) m.postScale(scale, scale);
            bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.getWidth(), bmp.getHeight(), m, true);
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.JPEG, QUALITY, out);
        return out.toByteArray();
    }

    /** Runs on a background thread, then ALWAYS removes the camera app's copy. ``done`` is called on that thread. */
    public static void send(final Context c, final Uri uri, final Pc pc, final Done done) {
        new Thread(new Runnable() {
            @Override public void run() {
                String message;
                try {
                    if (pc == null) {
                        message = "Pair a computer first.";
                    } else {
                        byte[] jpeg = prepare(c.getContentResolver(), uri);
                        Map<String, Object> args = new LinkedHashMap<String, Object>();
                        args.put("name", "camera-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date()) + ".jpg");
                        args.put("data", Base64.encodeToString(jpeg, Base64.NO_WRAP));
                        Api.Result result;
                        try {
                            result = Api.post(pc, false, "/api/mobile/upload", Protocol.envelope(pc.id, "phone_file", args), 120000);
                        } catch (Exception e) {
                            result = Api.post(pc, true, "/api/mobile/upload", Protocol.envelope(pc.id, "phone_file", args), 120000);
                        }
                        message = result.ok() ? "Photo sent to your PC. Ask JARVIS about it (\"what is this?\")."
                                : "Your PC said no: " + Json.str(result.json(), "message", "error " + result.status);
                    }
                } catch (Exception e) {
                    message = "Couldn't send the photo: " + e.getMessage();
                } finally {
                    try { c.getContentResolver().delete(uri, null, null); } catch (Exception ignored) { /* already gone */ }
                }
                done.finished(message);
            }
        }).start();
    }
}
