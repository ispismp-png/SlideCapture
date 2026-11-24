package com.slidecapture.pro

import android.content.Context
import android.graphics.BitmapFactory
import org.json.JSONArray
import java.io.File
import java.security.MessageDigest

/** 캡처된 슬라이드 목록(앱 저장 폴더 SlideDown 의 PNG). 순서는 목록 파일에 보관 */
class SlideStore(ctx: Context) {
    val dir: File = File(ctx.filesDir, "SlideDown").apply { mkdirs() }
    private val prefs = ctx.getSharedPreferences("slides", Context.MODE_PRIVATE)

    fun list(): MutableList<File> {
        val arr = JSONArray(prefs.getString("list", "[]"))
        val out = mutableListOf<File>()
        for (i in 0 until arr.length()) {
            val f = File(arr.getString(i))
            if (f.exists()) out.add(f)
        }
        return out
    }

    fun save(list: List<File>) {
        val arr = JSONArray()
        list.forEach { arr.put(it.absolutePath) }
        prefs.edit().putString("list", arr.toString()).apply()
    }

    fun add(f: File) {
        val l = list()
        l.add(f)
        save(l)
    }

    /** index 번째 슬라이드 삭제(앱 폴더의 캡처 파일도 삭제) */
    fun remove(index: Int): Boolean {
        val l = list()
        if (index !in l.indices) return false
        val f = l.removeAt(index)
        if (f.parentFile?.absolutePath == dir.absolutePath) f.delete()
        save(l)
        return true
    }

    fun newFile(prefix: String = "slide"): File {
        var n = 1
        val used = dir.listFiles()?.mapNotNull {
            Regex("""^[a-z]+_(\d+)\.png$""").find(it.name)?.groupValues?.get(1)?.toIntOrNull()
        } ?: emptyList()
        if (used.isNotEmpty()) n = used.max() + 1
        return File(dir, String.format("%s_%03d.png", prefix, n))
    }

    companion object {
        fun md5(f: File): String {
            val md = MessageDigest.getInstance("MD5")
            f.inputStream().use { inp ->
                val buf = ByteArray(1 shl 16)
                while (true) {
                    val r = inp.read(buf)
                    if (r <= 0) break
                    md.update(buf, 0, r)
                }
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }

        fun isImage(f: File): Boolean {
            val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(f.absolutePath, o)
            return o.outWidth > 0 && o.outHeight > 0
        }
    }
}
