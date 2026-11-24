package com.slidecapture.pro

import android.Manifest
import android.annotation.SuppressLint
import android.app.ActivityManager
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Matrix
import android.hardware.camera2.CameraCharacteristics
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.util.LruCache
import android.util.Size
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.GridLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.OptIn
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraInfo
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton
import com.google.android.material.switchmaterial.SwitchMaterial
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.abs

class MainActivity : AppCompatActivity() {

    private lateinit var store: SlideStore
    private lateinit var previewView: PreviewView
    private lateinit var sourceLabel: TextView
    private lateinit var flashView: View
    private lateinit var listTitle: TextView
    private lateinit var thumbs: RecyclerView
    private lateinit var btnExport: MaterialButton
    private lateinit var status: TextView
    private lateinit var switchAuto: SwitchMaterial
    private val prefs by lazy { getSharedPreferences("opts", Context.MODE_PRIVATE) }

    private val bg: ExecutorService = Executors.newSingleThreadExecutor()          // Python 처리(캡처 정리·불러오기)
    private val analysisExec: ExecutorService = Executors.newSingleThreadExecutor() // 카메라 프레임 분석

    private var frames: MutableList<File> = mutableListOf()
    private var sel = -1
    private val thumbCache = LruCache<String, Bitmap>(80)

    // 카메라
    private var cameraProvider: ProcessCameraProvider? = null
    private var imageCapture: ImageCapture? = null
    @Volatile private var lastFrame: Bitmap? = null
    @Volatile private var capturing = false
    private var boardMissing = false

    // 자동 캡처(변화 감지)
    private var prevSig: FloatArray? = null
    private var lastCapSig: FloatArray? = null
    private var stableSince = 0L
    private var lastSigAt = 0L

    /** 변환 옵션 체크박스: 키, 이름, 기본값, 한 줄 도움말 */
    private val optDefs = listOf(
        Triple("auto_aspect", "화면 비율 자동조절", true) to "좌우 검은 여백을 잘라 실제 슬라이드 비율로 맞춥니다",
        Triple("correct", "화면 자동 보정", true) to "카메라로 찍은 화면의 테두리를 찾아 반듯하게 펴서 자릅니다",
        Triple("ocr", "OCR 텍스트 객체", true) to "글자를 인식해 편집 가능한 텍스트 상자로 만듭니다",
        Triple("tables", "실제 PPT 표 복원", true) to "표 격자를 찾아 PowerPoint 표로 만듭니다 (완전 복원 모드)",
        Triple("vectors", "사각형·선 벡터", true) to "도형·선·아이콘을 편집 가능한 도형/커넥터/프리폼으로 만듭니다",
        Triple("bg", "원본 배경 유지", true) to "원본 캡처를 배경으로 두고 그 위에 편집 객체를 겹칩니다 (권장)",
        Triple("inpaint", "텍스트 배경 복원", true) to "텍스트로 바꾼 글자를 배경에서 지워 이중 글자를 막습니다",
        Triple("native_only", "네이티브 객체 우선", false) to "배경 이미지 없이 도형·텍스트만으로 다시 그립니다",
        Triple("exact", "원본 동일(투명 텍스트)", false) to "화면은 캡처 그대로, 글자는 검색·복사용 투명 텍스트로 넣습니다"
    )
    private val optBoxes = mutableMapOf<String, CheckBox>()

