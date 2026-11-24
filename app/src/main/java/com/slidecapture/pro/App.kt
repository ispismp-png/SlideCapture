package com.slidecapture.pro

import android.app.Application
import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/** 프로그램 이름·버전: 버전은 app/build.gradle.kts 의 appVersion 한 곳에서만 관리 */
object AppInfo {
    const val NAME = "SlideCapture Pro"
    val VERSION: String get() = BuildConfig.VERSION_NAME          // 예: v1.6
    val TITLE: String get() = "$NAME $VERSION"                     // 헤더·알림·보고서에 같은 문자열 사용
    val DEFAULT_PPT_NAME: String get() = "${NAME.replace(' ', '_')}_$VERSION.pptx"
    const val OUT_FOLDER = "SlideCapture Pro"                      // 다운로드/SlideCapture Pro
}

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        // Python(Chaquopy) 시작: 화면 프로세스와 변환 프로세스(:convert) 각각에서 실행된다
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
    }
}

/** Python 쪽 준비(Tesseract 데이터·검증용 글꼴 복사, 경로 지정). 백그라운드 스레드에서 호출할 것 */
object Py {
    @Volatile private var ready = false

    @Synchronized
    fun ensure(ctx: Context): Python {
        val py = Python.getInstance()
        if (ready) return py
        copyAssets(ctx, "tessdata", File(ctx.filesDir, "tessdata"))
        copyAssets(ctx, "fonts", File(ctx.filesDir, "fonts"))
        py.getModule("mobile_worker").callAttr(
            "setup",
            ctx.filesDir.absolutePath,                                   // tessdata 의 부모 폴더
            File(ctx.filesDir, "fonts/NanumGothic-Regular.ttf").absolutePath,
            File(ctx.filesDir, "fonts/NanumGothic-Bold.ttf").absolutePath
        )
        ready = true
        return py
    }

    private fun copyAssets(ctx: Context, assetDir: String, outDir: File) {
        outDir.mkdirs()
        val names = ctx.assets.list(assetDir) ?: return
        for (n in names) {
            val dst = File(outDir, n)
            // 압축되지 않은 자산(traineddata·ttf)은 크기 비교, 그 밖의 파일은 있으면 건너뜀
            val size = runCatching { ctx.assets.openFd("$assetDir/$n").use { it.length } }.getOrDefault(-1L)
            if (dst.exists() && (size < 0 || dst.length() == size)) continue
            val tmp = File(outDir, "$n.tmp${android.os.Process.myPid()}")
            ctx.assets.open("$assetDir/$n").use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
            if (!tmp.renameTo(dst)) tmp.delete()
        }
    }
}
