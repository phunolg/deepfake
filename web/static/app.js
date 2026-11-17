const $ = (id) => document.getElementById(id);
let upload = null;
let currentJob = null;
let pollTimer = null;
let selectedModelProfile = "mouth";

function showPage(pageId) {
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === pageId));
  document.querySelectorAll(".nav-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.page === pageId));
}

function showError(message) {
  $("upload-error").textContent = message;
  $("upload-error").classList.remove("hidden");
}
function clearError() { $("upload-error").classList.add("hidden"); }
function formatSeconds(value) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}
function meta(label, value) { return `<div class="meta-item"><span>${label}</span><b>${value}</b></div>`; }

async function uploadVideo(file) {
  clearError();
  if (!file) return;
  const form = new FormData();
  form.append("video", file);
  $("choose-btn").disabled = true;
  $("choose-btn").textContent = "Đang kiểm tra...";
  try {
    const response = await fetch("/api/upload", { method:"POST", body:form });
    const data = await response.json();
    if (response.status === 401) { window.location.href = "/login?next=/"; return; }
    if (!response.ok) throw new Error(data.error || "Upload thất bại.");
    upload = data;
    $("preview").src = data.media_url;
    $("hero-preview").src = data.media_url;
    $("hero-preview").classList.remove("hidden");
    $("drop-hint").classList.add("hidden");
    $("file-name").textContent = data.filename;
    const m = data.metadata;
    $("metadata-grid").innerHTML = [
      meta("Định dạng", m.format.toUpperCase()),
      meta("Độ phân giải", `${m.width} × ${m.height}`),
      meta("FPS", m.fps),
      meta("Thời lượng", formatSeconds(m.duration)),
      meta("Audio", m.has_audio ? "Có" : "Không"),
      meta("Dung lượng", `${(file.size / 1024 / 1024).toFixed(1)} MB`)
    ].join("");
    $("analyze-btn").disabled = false;
  } catch (error) { showError(error.message); }
  finally { $("choose-btn").disabled = false; $("choose-btn").textContent = "Chọn video"; }
}

async function startAnalysis() {
  if (!upload) return;
  $("analyze-btn").disabled = true;
  $("progress-panel").classList.remove("hidden");
  $("result-panel").classList.add("hidden");
  $("progress-bar").style.width = "0%";
  $("progress-text").textContent = "0%";
  const response = await fetch("/api/analyze", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({
      upload_id: upload.upload_id, filename: upload.filename,
      threshold: Number($("threshold").value) / 100,
      max_seconds: Number($("max-seconds").value),
      max_chunks: Number($("max-chunks").value),
      crop_mode: selectedModelProfile === "face" ? "face" : $("crop-mode").value,
      model_profile: selectedModelProfile
    })
  });
  const data = await response.json();
  if (response.status === 401) { window.location.href = "/login?next=/"; return; }
  if (!response.ok) { showError(data.error || "Không thể bắt đầu phân tích."); $("analyze-btn").disabled = false; return; }
  currentJob = data.job_id;
  pollTimer = setInterval(pollJob, 700);
  pollJob();
}

async function pollJob() {
  const response = await fetch(`/api/jobs/${currentJob}`);
  const job = await response.json();
  const progress = Number(job.progress || 0);
  $("progress-bar").style.width = `${progress}%`;
  $("progress-text").textContent = `${progress}%`;
  $("status-message").textContent = job.message || "";
  if (job.status === "completed") {
    clearInterval(pollTimer);
    window.location.href = job.report_url || `/analysis/${currentJob}`;
  } else if (job.status === "failed") {
    clearInterval(pollTimer); showError(job.error || "Phân tích thất bại."); $("analyze-btn").disabled = false;
  }
}

