package com.slidecapture.pro

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.os.PowerManager
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * PPT 변환 서비스 - 별도 프로세스(:convert)에서 실행되는 포그라운드 서비스.
 * PC v1.6과 같이 화면과 분리되어 변환 중에도 캡처·미리보기 등을 그대로 쓸 수 있고,
 * 앱을 내려도 알림창에 진행률을 표시하며 계속 변환한다. 취소하면 이 프로세스만 종료한다.
 */
class ConvertService : Service() {

    /** Python(mobile_worker)이 호출하는 콜백: JSON 배열 문자열 ["progress", n, total, name] 등 */
    inner class Reporter {
        fun onMessage(msg: String) {
            handle(msg)
        }
    }

    private var total = 0
    private var wake: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_CANCEL) {
            cancelNow()
            return START_NOT_STICKY
        }
        val job = intent?.getStringExtra(EXTRA_JOB) ?: return START_NOT_STICKY
        if (running) return START_NOT_STICKY
        running = true
        total = JSONObject(job).getJSONArray("frames").length()
        startFg(notification("PPT 변환 준비 중...", 0, 0))
        // 화면 프로세스가 읽을 수 있도록 실행 정보를 파일로 남긴다(프로세스 간 SharedPreferences 캐시 문제 방지)
        jobFile(this).writeText(JSONObject().put("pid", android.os.Process.myPid())
            .put("workdir", JSONObject(job).getString("workdir")).toString())
        wake = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SlideCapture:convert").apply { acquire(3 * 60 * 60 * 1000L) }
        Thread({
            try {
                val py = Py.ensure(this)
                py.getModule("mobile_worker").callAttr("convert", job, Reporter())
            } catch (e: Throwable) {
                handle(JSONArray().put("error").put(e.message ?: e.toString()).put(e.stackTraceToString()).toString())
            }
        }, "ppt-convert").start()
        return START_NOT_STICKY
    }

    private fun handle(msg: String) {
        val a = JSONArray(msg)
        when (a.getString(0)) {
            "progress" -> {
                val n = a.getInt(1)
                val t = a.getInt(2)
                notify(notification("PPT 변환 중 $n/$t · ${a.getString(3)}", n, t))
                broadcast(msg)
            }
            "saving" -> {
                notify(notification("PPT 파일 저장 중...", total, total))
                broadcast(msg)
            }
            "done" -> {
                val r = a.getJSONObject(1)
                val uri = try {
                    publish(File(r.getString("out")), File(r.getString("report")))
                } catch (e: Throwable) {
                    finishWith(JSONArray().put("error").put("저장 실패: ${e.message}").put(e.stackTraceToString()).toString())
                    return
                }
                r.put("uri", uri.toString())
                r.put("display", "다운로드/${AppInfo.OUT_FOLDER}/" + File(r.getString("out")).name)
                val done = JSONArray().put("done").put(r).toString()
                doneNotification(uri, r.getInt("slides"))
                finishWith(done)
            }
            "error" -> finishWith(msg)
        }
    }

    /** 결과 PPTX·보고서(JSON)를 다운로드/SlideCapture Pro 폴더에 저장하고 PPTX 의 Uri 를 돌려준다 */
    private fun publish(pptx: File, report: File): Uri {
        val pptUri = toDownloads(pptx, MIME_PPTX)
        if (report.exists()) runCatching { toDownloads(report, "application/json") }
        pptx.delete()
        report.delete()
        return pptUri
    }

    private fun toDownloads(f: File, mime: String): Uri {
        val cv = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, f.name)
            put(MediaStore.Downloads.MIME_TYPE, mime)
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/" + AppInfo.OUT_FOLDER)
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv)
            ?: throw IllegalStateException("다운로드 폴더에 파일을 만들 수 없습니다.")
        contentResolver.openOutputStream(uri)!!.use { out -> f.inputStream().use { it.copyTo(out) } }
        cv.clear()
        cv.put(MediaStore.Downloads.IS_PENDING, 0)
        contentResolver.update(uri, cv, null, null)
        return uri
    }

    private fun finishWith(msg: String) {
        broadcast(msg)
        cleanup()
        running = false
        stopForeground(STOP_FOREGROUND_DETACH)
        stopSelf()
    }

    private fun cleanup() {
        val f = jobFile(this)
        runCatching { JSONObject(f.readText()).optString("workdir") }.getOrNull()
            ?.takeIf { it.isNotEmpty() }?.let { File(it).deleteRecursively() }
        f.delete()
        runCatching { wake?.release() }
    }

    private fun cancelNow() {
        cleanup()
        broadcast(JSONArray().put("cancelled").toString())
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        // 변환 중인 Python 스레드를 즉시 멈추기 위해 변환 프로세스(:convert)만 종료 (화면 프로세스는 그대로)
        android.os.Process.killProcess(android.os.Process.myPid())
    }

    private fun broadcast(msg: String) {
        sendBroadcast(Intent(ACTION_EVENT).setPackage(packageName).putExtra(EXTRA_MSG, msg))
    }

    // ---------------------------------------------------------------- 알림
    private fun channel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CH) == null) {
            nm.createNotificationChannel(NotificationChannel(CH, "PPT 변환", NotificationManager.IMPORTANCE_LOW))
        }
        if (nm.getNotificationChannel(CH_DONE) == null) {
            nm.createNotificationChannel(NotificationChannel(CH_DONE, "PPT 변환 완료", NotificationManager.IMPORTANCE_DEFAULT))
        }
    }

    private fun mainIntent(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
    )

    private fun notification(text: String, n: Int, t: Int): Notification {
        channel()
        val cancel = PendingIntent.getService(
            this, 1, Intent(this, ConvertService::class.java).setAction(ACTION_CANCEL),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CH)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle(AppInfo.TITLE)
            .setContentText(text)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setProgress(t, n, t == 0)
            .setContentIntent(mainIntent())
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "취소", cancel)
            .build()
    }

    private fun notify(n: Notification) {
        getSystemService(NotificationManager::class.java).notify(NOTI_ID, n)
    }

    private fun startFg(n: Notification) {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTI_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTI_ID, n)
        }
    }

    private fun doneNotification(uri: Uri, slides: Int) {
        channel()
        val open = Intent(Intent.ACTION_VIEW).setDataAndType(uri, MIME_PPTX)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        val pi = PendingIntent.getActivity(this, 2, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(this, CH_DONE)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("PPTX 생성 완료 ($slides 장)")
            .setContentText("다운로드/${AppInfo.OUT_FOLDER} 에 저장했습니다. 눌러서 열기")
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        getSystemService(NotificationManager::class.java).notify(NOTI_DONE, n)
    }

    companion object {
        const val ACTION_EVENT = "com.slidecapture.pro.CONVERT_EVENT"
        const val ACTION_CANCEL = "com.slidecapture.pro.CONVERT_CANCEL"
        const val EXTRA_JOB = "job"
        const val EXTRA_MSG = "msg"
        const val MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        private const val CH = "convert"
        private const val CH_DONE = "convert_done"
        private const val NOTI_ID = 16
        private const val NOTI_DONE = 17

        @Volatile var running = false

        /** 화면 프로세스에서 변환 프로세스가 살아 있는지 확인 */
        fun jobFile(ctx: Context) = File(ctx.filesDir, "convert_job.json")

        fun isRunning(ctx: Context): Boolean {
            val f = jobFile(ctx)
            if (!f.exists()) return false
            val pid = runCatching { JSONObject(f.readText()).getInt("pid") }.getOrDefault(0)
            if (pid == 0) return false
            val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager
            return am.runningAppProcesses?.any { it.pid == pid && it.processName.endsWith(":convert") } == true
        }
    }
}
