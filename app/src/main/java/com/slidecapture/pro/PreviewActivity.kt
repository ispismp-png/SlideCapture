package com.slidecapture.pro

import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.GestureDetector
import android.view.KeyEvent
import android.view.MotionEvent
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import java.io.File
import kotlin.math.abs

/** 캡처된 슬라이드 미리보기: 이전 · 다음 · 삭제 · 파일열기 · 닫기 (좌우로 밀어서 이동도 가능) */
class PreviewActivity : AppCompatActivity() {
    private lateinit var store: SlideStore
    private lateinit var img: ImageView
    private lateinit var title: TextView
    private lateinit var info: TextView
    private var frames: MutableList<File> = mutableListOf()
    private var idx = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_preview)
        store = SlideStore(this)
        img = findViewById(R.id.pvImage)
        title = findViewById(R.id.pvTitle)
        info = findViewById(R.id.pvInfo)
        idx = intent.getIntExtra("index", 0)
        findViewById<android.view.View>(R.id.pvPrev).setOnClickListener { step(-1) }
        findViewById<android.view.View>(R.id.pvNext).setOnClickListener { step(1) }
        findViewById<android.view.View>(R.id.pvDelete).setOnClickListener { delete() }
        findViewById<android.view.View>(R.id.pvOpen).setOnClickListener { openFile() }
        findViewById<android.view.View>(R.id.pvClose).setOnClickListener { finish() }
        val gd = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true
            override fun onFling(e1: MotionEvent?, e2: MotionEvent, vx: Float, vy: Float): Boolean {
                val dx = e2.x - (e1?.x ?: e2.x)
                if (abs(dx) > 80 && abs(vx) > abs(vy)) {
                    step(if (dx < 0) 1 else -1)
                    return true
                }
                return false
            }
        })
        img.setOnTouchListener { v, ev ->
            if (ev.action == MotionEvent.ACTION_UP) v.performClick()
            gd.onTouchEvent(ev)
        }
        show()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        when (keyCode) {
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_PAGE_UP -> step(-1)
            KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_PAGE_DOWN, KeyEvent.KEYCODE_SPACE -> step(1)
            KeyEvent.KEYCODE_FORWARD_DEL, KeyEvent.KEYCODE_DEL -> delete()
            else -> return super.onKeyDown(keyCode, event)
        }
        return true
    }

    private fun show() {
        frames = store.list()
        if (frames.isEmpty()) {
            finish()
            return
        }
        idx = idx.coerceIn(0, frames.size - 1)
        val f = frames[idx]
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, o)
        var s = 1
        val maxW = resources.displayMetrics.widthPixels.coerceAtLeast(800)
        while (o.outWidth / (s * 2) >= maxW) s *= 2
        img.setImageBitmap(BitmapFactory.decodeFile(f.absolutePath, BitmapFactory.Options().apply { inSampleSize = s }))
        title.text = "${idx + 1} / ${frames.size}   ·   ${f.name}"
        val kb = f.length() / 1024.0
        info.text = "  ${o.outWidth} x ${o.outHeight}   |   ${"%,.0f".format(kb)} KB   |   좌우로 밀기: 이전/다음"
    }

    private fun step(d: Int) {
        if (frames.isEmpty()) return
        val n = (idx + d).coerceIn(0, frames.size - 1)
        if (n != idx) {
            idx = n
            show()
        }
    }

    /** 확인 후 현재 슬라이드 삭제, 다음 슬라이드를 이어서 표시 */
    private fun delete() {
        if (frames.isEmpty()) return
        val f = frames[idx]
        AlertDialog.Builder(this).setTitle("삭제 확인")
            .setMessage("현재 슬라이드를 삭제할까요?\n\n${f.name}")
            .setPositiveButton("삭제") { _, _ ->
                store.remove(idx)
                frames = store.list()
                if (frames.isEmpty()) {
                    Toast.makeText(this, "모든 슬라이드를 삭제했습니다.", Toast.LENGTH_SHORT).show()
                    finish()
                } else {
                    if (idx >= frames.size) idx = frames.size - 1
                    show()
                }
            }
            .setNegativeButton("취소", null).show()
    }

    private fun openFile() {
        if (frames.isEmpty()) return
        val uri = FileProvider.getUriForFile(this, "$packageName.files", frames[idx])
        try {
            startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "image/png")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
        } catch (e: Exception) {
            Toast.makeText(this, "이미지를 열 수 있는 앱이 없습니다.", Toast.LENGTH_SHORT).show()
        }
    }
}