    private val camPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) startCamera() else setStatus("● 카메라 권한이 없어 캡처할 수 없습니다 (기존파일열기는 사용 가능)")
    }
    private val notiPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private val pickImages = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (!uris.isNullOrEmpty()) importUris(uris)
    }

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            val msg = i.getStringExtra(ConvertService.EXTRA_MSG) ?: return
            onConvertEvent(JSONArray(msg))
        }
    }

    // ------------------------------------------------------------------ 생명주기
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        store = SlideStore(this)

        findViewById<TextView>(R.id.headerTitle).text = AppInfo.TITLE   // 헤더와 작업 화면 제목을 같은 문자열로
        title = AppInfo.TITLE
        @Suppress("DEPRECATION")
        setTaskDescription(ActivityManager.TaskDescription(AppInfo.TITLE))

        previewView = findViewById(R.id.previewView)
        sourceLabel = findViewById(R.id.sourceLabel)
        flashView = findViewById(R.id.flash)
        listTitle = findViewById(R.id.listTitle)
        thumbs = findViewById(R.id.thumbs)
        btnExport = findViewById(R.id.btnExport)
        status = findViewById(R.id.status)
        switchAuto = findViewById(R.id.switchAuto)

        thumbs.layoutManager = LinearLayoutManager(this, LinearLayoutManager.HORIZONTAL, false)
        thumbs.adapter = ThumbAdapter()

        findViewById<View>(R.id.btnSettings).setOnClickListener { openSettings() }
        findViewById<View>(R.id.btnCapture).setOnClickListener { capture(false) }
        findViewById<View>(R.id.btnPreview).apply {
            setOnClickListener { openPreview(sel) }
            tooltipText = "선택한 슬라이드를 크게 봅니다 (썸네일을 한 번 더 눌러도 됨)"
        }
        findViewById<View>(R.id.btnOutDir).apply {
            setOnClickListener { openOutDir() }
            tooltipText = "변환된 PPT가 저장되는 다운로드/${AppInfo.OUT_FOLDER} 폴더를 엽니다"
        }
        findViewById<View>(R.id.btnImport).apply {
            setOnClickListener { pickImages.launch(arrayOf("image/png", "image/jpeg")) }
            tooltipText = "저장해 둔 캡처 PNG/JPG를 골라 목록 뒤에 이어 붙입니다"
        }
        btnExport.setOnClickListener { export() }
        switchAuto.isChecked = false
        switchAuto.tooltipText = "화면이 바뀌고 잠시 멈추면 자동으로 캡처합니다 (삼각대·캡처보드 권장)"
        switchAuto.setOnCheckedChangeListener { _, on ->
            lastCapSig = prevSig
            setStatus(if (on) "● 자동 캡처 켜짐 - 화면이 바뀌면 캡처합니다" else "● 자동 캡처 꺼짐")
        }

        buildOptions()
        // 엔진 준비(Tesseract 데이터 복사 등)는 백그라운드에서 미리 해 둔다
        bg.execute { runCatching { Py.ensure(this) } }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            camPerm.launch(Manifest.permission.CAMERA)
        }
        ContextCompat.registerReceiver(this, receiver, IntentFilter(ConvertService.ACTION_EVENT),
            ContextCompat.RECEIVER_NOT_EXPORTED)
    }

    override fun onResume() {
        super.onResume()
        reloadFrames()
        if (ConvertService.isRunning(this)) {
            btnExport.text = "PPT 변환 중... · 취소"
        } else {
            btnExport.text = "PPTX 생성 + 분석 보고서"
        }
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(receiver) }
        bg.shutdown()
        analysisExec.shutdown()
        super.onDestroy()
    }

    private fun setStatus(s: String) {
        status.text = s
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()

    // ------------------------------------------------------------------ 옵션 체크박스
    private fun buildOptions() {
        val grid = findViewById<GridLayout>(R.id.optGrid)
        for ((def, tip) in optDefs) {
            val (key, label, dflt) = def
            val cb = CheckBox(this)
            cb.text = label
            cb.textSize = 12f
            cb.setTextColor(ContextCompat.getColor(this, R.color.text))
            cb.isChecked = prefs.getBoolean(key, dflt)
            cb.tooltipText = tip                                   // 길게 누르면 한 줄 도움말(화면 밖으로 잘리지 않게 시스템이 배치)
            cb.setOnCheckedChangeListener { _, v -> prefs.edit().putBoolean(key, v).apply() }
            val lp = GridLayout.LayoutParams(GridLayout.spec(GridLayout.UNDEFINED), GridLayout.spec(GridLayout.UNDEFINED, 1f))
            lp.width = 0
            cb.layoutParams = lp
            grid.addView(cb)
            optBoxes[key] = cb
        }
    }

    private fun opt(key: String) = optBoxes[key]?.isChecked ?: prefs.getBoolean(key, true)

    // ------------------------------------------------------------------ 목록
    private fun reloadFrames() {
        frames = store.list()
        if (sel >= frames.size) sel = frames.size - 1
        listTitle.text = "캡처된 슬라이드 (${frames.size}장)"
        thumbs.adapter?.notifyDataSetChanged()
        if (sel >= 0) thumbs.scrollToPosition(sel)
    }

    private fun thumb(f: File): Bitmap? {
        val key = f.absolutePath + ":" + f.lastModified()
        thumbCache.get(key)?.let { return it }
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, o)
        var s = 1
        while (o.outWidth / (s * 2) >= 200) s *= 2
        val b = BitmapFactory.decodeFile(f.absolutePath, BitmapFactory.Options().apply { inSampleSize = s }) ?: return null
        thumbCache.put(key, b)
        return b
    }

    inner class ThumbVH(v: View) : RecyclerView.ViewHolder(v) {
        val img: ImageView = v.findViewById(R.id.img)
        val num: TextView = v.findViewById(R.id.num)
    }

    inner class ThumbAdapter : RecyclerView.Adapter<ThumbVH>() {
        override fun onCreateViewHolder(p: ViewGroup, t: Int) =
            ThumbVH(LayoutInflater.from(p.context).inflate(R.layout.item_thumb, p, false))

        override fun getItemCount() = frames.size

        override fun onBindViewHolder(h: ThumbVH, pos: Int) {
            val f = frames[pos]
            h.img.setImageBitmap(thumb(f))
            h.img.setBackgroundResource(if (pos == sel) R.drawable.thumb_sel else R.drawable.thumb_bg)
            h.num.text = "${pos + 1}"
            h.itemView.tooltipText = "${f.name}\n누르면 선택 · 한 번 더 누르면 미리보기"
            h.itemView.setOnClickListener {
                val p = h.bindingAdapterPosition
                if (p == RecyclerView.NO_POSITION) return@setOnClickListener
                if (p == sel) openPreview(p) else {
                    val old = sel
                    sel = p
                    if (old >= 0) notifyItemChanged(old)
                    notifyItemChanged(p)
                }
            }
        }
    }

    private fun openPreview(i: Int) {
        if (frames.isEmpty()) {
            toast("캡처된 슬라이드가 없습니다.")
            return
        }
        val idx = if (i in frames.indices) i else frames.size - 1
        startActivity(Intent(this, PreviewActivity::class.java).putExtra("index", idx))
    }

    private fun openOutDir() {
        try {
            startActivity(Intent(DownloadManager.ACTION_VIEW_DOWNLOADS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            toast("다운로드/${AppInfo.OUT_FOLDER} 폴더에 저장됩니다")
        } catch (e: Exception) {
            toast("파일 앱을 열 수 없습니다: 다운로드/${AppInfo.OUT_FOLDER}")
        }
    }

    // ------------------------------------------------------------------ 카메라
    private fun source() = prefs.getString("source", "camera") ?: "camera"

    private fun startCamera() {
        val f = ProcessCameraProvider.getInstance(this)
        f.addListener({
            cameraProvider = f.get()
            bindCamera()
        }, ContextCompat.getMainExecutor(this))
    }

    @OptIn(markerClass = [ExperimentalCamera2Interop::class])
    private fun externalCamera(p: ProcessCameraProvider): CameraInfo? =
        p.availableCameraInfos.firstOrNull {
            runCatching {
                Camera2CameraInfo.from(it).getCameraCharacteristic(CameraCharacteristics.LENS_FACING) ==
                    CameraCharacteristics.LENS_FACING_EXTERNAL
            }.getOrDefault(false)
        }

    private fun bindCamera() {
        val p = cameraProvider ?: return
        p.unbindAll()
        imageCapture = null
        lastFrame = null
        prevSig = null
        val board = source() == "board"
        val selector: CameraSelector
        if (board) {
            val info = externalCamera(p)
            if (info == null) {
                boardMissing = true
                sourceLabel.text = "USB 캡처보드를 찾을 수 없습니다 (연결 확인 · ⚙ 환경설정)"
                setStatus("● USB HDMI 캡처보드가 외장 카메라로 인식되지 않았습니다. 연결을 확인하거나 카메라 입력을 선택하세요.")
                return
            }
            boardMissing = false
            selector = CameraSelector.Builder().addCameraFilter { list -> list.filter { it == info } }.build()
            sourceLabel.text = "입력: USB HDMI 캡처보드"
        } else {
            selector = CameraSelector.DEFAULT_BACK_CAMERA
            sourceLabel.text = "입력: 후면 카메라 (화면 전체가 보이게 찍으세요)"
        }
        val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        val rs = ResolutionSelector.Builder()
            .setResolutionStrategy(ResolutionStrategy(Size(1920, 1080),
                ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
            .build()
        val analysis = ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .setResolutionSelector(rs)
            .build()
        analysis.setAnalyzer(analysisExec) { img ->
            try {
                onFrame(img, board)
            } catch (_: Throwable) {
            } finally {
                img.close()
            }
        }
        try {
            if (!board) {
                val cap = ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                    .build()
                p.bindToLifecycle(this, selector, preview, cap, analysis)
                imageCapture = cap
            } else {
                p.bindToLifecycle(this, selector, preview, analysis)
            }
        } catch (e: Exception) {
            try {
                p.unbindAll()
                p.bindToLifecycle(this, selector, preview, analysis)   // 일부 기기·캡처보드: 사진 촬영 미지원
                imageCapture = null
            } catch (e2: Exception) {
                setStatus("● 카메라를 시작할 수 없습니다: ${e2.message}")
            }
        }
        imageCapture?.targetRotation = previewView.display?.rotation ?: 0
    }

    /** 카메라 프레임: 캡처보드 입력은 최신 프레임 보관, 자동 캡처는 변화 감지 */
    private fun onFrame(img: ImageProxy, board: Boolean) {
        val now = SystemClock.elapsedRealtime()
        val needSig = switchAuto.isChecked && now - lastSigAt >= 200
        if (!board && !needSig) return
        var bmp = img.toBitmap()
        val rot = img.imageInfo.rotationDegrees
        if (rot != 0) {
            val m = Matrix().apply { postRotate(rot.toFloat()) }
            bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true)
        }
        if (board) lastFrame = bmp
        if (!needSig) return
        lastSigAt = now
        val small = Bitmap.createScaledBitmap(bmp, 32, 18, true)
        val px = IntArray(32 * 18)
        small.getPixels(px, 0, 32, 0, 0, 32, 18)
        val sig = FloatArray(px.size) { i ->
            val c = px[i]
            0.299f * Color.red(c) + 0.587f * Color.green(c) + 0.114f * Color.blue(c)
        }
        val prev = prevSig
        prevSig = sig
        if (prev == null) {
            stableSince = now
            return
        }
        if (diffFrac(sig, prev, 12f) > 0.03f) {
            stableSince = now          // 아직 화면이 바뀌는 중
            return
        }
        val thr = when (prefs.getString("sensitivity", "mid")) {
            "high" -> 0.02f
            "low" -> 0.10f
            else -> 0.05f
        }
        val last = lastCapSig
        if (now - stableSince >= 1200 && (last == null || diffFrac(sig, last, 18f) > thr) && !capturing) {
            lastCapSig = sig
            runOnUiThread { capture(true) }
        }
    }

    private fun diffFrac(a: FloatArray, b: FloatArray, t: Float): Float {
        var n = 0
        for (i in a.indices) if (abs(a[i] - b[i]) > t) n++
        return n.toFloat() / a.size
    }

    // ------------------------------------------------------------------ 캡처
    private fun flash() {
        flashView.visibility = View.VISIBLE
        flashView.alpha = 0.7f
        flashView.animate().alpha(0f).setDuration(250).withEndAction { flashView.visibility = View.GONE }.start()
    }

    private fun capture(auto: Boolean) {
        if (capturing) return
        if (cameraProvider == null) {
            toast("카메라가 준비되지 않았습니다.")
            return
        }
        val board = source() == "board"
        if (board && boardMissing) {
            toast("USB 캡처보드가 연결되지 않았습니다.")
            return
        }
        capturing = true
        flash()
        val ts = System.currentTimeMillis()
        val cap = imageCapture
        if (board || cap == null) {
            val b = lastFrame
            if (b == null) {
                capturing = false
                toast("화면 신호가 없습니다.")
                return
            }
            bg.execute {
                val tmp = File(cacheDir, "cap_$ts.png")
                tmp.outputStream().use { b.compress(Bitmap.CompressFormat.PNG, 100, it) }
                finishCapture(tmp, if (board) "board" else "camera", auto)
            }
        } else {
            val tmp = File(cacheDir, "cap_$ts.jpg")
            cap.takePicture(ImageCapture.OutputFileOptions.Builder(tmp).build(), bg,
                object : ImageCapture.OnImageSavedCallback {
                    override fun onImageSaved(r: ImageCapture.OutputFileResults) = finishCapture(tmp, "camera", auto)
                    override fun onError(e: ImageCaptureException) {
                        runOnUiThread {
                            capturing = false
                            toast("촬영 실패: ${e.message}")
                        }
                    }
                })
        }
    }

    /** bg 스레드: 화면 보정·검정 줄 제거(Python) 후 목록에 추가 */
    private fun finishCapture(tmp: File, src: String, auto: Boolean) {
        val out = store.newFile()
        val res = try {
            JSONObject(Py.ensure(this).getModule("phone_capture").callAttr(
                "process_capture", tmp.absolutePath, out.absolutePath, src, opt("correct"), opt("auto_aspect")
            ).toString())
        } catch (e: Throwable) {
            JSONObject().put("ok", false).put("error", e.message)
        }
        tmp.delete()
        runOnUiThread {
            capturing = false
            if (res.optBoolean("ok")) {
                store.add(out)
                reloadFrames()
                sel = frames.size - 1
                thumbs.adapter?.notifyDataSetChanged()
                thumbs.scrollToPosition(sel)
                val extra = buildList {
                    if (res.optBoolean("rectified")) add("화면 보정")
                    if (res.optBoolean("trimmed")) add("검정 여백 제거")
                }.joinToString(", ")
                setStatus("● ${if (auto) "자동 " else ""}캡처 ${frames.size}장" + if (extra.isNotEmpty()) " ($extra)" else "")
            } else {
                out.delete()
                setStatus("● 캡처 처리 실패: ${res.optString("error")}")
            }
        }
    }

    // ------------------------------------------------------------------ 기존파일열기
    private fun importUris(uris: List<Uri>) {
        setStatus("● 기존 파일 불러오는 중... (${uris.size}개)")
        bg.execute {
            val known = HashSet<String>()
            store.list().forEach { runCatching { known.add(SlideStore.md5(it)) } }
            known.addAll(prefs.getStringSet("imported_md5", emptySet()) ?: emptySet())
            val newMd5 = HashSet<String>()
            var added = 0
            var dup = 0
            val bad = mutableListOf<String>()
            for (u in uris) {
                val name = queryName(u)
                val tmp = File(cacheDir, "imp_${System.nanoTime()}")
                try {
                    contentResolver.openInputStream(u)!!.use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
                } catch (e: Exception) {
                    bad.add(name)
                    tmp.delete()
                    continue
                }
                if (!SlideStore.isImage(tmp)) {
                    bad.add(name)
                    tmp.delete()
                    continue
                }
                val md5 = SlideStore.md5(tmp)
                if (md5 in known || md5 in newMd5) {
                    dup++
                    tmp.delete()
                    continue
                }
                val out = store.newFile()
                val ok = try {
                    JSONObject(Py.ensure(this).getModule("phone_capture").callAttr(
                        "process_capture", tmp.absolutePath, out.absolutePath, "import", false, opt("auto_aspect")
                    ).toString()).optBoolean("ok")
                } catch (e: Throwable) {
                    false
                }
                tmp.delete()
                if (ok) {
                    store.add(out)
                    newMd5.add(md5)
                    added++
                } else {
                    out.delete()
                    bad.add(name)
                }
            }
            val all = (prefs.getStringSet("imported_md5", emptySet()) ?: emptySet()).toMutableSet()
            all.addAll(newMd5)
            prefs.edit().putStringSet("imported_md5", all).apply()
            runOnUiThread {
                reloadFrames()
                if (added > 0) {
                    sel = frames.size - 1
                    thumbs.adapter?.notifyDataSetChanged()
                    thumbs.scrollToPosition(sel)
                }
                var msg = "${added}장을 불러왔습니다."
                val skip = mutableListOf<String>()
                if (dup > 0) skip.add("이미 목록에 있는 파일 ${dup}장")
                if (bad.isNotEmpty()) skip.add("열 수 없는 파일 ${bad.size}장")
                if (skip.isNotEmpty()) {
                    msg += "\n건너뜀: " + skip.joinToString(", ")
                    if (bad.isNotEmpty()) msg += "\n(" + bad.take(5).joinToString(", ") + (if (bad.size > 5) " 외" else "") + ")"
                }
                setStatus("● 기존 파일 ${added}장 불러옴 (총 ${frames.size}장)")
                AlertDialog.Builder(this).setTitle("기존파일열기").setMessage(msg).setPositiveButton("확인", null).show()
            }
        }
    }

    @SuppressLint("Range")
    private fun queryName(u: Uri): String {
        return runCatching {
            contentResolver.query(u, null, null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)) else null
            }
        }.getOrNull() ?: (u.lastPathSegment ?: "파일")
    }

    // ------------------------------------------------------------------ PPT 변환(백그라운드)
    private fun export() {
        if (ConvertService.isRunning(this)) {
            AlertDialog.Builder(this).setTitle("PPT 변환 취소").setMessage("진행 중인 PPT 변환을 취소할까요?")
                .setPositiveButton("취소하기") { _, _ ->
                    startService(Intent(this, ConvertService::class.java).setAction(ConvertService.ACTION_CANCEL))
                }
                .setNegativeButton("계속 변환", null).show()
            return
        }
        frames = store.list()
        if (frames.isEmpty()) {
            AlertDialog.Builder(this).setTitle("캡처 필요").setMessage("먼저 슬라이드를 캡처하세요.")
                .setPositiveButton("확인", null).show()
            return
        }
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notiPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        val exact = opt("exact")
        val keepBg = exact || (opt("bg") && !opt("native_only"))
        val outDir = File(cacheDir, "out").apply { mkdirs() }
        // 시작 시점의 목록·옵션으로 고정 (변환 중 캡처·삭제·옵션 변경은 진행 중인 변환에 영향 없음)
        val job = JSONObject()
            .put("frames", JSONArray(frames.map { it.absolutePath }))
            .put("out", File(outDir, AppInfo.DEFAULT_PPT_NAME).absolutePath)
            .put("workdir", File(cacheDir, "_work_${System.currentTimeMillis()}").absolutePath)
            .put("app", AppInfo.TITLE)
            .put("auto_aspect", opt("auto_aspect"))
            .put("ocr", opt("ocr"))
            .put("tables", opt("tables"))
            .put("vectors", opt("vectors"))
            .put("keep_bg", keepBg)
            .put("inpaint", opt("inpaint"))
            .put("exact", exact)
            .put("tess", "")
        ContextCompat.startForegroundService(this,
            Intent(this, ConvertService::class.java).putExtra(ConvertService.EXTRA_JOB, job.toString()))
        btnExport.text = "PPT 변환 준비 중... · 취소"
        setStatus("● PPT 변환 시작 (${frames.size}장) - 다른 메뉴는 계속 사용할 수 있습니다")
    }

    private fun onConvertEvent(a: JSONArray) {
        when (a.getString(0)) {
            "progress" -> {
                btnExport.text = "PPT 변환 중 ${a.getInt(1)}/${a.getInt(2)} · 취소"
                setStatus("● PPT 변환 중... ${a.getInt(1)}/${a.getInt(2)} (${a.getString(3)}) - 다른 메뉴는 계속 사용할 수 있습니다")
            }
            "saving" -> {
                btnExport.text = "PPT 저장 중... · 취소"
                setStatus("● PPT 파일 저장 중...")
            }
            "done" -> {
                btnExport.text = "PPTX 생성 + 분석 보고서"
                val r = a.getJSONObject(1)
                val uri = Uri.parse(r.getString("uri"))
                setStatus("● PPTX 생성 완료 (${r.getInt("slides")}장)")
                var msg = "PPTX: ${r.optString("display")}\n분석 보고서(JSON)도 같은 폴더에 저장했습니다.\n" +
                    "슬라이드 ${r.getInt("slides")}장 · 모드: ${r.optString("mode")}\n" +
                    "원본과 일치가 확인된 글자만 편집 가능한 텍스트로 바꾸고, 나머지는 원본 그대로 두었습니다."
                val skipped = r.optJSONArray("skipped")
                if (skipped != null && skipped.length() > 0) {
                    msg += "\n\n읽을 수 없어 건너뛴 캡처 ${skipped.length()}장(변환 중 삭제 등)"
                }
                AlertDialog.Builder(this).setTitle("변환 완료").setMessage(msg)
                    .setPositiveButton("열기") { _, _ -> openPptx(uri) }
                    .setNeutralButton("공유") { _, _ -> sharePptx(uri) }
                    .setNegativeButton("닫기", null).show()
            }
            "error" -> {
                btnExport.text = "PPTX 생성 + 분석 보고서"
                setStatus("● PPT 생성 오류")
                AlertDialog.Builder(this).setTitle("PPT 생성 오류")
                    .setMessage(a.optString(1) + "\n\n" + a.optString(2).take(1500))
                    .setPositiveButton("확인", null).show()
            }
            "cancelled" -> {
                btnExport.text = "PPTX 생성 + 분석 보고서"
                setStatus("● PPT 변환을 취소했습니다.")
            }
        }
    }

    private fun openPptx(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, ConvertService.MIME_PPTX)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
        } catch (e: Exception) {
            toast("PPTX를 열 수 있는 앱이 없습니다 (PowerPoint 앱 설치 필요)")
        }
    }

    private fun sharePptx(uri: Uri) {
        val i = Intent(Intent.ACTION_SEND).setType(ConvertService.MIME_PPTX)
            .putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        startActivity(Intent.createChooser(i, "PPTX 공유"))
    }

    // ------------------------------------------------------------------ 환경설정
    private fun openSettings() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }
        fun label(t: String) = TextView(this).apply {
            text = t
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.band_fg))
            setPadding(0, pad / 2, 0, 0)
        }
        box.addView(label("입력 소스"))
        val src = RadioGroup(this)
        val rbCam = RadioButton(this).apply { text = "폰 카메라 촬영 (화면·프로젝터)"; id = View.generateViewId() }
        val rbBoard = RadioButton(this).apply { text = "USB HDMI 캡처보드 (OTG 연결)"; id = View.generateViewId() }
        src.addView(rbCam)
        src.addView(rbBoard)
        if (source() == "board") rbBoard.isChecked = true else rbCam.isChecked = true
        box.addView(src)
        box.addView(label("자동 캡처 민감도"))
        val sens = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val rs = listOf("low" to "낮음", "mid" to "보통", "high" to "높음").map { (k, t) ->
            RadioButton(this).apply { text = t; id = View.generateViewId(); tag = k }
        }
        rs.forEach { sens.addView(it) }
        val cur = prefs.getString("sensitivity", "mid")
        rs.firstOrNull { it.tag == cur }?.isChecked = true
        box.addView(sens)
        box.addView(TextView(this).apply {
            text = "결과 PPT 저장 위치: 다운로드/${AppInfo.OUT_FOLDER}\n캡처 이미지는 앱 저장 공간에 보관됩니다.\n" +
                "USB 캡처보드는 기기가 외장 카메라(UVC)로 인식해야 사용할 수 있습니다."
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.sub))
            textSize = 12f
            setPadding(0, pad / 2, 0, 0)
        })
        AlertDialog.Builder(this).setTitle("⚙ 환경설정").setView(box)
            .setPositiveButton("저장") { _, _ ->
                val newSrc = if (rbBoard.isChecked) "board" else "camera"
                val newSens = rs.firstOrNull { it.isChecked }?.tag as? String ?: "mid"
                val changed = newSrc != source()
                prefs.edit().putString("source", newSrc).putString("sensitivity", newSens).apply()
                if (changed) bindCamera()
            }
            .setNegativeButton("취소", null).show()
    }
}
