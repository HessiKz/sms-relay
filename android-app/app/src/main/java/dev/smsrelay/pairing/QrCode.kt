package dev.smsrelay.pairing

import android.graphics.Bitmap
import com.google.zxing.BarcodeFormat
import com.google.zxing.MultiFormatWriter

/**
 * Draws the QR code of a pairing.
 *
 * The code is built on the phone, without a network round trip and
 * without a camera permission: the payload comes from the server and is
 * shown as it is.
 */
object QrCode {

    /**
     * Renders [payload] as a QR code of [sizePx] pixels.
     *
     * Returns null when the payload cannot be drawn, e.g. because it is
     * too long for a QR code. The caller then asks for a new code.
     */
    fun render(payload: String, sizePx: Int): Bitmap? {
        if (payload.isBlank() || sizePx <= 0) return null
        return runCatching {
            val matrix = MultiFormatWriter().encode(
                payload,
                BarcodeFormat.QR_CODE,
                sizePx,
                sizePx
            )
            val width = matrix.width
            val height = matrix.height
            val pixels = IntArray(width * height)
            for (y in 0 until height) {
                val offset = y * width
                for (x in 0 until width) {
                    pixels[offset + x] = if (matrix.get(x, y)) BLACK else WHITE
                }
            }
            Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).apply {
                setPixels(pixels, 0, width, 0, 0, width, height)
            }
        }.getOrNull()
    }

    private const val BLACK = 0xFF000000.toInt()
    private const val WHITE = 0xFFFFFFFF.toInt()
}