function renderResult(result, reportUrl) {
  $("result-panel").classList.remove("hidden");
  $("overall-label").textContent = result.overall_label;
  $("overall-label").style.color = result.overall_label === "FAKE" ? "var(--red)" : "var(--green)";
  $("overall-score").textContent = result.overall_score == null ? "N/A" : Number(result.overall_score).toFixed(4);
  $("valid-count").textContent = `${result.valid_chunks_count} / ${result.valid_chunks_count + result.skipped_count}`;
  $("overall-distance").textContent = result.overall_distance == null ? "N/A" : Number(result.overall_distance).toFixed(4);
  $("report-threshold").textContent = Number(result.threshold).toFixed(2).replace(".", ",");
  $("report-model").textContent = result.checkpoint_info.run_tag || `epoch${result.checkpoint_info.epoch || "50"}`;
  $("report-file").textContent = `${result.video_info.format?.toUpperCase() || "VIDEO"} · ${result.video_info.duration.toFixed(2)} GIÂY`;
  $("report-link").href = reportUrl;
  const timeline = $("timeline");
  timeline.innerHTML = "";
  result.chunks.forEach((chunk, index) => {
    const el = document.createElement("button");
    el.className = "segment " + (chunk.status === "skipped" ? "" : (chunk.score >= result.threshold ? "high" : "low"));
    el.innerHTML = `<span>${chunk.start}-${chunk.end}s: ${chunk.status === "valid" ? `P=${Number(chunk.score).toFixed(3)}` : "Bị loại"}</span>`;
    el.title = `${chunk.start}-${chunk.end}s`;
    el.onclick = () => selectSegment(chunk, el);
    timeline.appendChild(el);
    if (index === 0) selectSegment(chunk, el);
  });
  $("distance-strip").innerHTML = result.chunks.map((chunk) => {
    const distance = chunk.distance == null ? 0.12 : Number(chunk.distance);
    const opacity = Math.min(1, Math.max(.18, distance * 3));
    return `<div style="opacity:${opacity.toFixed(2)}"></div>`;
  }).join("");
  $("technical-info").innerHTML = [
    meta("Bộ dò mặt", result.detector_status || "—"),
    meta("Landmarker", result.landmarker_status || "—"),
    meta("Thiết bị", result.device || "—"),
    meta("Model", result.checkpoint_info.run_tag || "—"),
    meta("Video", `${result.video_info.width} × ${result.video_info.height}`),
    meta("Thời lượng", formatSeconds(result.video_info.duration))
  ].join("");
  renderAudioVisual(result);
  $("result-panel").scrollIntoView({ behavior:"smooth", block:"start" });
}

function renderAudioVisual(result) {
  const metrics = result.av_metrics || {};
  const valid = result.chunks.filter((chunk) => chunk.status === "valid" && chunk.distance != null);
  const anomalyCount = valid.filter((chunk) => chunk.relative_av === "cao_hon_trung_binh").length;
  const correlation = metrics.correlation == null ? "—" : `r = ${Number(metrics.correlation).toFixed(2)}`;
  const stat = (label, value, note, red = false) =>
    `<div class="av-stat"><span class="label">${label}</span><strong class="${red ? "red" : ""}">${value}</strong><span class="label">${note}</span></div>`;
  $("av-stats").innerHTML = [
    stat("A-V DISTANCE TRUNG BÌNH", metrics.mean == null ? "—" : Number(metrics.mean).toFixed(2), "Giá trị tương đối"),
    stat("BIẾN THIÊN GIỮA ĐOẠN", metrics.variation == null ? "—" : Number(metrics.variation).toFixed(2), `${valid.length} đoạn hợp lệ`),
    stat("ĐOẠN CAO HƠN XU HƯỚNG", `${anomalyCount} / ${valid.length}`, "Không phải kết luận độc lập", anomalyCount > 0),
    stat("TƯƠNG QUAN VỚI P(FAKE)", correlation, "Mô tả trong video này")
  ].join("");

  const maxDistance = Math.max(...valid.map((chunk) => Number(chunk.distance)), 0.001);
  const points = $("av-line-chart");
  points.innerHTML = result.chunks.map((chunk, index) => {
    if (chunk.distance == null) return `<div class="av-point" style="--height:0%;--angle:0deg" title="${chunk.start}-${chunk.end}s: bị loại"></div>`;
    const height = `${Math.max(5, Number(chunk.distance) / maxDistance * 82)}%`;
    const next = valid[index + 1];
    const angle = next ? `${Math.atan2((Number(next.distance) - Number(chunk.distance)), 1) * 18}deg` : "0deg";
    return `<div class="av-point ${chunk.relative_av === "cao_hon_trung_binh" ? "high" : ""}" style="--height:${height};--angle:${angle}" title="${chunk.start}-${chunk.end}s: A-V ${Number(chunk.distance).toFixed(3)}"></div>`;
  }).join("");

  $("scatter-chart").innerHTML = valid.map((chunk) => {
    const x = Math.max(3, Math.min(97, Number(chunk.distance) / maxDistance * 94));
    const y = Math.max(3, Math.min(97, Number(chunk.score) * 94));
    return `<i class="scatter-point ${chunk.relative_av === "cao_hon_trung_binh" ? "high" : ""}" style="left:${x}%;bottom:${y}%" title="${chunk.start}-${chunk.end}s"></i>`;
  }).join("");

  let previous = null;
  $("av-table").innerHTML = result.chunks.map((chunk) => {
    const delta = chunk.distance == null || previous == null ? "—" : `${Number(chunk.distance - previous).toFixed(2)}`;
    if (chunk.distance != null) previous = Number(chunk.distance);
    const status = chunk.status === "skipped"
      ? `<span class="status-pill">BỊ LOẠI</span>`
      : chunk.relative_av === "cao_hon_trung_binh"
        ? `<span class="status-pill warning">CAO HƠN XU HƯỚNG</span>`
        : `<span class="status-pill">TRONG XU HƯỚNG</span>`;
    return `<tr><td>${chunk.start + 1}</td><td>${chunk.distance == null ? "—" : Number(chunk.distance).toFixed(2)}</td><td>${delta}</td><td>${chunk.score == null ? "—" : Number(chunk.score).toFixed(2)}</td><td>${status}</td></tr>`;
  }).join("");
}

