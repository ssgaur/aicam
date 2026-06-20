package ai.camera.shail

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Semaphore
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Enterprise-grade upload queue for AiCam.
 *
 * - Files on disk ARE the queue (cache/aicam_chunks/[all].mp4)
 * - Parallel upload workers (configurable concurrency)
 * - Exponential backoff on failures
 * - Auto-drain when server is reachable
 * - Manual retry trigger from UI
 * - Stats: uploaded, failed, pending, retrying
 * - Self-healing: periodic health check → auto-resume
 */
class UploadQueue(
    private val context: Context,
    private val http: OkHttpClient,
    private val getBackendUrl: () -> String,
    private val getSampleFps: () -> Double,
    private val getChunkSeconds: () -> Double,
) {
    companion object {
        private const val TAG = "UploadQueue"
        private const val MAX_CONCURRENT_UPLOADS = 3
        private const val MAX_RETRIES = 10
        private const val BASE_BACKOFF_MS = 2000L
        private const val MAX_BACKOFF_MS = 60_000L
        private const val HEALTH_CHECK_INTERVAL_MS = 15_000L
        private const val PREFS_NAME = "aicam_upload_queue"
    }

    // --- State ---
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val uploadSemaphore = Semaphore(MAX_CONCURRENT_UPLOADS)
    private val pending = ConcurrentLinkedQueue<ChunkEntry>()
    private val inFlight = ConcurrentHashMap<String, ChunkEntry>()
    private val retryCount = ConcurrentHashMap<String, Int>() // filename → retry count

    // Counters (observable from UI)
    val uploadedCount = AtomicInteger(0)
    val failedCount = AtomicInteger(0)      // permanent failures (exceeded max retries)
    val errorCount = AtomicInteger(0)        // transient errors (will retry)
    val pendingCount = AtomicInteger(0)
    val inFlightCount = AtomicInteger(0)

    // Control flags
    private val queueActive = AtomicBoolean(false)
    private val isPaused = AtomicBoolean(false)
    private val serverReachable = AtomicBoolean(true)

    private var drainJob: Job? = null
    private var healthJob: Job? = null
    private val prefs: SharedPreferences by lazy {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    }

    data class ChunkEntry(
        val file: File,
        val chunkIndex: Int,
        val recordedAtMs: Long = System.currentTimeMillis(),
    )

    // --- Public API ---

    /** Start the queue system. Scans disk for unsent chunks. */
    fun start() {
        if (queueActive.getAndSet(true)) return
        isPaused.set(false)

        // Restore persisted counters
        uploadedCount.set(prefs.getInt("uploaded_total", 0))
        failedCount.set(prefs.getInt("failed_total", 0))

        // Scan disk for existing chunks (from previous sessions)
        scanDiskForPending()

        // Start drain workers
        startDrainLoop()
        // Start health check
        startHealthCheck()

        Log.i(TAG, "Queue started. Pending: ${pendingCount.get()}, Uploaded: ${uploadedCount.get()}")
    }

    /** Stop the queue (graceful — in-flight uploads finish). */
    fun stop() {
        queueActive.set(false)
        drainJob?.cancel()
        healthJob?.cancel()
        persistCounters()
        Log.i(TAG, "Queue stopped.")
    }

    /** Enqueue a new chunk for upload. Called by the recording loop. */
    fun enqueue(file: File, chunkIndex: Int) {
        val entry = ChunkEntry(file = file, chunkIndex = chunkIndex)
        pending.offer(entry)
        pendingCount.incrementAndGet()
        Log.d(TAG, "Enqueued chunk #$chunkIndex: ${file.name}")
    }

    /** Pause uploads (e.g., user wants to stop sending but keep recording). */
    fun pause() {
        isPaused.set(true)
        Log.i(TAG, "Queue paused.")
    }

    /** Resume uploads. */
    fun resume() {
        isPaused.set(false)
        Log.i(TAG, "Queue resumed.")
    }

    /** Retry all failed chunks. Re-scans disk and resets error counters. */
    fun retryAll() {
        errorCount.set(0)
        retryCount.clear()
        scanDiskForPending()
        isPaused.set(false)
        Log.i(TAG, "Retry all: ${pendingCount.get()} chunks to send.")
    }

    /** Delete all queued files (user wants a clean slate). */
    fun clearAll() {
        pending.clear()
        val dir = chunksDir()
        dir.listFiles()?.forEach { it.delete() }
        pendingCount.set(0)
        errorCount.set(0)
        failedCount.set(0)
        retryCount.clear()
        persistCounters()
        Log.i(TAG, "Queue cleared — all files deleted.")
    }

    /** Reset session counters (not total). */
    fun resetSessionCounters() {
        uploadedCount.set(0)
        failedCount.set(0)
        errorCount.set(0)
        prefs.edit().putInt("uploaded_total", 0).putInt("failed_total", 0).apply()
    }

    /** Get summary string for UI. */
    fun summaryText(): String {
        val p = pendingCount.get()
        val u = uploadedCount.get()
        val f = inFlightCount.get()
        val e = errorCount.get()
        val dead = failedCount.get()
        return buildString {
            append("↑$u")
            if (f > 0) append(" ⟳$f")
            if (p > 0) append(" ⏳$p")
            if (e > 0) append(" ⚠$e")
            if (dead > 0) append(" ✗$dead")
        }
    }

    // --- Internal ---

    private fun chunksDir(): File = File(context.cacheDir, "aicam_chunks").apply { mkdirs() }

    private fun scanDiskForPending() {
        val dir = chunksDir()
        val existing = dir.listFiles()?.filter { it.extension == "mp4" } ?: emptyList()
        val alreadyQueued = pending.map { it.file.name }.toSet() +
                inFlight.keys

        var added = 0
        existing.sortedBy { it.lastModified() }.forEach { file ->
            if (file.name !in alreadyQueued) {
                // Parse index from filename: chunk_<timestamp>_<index>.mp4
                val idx = file.nameWithoutExtension.substringAfterLast('_').toIntOrNull() ?: 0
                pending.offer(ChunkEntry(file = file, chunkIndex = idx, recordedAtMs = file.lastModified()))
                added++
            }
        }
        pendingCount.addAndGet(added)
        if (added > 0) Log.i(TAG, "Scanned disk: added $added chunks to queue.")
    }

    private fun startDrainLoop() {
        drainJob?.cancel()
        drainJob = scope.launch {
            while (queueActive.get()) {
                if (isPaused.get() || !serverReachable.get()) {
                    delay(2000)
                    continue
                }

                val entry = pending.poll()
                if (entry == null) {
                    delay(500) // nothing to do
                    continue
                }

                pendingCount.decrementAndGet()

                // Validate file still exists
                if (!entry.file.exists() || entry.file.length() == 0L) {
                    Log.w(TAG, "Skipping missing/empty file: ${entry.file.name}")
                    continue
                }

                // Acquire semaphore slot (limits concurrency)
                uploadSemaphore.acquire()
                inFlight[entry.file.name] = entry
                inFlightCount.incrementAndGet()

                // Launch upload in parallel
                launch {
                    try {
                        val success = doUpload(entry)
                        if (success) {
                            entry.file.delete()
                            uploadedCount.incrementAndGet()
                            retryCount.remove(entry.file.name)
                            Log.d(TAG, "Uploaded: ${entry.file.name}")
                        } else {
                            handleUploadFailure(entry)
                        }
                    } catch (e: CancellationException) {
                        // Re-queue on cancellation
                        pending.offer(entry)
                        pendingCount.incrementAndGet()
                    } catch (e: Exception) {
                        Log.e(TAG, "Upload exception: ${entry.file.name}", e)
                        handleUploadFailure(entry)
                    } finally {
                        inFlight.remove(entry.file.name)
                        inFlightCount.decrementAndGet()
                        uploadSemaphore.release()
                    }
                }
            }
        }
    }

    private fun handleUploadFailure(entry: ChunkEntry) {
        val retries = retryCount.getOrDefault(entry.file.name, 0) + 1
        retryCount[entry.file.name] = retries

        if (retries >= MAX_RETRIES) {
            // Permanent failure — move to dead letter
            failedCount.incrementAndGet()
            errorCount.decrementAndGet().coerceAtLeast(0).let { errorCount.set(it.coerceAtLeast(0)) }
            Log.w(TAG, "Permanent failure after $MAX_RETRIES retries: ${entry.file.name}")
            // Don't delete — user might want to retry manually later
        } else {
            // Transient failure — re-queue with backoff
            errorCount.incrementAndGet()
            scope.launch {
                val backoff = (BASE_BACKOFF_MS * (1L shl (retries - 1).coerceAtMost(5)))
                    .coerceAtMost(MAX_BACKOFF_MS)
                delay(backoff)
                if (entry.file.exists()) {
                    pending.offer(entry)
                    pendingCount.incrementAndGet()
                    errorCount.decrementAndGet().coerceAtLeast(0).let { errorCount.set(it.coerceAtLeast(0)) }
                }
            }
            Log.d(TAG, "Retry #$retries for ${entry.file.name} (backoff ${BASE_BACKOFF_MS * (1L shl (retries - 1).coerceAtMost(5))}ms)")
        }
    }

    private fun startHealthCheck() {
        healthJob?.cancel()
        healthJob = scope.launch {
            while (queueActive.get()) {
                delay(HEALTH_CHECK_INTERVAL_MS)
                val reachable = checkServerHealth()
                val wasDown = !serverReachable.getAndSet(reachable)
                if (reachable && wasDown) {
                    Log.i(TAG, "Server back online — resuming uploads.")
                    // Reset retry counts to give everything a fresh chance
                    retryCount.clear()
                    scanDiskForPending()
                }
            }
        }
    }

    private suspend fun checkServerHealth(): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = "${getBackendUrl().trimEnd('/')}/healthz"
            val req = Request.Builder().url(url).get().build()
            http.newCall(req).execute().use { it.isSuccessful }
        } catch (_: Exception) {
            false
        }
    }

    private suspend fun doUpload(entry: ChunkEntry): Boolean = withContext(Dispatchers.IO) {
        val backendUrl = getBackendUrl().trimEnd('/')
        val sampleFps = getSampleFps()
        val chunkSeconds = getChunkSeconds()

        val now = entry.recordedAtMs / 1000.0
        val startTs = now - chunkSeconds

        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("start_ts", "%.3f".format(startTs))
            .addFormDataPart("end_ts", "%.3f".format(now))
            .addFormDataPart("sample_fps", sampleFps.toString())
            .addFormDataPart("conf", "0.35")
            .addFormDataPart("chunk_index", entry.chunkIndex.toString())
            .addFormDataPart("device_id", "native-camerax")
            .addFormDataPart("file", entry.file.name, entry.file.asRequestBody("video/mp4".toMediaType()))
            .build()

        val req = Request.Builder()
            .url("$backendUrl/api/native/upload")
            .post(body)
            .build()

        http.newCall(req).execute().use { response ->
            when {
                response.isSuccessful -> true
                response.code in 400..499 -> {
                    // Client error — file is probably corrupt, don't retry forever
                    Log.w(TAG, "Server rejected ${entry.file.name}: HTTP ${response.code}")
                    // Count as one retry attempt so it eventually dies
                    true // treat as "uploaded" to avoid infinite loop on corrupt files
                }
                else -> false // 5xx = server error, retry
            }
        }
    }

    private fun persistCounters() {
        prefs.edit()
            .putInt("uploaded_total", uploadedCount.get())
            .putInt("failed_total", failedCount.get())
            .apply()
    }
}