function selectSegment(chunk, element) {
  document.querySelectorAll(".segment").forEach((item) => item.classList.remove("active"));
  element.classList.add("active");
  $("preview").currentTime = chunk.start;
  if (chunk.overlay_url) {
    $("overlay-image").src = chunk.overlay_url;
    $("overlay-image").alt = `Đoạn ${chunk.start}-${chunk.end}s: bounding box khuôn mặt và crop miệng`;
  } else {
    $("overlay-image").removeAttribute("src");
  }
  $("crop-gallery").innerHTML = (chunk.crop_urls || []).map((url, index) =>
    `<img src="${url}" alt="Frame crop mẫu ${index + 1} của đoạn ${chunk.start}-${chunk.end}s">`
  ).join("");
  const cropUrls = chunk.crop_urls || [];
  $("crop-strip").innerHTML = Array.from({ length: 5 }, (_, index) => {
    const url = cropUrls[index % Math.max(cropUrls.length, 1)];
    return url
      ? `<div class="crop-frame"><img src="${url}" alt="Ảnh crop vùng miệng ${index + 1}"><span>${index === 0 ? "ẢNH CROP 224×224" : "ẢNH CROP"}</span></div>`
      : `<div class="crop-frame placeholder"><span>ĐOẠN BỊ LOẠI</span></div>`;
  }).join("");
  if (chunk.status === "valid") {
    $("segment-detail").innerHTML = `<div class="detail-row">
      <div><span class="label">Khoảng thời gian</span><div class="detail-value">${formatSeconds(chunk.start)} - ${formatSeconds(chunk.end)}</div></div>
      <div><span class="label">P(FAKE)</span><div class="detail-value">${Number(chunk.score).toFixed(4)} · ${chunk.label}</div></div>
      <div><span class="label">A-V distance</span><div class="detail-value">${Number(chunk.distance).toFixed(4)}</div></div>
    </div>`;
  } else {
    $("segment-detail").innerHTML = `<div class="detail-row">
      <div><span class="label">Khoảng thời gian</span><div class="detail-value">${formatSeconds(chunk.start)} - ${formatSeconds(chunk.end)}</div></div>
      <div><span class="label">Trạng thái</span><div class="detail-value reason">BỊ LOẠI</div></div>
      <div><span class="label">Lý do</span><div class="detail-value reason">${chunk.reason || "Không rõ"}</div></div>
    </div>`;
  }
}

$("choose-btn").onclick = () => $("video-input").click();
$("hero-check").onclick = () => {
  if (upload) {
    $("upload-status").classList.remove("hidden");
    showPage("page-check");
  } else {
    $("video-input").click();
  }
};
$("sample-report").onclick = () => showPage("page-report");
$("model-profile").onchange = (event) => {
  selectedModelProfile = event.target.value;
  if (selectedModelProfile === "face") {
    $("crop-mode").value = "face";
    $("crop-mode").disabled = true;
  } else {
    $("crop-mode").disabled = false;
    $("crop-mode").value = "mouth_lm";
  }
};
$("face-test-button").onclick = () => {
  selectedModelProfile = "mouth";
  $("model-profile").value = "mouth";
  $("crop-mode").value = "mouth_lm";
  $("crop-mode").disabled = false;
  showPage("page-check");
  $("video-input").click();
};
$("video-input").onchange = (event) => uploadVideo(event.target.files[0]);
$("analyze-btn").onclick = startAnalysis;
$("threshold").oninput = (event) => $("threshold-value").textContent = (Number(event.target.value) / 100).toFixed(2);
["dragenter","dragover"].forEach((eventName) => $("dropzone").addEventListener(eventName, (event) => { event.preventDefault(); $("dropzone").classList.add("drag"); }));
["dragleave","drop"].forEach((eventName) => $("dropzone").addEventListener(eventName, (event) => { event.preventDefault(); $("dropzone").classList.remove("drag"); }));
$("dropzone").ondrop = (event) => uploadVideo(event.dataTransfer.files[0]);
document.querySelectorAll(".nav-tab").forEach((tab) => {
  tab.onclick = () => {
    if (tab.dataset.page === "page-check") {
      if (!upload) {
        showPage("page-home");
        $("video-input").click();
        return;
      }
      $("upload-status").classList.remove("hidden");
    }
    showPage(tab.dataset.page);
  };
});
showPage(document.body.dataset.initialPage || "page-home");
if (window.__analysisResult) {
  renderResult(window.__analysisResult, window.__analysisReportUrl || "#");
  showPage("page-report");
}
