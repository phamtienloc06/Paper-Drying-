"use strict";
const $ = (id) => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const phaseLabels = {IDLE:"Chờ bắt đầu",HEATING:"Gia nhiệt",HOLDING:"Giữ nhiệt",COOLING:"Làm nguội",COMPLETE:"Hoàn tất"};
const commandLabels = {SET_RECIPE:"Áp dụng thông số mẻ",START_DRYING:"Bắt đầu sấy",SET_MODE:"Dừng mẻ · OFF",EMERGENCY_STOP:"Yêu cầu dừng khẩn"};
const alarmHelp = {
  SENSOR_DISCONNECTED:["Lỗi cảm biến PT100","Kiểm tra nguồn, dây cảm biến và bus Modbus tại máy 1."],
  MODBUS_COMMUNICATION_LOST:["Mất giao tiếp Modbus","Kiểm tra cổng COM, nguồn thiết bị và đường RS485 tại máy 1."],
  OVER_TEMPERATURE:["Quá nhiệt","Kiểm tra nhiệt độ, bộ gia nhiệt và làm nguội tại máy 1 trước khi reset."],
  HEATER_CONTROL_FAILED:["Chưa xác nhận điều khiển gia nhiệt","Kiểm tra relay, phản hồi đầu ra và nguồn tải tại máy 1."],
  PROCESS_FAN_START_FAILED:["Chưa xác nhận điều khiển quạt","Kiểm tra relay quạt, nguồn tải và quạt tại máy 1."],
  EMERGENCY_STOPPED:["Đã yêu cầu dừng khẩn","Kiểm tra tình trạng tại chỗ; reset FAULT tại HMI sau khi an toàn."],
};
const fieldIds = ["setpoint-input","duration-input","cooling-input"];
const dirtyFields = new Set();
let latestPayload = null, receivedAt = 0, backendAvailable = false;
let activeView = "operation", commandBusy = false, emergencyBusy = false, pendingRecipe = null;
let stateRefreshRunning = false, historyRefreshRunning = false, historyAbortController = null;
let historyData = null, historyRefreshAt = 0, historyRequestId = 0, chartGeometry = null;
const commandHistory = [];

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const displayValue = (value, suffix = "") => finite(value) ? value.toFixed(1) + suffix : "—";
const dateText = (value) => finite(value) ? new Date(value).toLocaleString("vi-VN") : "—";
const operatorMessage = (value) => String(value || "").replace(/^INVALID_COMMAND:\s*/, "").replaceAll("LOCAL_HMI", "HMI máy 1");
function formatDuration(value) {
  if (!finite(value)) return "—";
  const seconds = Math.max(0, Math.floor(value));
  return Math.floor(seconds / 3600) > 0
    ? Math.floor(seconds / 3600) + " giờ " + Math.floor(seconds % 3600 / 60) + " phút"
    : Math.floor(seconds / 60) + " phút " + seconds % 60 + " giây";
}
function dataAge() {
  const age = latestPayload?.telemetry_age_s;
  return finite(age) ? age + Math.max(0, performance.now() - receivedAt) / 1000 : null;
}
function isFresh() { return backendAvailable && finite(dataAge()) && dataAge() <= 5; }
function setText(id, text) { $(id).textContent = text; }
function setStateColor(id, state) {
  for (const name of ["good","bad","neutral","unknown"]) $(id).classList.toggle("state-" + name, name === state);
}
function showBoolean(id, value) {
  setText(id, value === true ? "Kết nối" : value === false ? "Mất kết nối" : "Chưa xác nhận");
  setStateColor(id, value === true ? "good" : value === false ? "bad" : "unknown");
}
function recipeFromState(state) {
  return {temperature_setpoint_c:state.temperature_setpoint_c,drying_duration_s:state.drying_duration_s,cooling_stop_temperature_c:state.cooling_stop_temperature_c};
}
function recipeMatches(state, recipe) {
  return Object.keys(recipe).every((key) => finite(state[key]) && Math.abs(state[key] - recipe[key]) < 0.000001);
}
function renderState(payload) {
  latestPayload = payload;
  receivedAt = performance.now();
  backendAvailable = true;
  renderSnapshot();
}
function renderSnapshot() {
  const state = latestPayload?.state || {};
  const fresh = isFresh(), age = dataAge();
  const known = (value) => fresh ? value : null;
  const source = state.simulation === true ? "MÔ PHỎNG" : state.simulation === false ? "THIẾT BỊ THẬT" : "CHƯA XÁC ĐỊNH";
  setText("source-label", source);
  $("cloud-indicator").classList.toggle("is-online", fresh);
  setText("cloud-status", !backendAvailable ? "Chưa kết nối SCADA" : fresh ? "Dữ liệu máy 1 còn mới" : "Dữ liệu chưa xác nhận");
  $("data-banner").hidden = fresh;
  setText("data-banner", !latestPayload
    ? "Đang chờ dữ liệu máy 1. Các giá trị thiết bị chưa được xác nhận."
    : !backendAvailable ? "Không đọc được SCADA. Trạng thái thiết bị hiện tại chưa xác nhận; vẫn có thể gửi yêu cầu dừng khẩn."
    : "Snapshot máy 1 đã cũ hoặc thiếu timestamp. Khóa thao tác vận hành thông thường cho đến khi nhận dữ liệu mới.");
  setText("updated-at", finite(state.timestamp_ms) ? new Date(state.timestamp_ms).toLocaleTimeString("vi-VN") : "—");
  setText("sample-age", finite(age) ? "Tuổi dữ liệu: " + age.toFixed(1) + " giây" : "Chưa có timestamp");
  const temperatureValid = fresh && state.sensor_connected === true && finite(state.temperature_c);
  setText("temperature", temperatureValid ? displayValue(state.temperature_c) : "—");
  setText("pv-quality", temperatureValid ? "Mẫu đo hợp lệ" : !fresh ? "Chưa xác nhận dữ liệu" : "Chưa có mẫu cảm biến hợp lệ");
  $("pv-quality").classList.toggle("is-valid", temperatureValid);
  document.querySelector(".chamber").classList.toggle("is-stale", !temperatureValid);
  setText("temperature-setpoint", fresh ? displayValue(state.temperature_setpoint_c, " °C") : "— °C");
  $("temperature-bar").style.width = (temperatureValid ? Math.max(0, Math.min(100, state.temperature_c)) : 0) + "%";
  $("setpoint-marker").style.left = (fresh && finite(state.temperature_setpoint_c) ? Math.max(0, Math.min(100, state.temperature_setpoint_c)) : 0) + "%";
  $("setpoint-marker").hidden = !fresh;
  setText("hold-band-label", fresh && finite(state.control_deadband_c) && finite(state.temperature_setpoint_c)
    ? displayValue(state.temperature_setpoint_c - state.control_deadband_c) + "–" + displayValue(state.temperature_setpoint_c + state.control_deadband_c) + " °C"
    : "Vùng giữ nhiệt: —");
  const mode = fresh && typeof state.mode === "string" ? state.mode : "CHƯA XÁC NHẬN";
  setText("mode", mode);
  $("mode").classList.toggle("is-fault", mode === "FAULT");
  $("mode").classList.toggle("is-auto", mode === "AUTO");
  $("mode").classList.toggle("is-manual", mode === "MANUAL");
  setText("phase", fresh ? phaseLabels[state.auto_phase] || "Chưa xác định" : "Chưa xác định");
  document.querySelectorAll("[data-phase]").forEach((node, index) => {
    const current = ["HEATING","HOLDING","COOLING","COMPLETE"].indexOf(state.auto_phase);
    node.classList.toggle("is-current", fresh && node.dataset.phase === state.auto_phase);
    node.classList.toggle("is-complete", fresh && index < current);
  });
  setText("timer-status", !fresh ? "Tiến độ chưa xác nhận"
    : state.hold_timer_running === true ? "Đang đếm thời gian đủ điều kiện giữ nhiệt"
    : state.auto_phase === "HOLDING" ? "Tạm dừng đếm: nhiệt độ ngoài vùng giữ nhiệt"
    : state.auto_phase === "COMPLETE" ? "Mẻ đã hoàn tất"
    : "Chỉ cộng thời gian khi đủ điều kiện giữ nhiệt");
  setText("elapsed", fresh ? formatDuration(state.drying_elapsed_s) : "—");
  setText("remaining", fresh ? formatDuration(state.drying_remaining_s) : "—");
  $("drying-progress").style.width = (fresh && finite(state.drying_duration_s) && state.drying_duration_s > 0 && finite(state.drying_elapsed_s)
    ? Math.max(0, Math.min(100, state.drying_elapsed_s / state.drying_duration_s * 100)) : 0) + "%";
  for (const [name, value, feedback] of [
    ["heater", known(state.heater_on), known(state.relay_connected === true ? state.relay_feedback_heater : null)],
    ["fan", known(state.process_fan_on), known(state.relay_connected === true ? state.relay_feedback_process_fan : null)],
  ]) {
    setText(name + "-state", value === true ? "LỆNH ON" : value === false ? "LỆNH OFF" : "CHƯA XÁC NHẬN");
    setText(name + "-feedback", feedback === true ? "ON" : feedback === false ? "OFF" : "Chưa xác nhận");
    setStateColor(name + "-feedback", feedback === true ? "good" : feedback === false ? "neutral" : "unknown");
    $(name + "-equipment").classList.toggle("is-on", value === true);
    $(name + "-equipment").classList.toggle("is-unknown", typeof value !== "boolean");
  }
  showBoolean("backend-connected", backendAvailable);
  showBoolean("telemetry-connected", fresh ? true : latestPayload ? false : null);
  for (const name of ["sensor","modbus","mqtt","relay"]) showBoolean(name + "-connected", known(state[name + "_connected"]));
  setText("data-quality", fresh ? "DỮ LIỆU MỚI" : latestPayload ? "CHƯA XÁC NHẬN" : "CHƯA CÓ DỮ LIỆU");
  setStateColor("data-quality", fresh ? "good" : "unknown");
  setText("device-timestamp", dateText(state.timestamp_ms));
  setText("diagnostic-age", finite(age) ? age.toFixed(1) + " giây" : "Chưa xác định");
  renderAlarm(state, fresh);
  if (pendingRecipe) {
    if (fresh && recipeMatches(state, pendingRecipe.values)) {
      pendingRecipe = null;
      dirtyFields.clear();
      showCommandResult("Máy 1 đã xác nhận đủ ba thông số mới qua telemetry.", true);
    } else if (performance.now() - pendingRecipe.startedAt > 15000) {
      pendingRecipe = null;
      showCommandResult("Controller đã chấp nhận, nhưng chưa xác nhận đủ thông số qua telemetry. Bản nhập được giữ lại; kiểm tra kết nối trước khi gửi lại.", false);
    }
  }
  const values = [state.temperature_setpoint_c, finite(state.drying_duration_s) ? state.drying_duration_s / 60 : null, state.cooling_stop_temperature_c];
  fieldIds.forEach((id, index) => {
    if (fresh && !dirtyFields.has(id) && !pendingRecipe && document.activeElement?.id !== id && finite(values[index])) $(id).value = values[index];
    $(id).classList.toggle("is-dirty", dirtyFields.has(id));
  });
  if (finite(state.minimum_setpoint_c)) $("setpoint-input").min = state.minimum_setpoint_c;
  if (finite(state.maximum_setpoint_c)) $("setpoint-input").max = state.maximum_setpoint_c;
  setText("recipe-limits", finite(state.minimum_setpoint_c) && finite(state.maximum_setpoint_c)
    ? "Điểm đặt: " + state.minimum_setpoint_c + "–" + state.maximum_setpoint_c + " °C. Ngưỡng dừng quạt thấp hơn điểm đặt" + (finite(state.control_deadband_c) ? " − " + state.control_deadband_c + " °C." : ".")
    : "Máy 1 kiểm tra giới hạn và liên động của toàn bộ thông số.");
  updateControls();
}
function updateControls() {
  const state = latestPayload?.state || {};
  const fresh = isFresh();
  const active = state.mode === "AUTO" || ["HEATING","HOLDING","COOLING"].includes(state.auto_phase);
  const fault = state.mode === "FAULT";
  const recipeReady = Object.values(recipeFromState(state)).every(finite);
  const busy = commandBusy || emergencyBusy;
  const locked = !fresh || active || fault || busy || Boolean(pendingRecipe) || !recipeReady;
  fieldIds.forEach((id) => { $(id).disabled = locked; });
  $("save-settings").disabled = locked || dirtyFields.size === 0;
  $("discard-settings").disabled = busy || Boolean(pendingRecipe) || dirtyFields.size === 0;
  $("start-button").disabled = !fresh || !recipeReady || active || fault || busy || Boolean(pendingRecipe) || dirtyFields.size > 0
    || state.sensor_connected !== true || state.modbus_connected !== true || state.relay_connected !== true;
  $("stop-button").disabled = !fresh || busy || state.mode === "OFF";
  $("emergency-button").disabled = emergencyBusy;
  setText("draft-status", pendingRecipe ? "Chờ telemetry xác nhận" : dirtyFields.size ? "Có thay đổi chưa áp dụng" : fresh ? "Thông số máy 1" : "Chờ dữ liệu");
  $("draft-status").classList.toggle("is-dirty", dirtyFields.size > 0 || Boolean(pendingRecipe));
  setText("control-reason", commandBusy ? "Đang chờ phản hồi yêu cầu. Dừng khẩn vẫn có thể gửi riêng."
    : pendingRecipe ? "Đã chấp nhận; đang chờ máy 1 gửi lại bộ thông số."
    : !fresh ? "Chờ dữ liệu mới để cho phép vận hành."
    : fault ? "Hệ thống đang FAULT. Xử lý nguyên nhân và reset tại HMI máy 1."
    : active ? "Mẻ đang chạy; dừng mẻ trước khi chỉnh thông số."
    : dirtyFields.size ? "Áp dụng hoặc hoàn tác bản nhập trước khi bắt đầu mẻ."
    : state.sensor_connected !== true || state.modbus_connected !== true || state.relay_connected !== true ? "Chưa xác nhận đủ cảm biến và relay để bắt đầu."
    : "Sẵn sàng nhận yêu cầu bắt đầu mẻ.");
}
function renderAlarm(state, fresh) {
  const alarm = state.active_alarm && typeof state.active_alarm === "object" ? state.active_alarm : null;
  const code = alarm?.code || state.alarm_code;
  const hasAlarm = Boolean(code && code !== "NONE");
  const title = alarmHelp[code]?.[0] || code || "Chưa xác nhận";
  const known = fresh && typeof code === "string";
  $("alarm-banner").hidden = !hasAlarm;
  setText("alarm-banner-title", (fresh ? "" : "CẢNH BÁO GẦN NHẤT · ") + title);
  setText("alarm-banner-message", fresh ? operatorMessage(alarm?.message || "Kiểm tra chi tiết cảnh báo.") : "Dữ liệu đã cũ; chưa xác nhận tình trạng hiện tại.");
  $("active-alarm-card").classList.toggle("has-alarm", hasAlarm);
  setText("alarm-count", hasAlarm ? "1" : known ? "0" : "—");
  setText("active-alarm", !known ? hasAlarm ? "Chưa xác nhận · " + title : "Chưa xác nhận cảnh báo" : hasAlarm ? title : "Không có cảnh báo đang hoạt động");
  setText("alarm-message", !known ? "Chờ dữ liệu mới từ máy 1." : hasAlarm ? operatorMessage(alarm?.message || code) : "Theo snapshot mới nhất từ máy 1.");
  setText("active-alarm-priority", hasAlarm ? alarm?.severity === "WARNING" ? "CẢNH BÁO" : "LỖI / FAULT" : "—");
  setText("alarm-raised-at", dateText(alarm?.raised_at_ms));
  setText("alarm-action", hasAlarm ? alarmHelp[code]?.[1] || "Kiểm tra nguyên nhân tại máy 1." : known ? "Tiếp tục theo dõi quá trình." : "Kiểm tra kết nối và tuổi dữ liệu.");
}
function switchView() {
  const requested = window.location.hash.slice(1);
  activeView = ["operation","trends","alarms","diagnostics"].includes(requested) ? requested : "operation";
  document.querySelectorAll("[data-panel]").forEach((node) => { node.hidden = node.dataset.panel !== activeView; });
  document.querySelectorAll("[data-view]").forEach((node) => {
    if (node.dataset.view === activeView) node.setAttribute("aria-current","page"); else node.removeAttribute("aria-current");
  });
  const titles = {operation:["VẬN HÀNH / TỔNG QUAN","Trạng thái hệ thống"],trends:["GIÁM SÁT / XU HƯỚNG","Xu hướng nhiệt độ"],alarms:["GIÁM SÁT / CẢNH BÁO","Cảnh báo và sự kiện"],diagnostics:["KẾT NỐI / CHẨN ĐOÁN","Chất lượng dữ liệu"]};
  setText("page-eyebrow", titles[activeView][0]);
  setText("page-title", titles[activeView][1]);
  if (activeView === "trends") window.requestAnimationFrame(drawChart);
  maybeRefreshHistory();
}
async function fetchJson(url, options = {}) {
  const response = await fetch(url, {cache:"no-store",credentials:"same-origin",...options});
  const data = await response.json();
  if (response.status === 401) { window.location.reload(); throw new Error("Phiên đăng nhập đã hết hạn."); }
  if (!response.ok) throw new Error(data.error || data.message || "HTTP " + response.status);
  return data;
}
async function refreshState() {
  if (stateRefreshRunning) return;
  stateRefreshRunning = true;
  const abort = new AbortController();
  const timeout = window.setTimeout(() => abort.abort(), 5000);
  try {
    const payload = await fetchJson("/api/state", {signal:abort.signal});
    if (!payload || !payload.state || typeof payload.state !== "object") throw new Error("Snapshot máy 1 không hợp lệ.");
    renderState(payload);
    setText("last-data-error", "Không có");
  } catch (error) {
    backendAvailable = false;
    setText("last-data-error", error.name === "AbortError" ? "API không phản hồi trong 5 giây." : error.message);
    renderSnapshot();
  } finally {
    window.clearTimeout(timeout);
    stateRefreshRunning = false;
    maybeRefreshHistory();
  }
}
function maybeRefreshHistory() {
  if (["trends","alarms"].includes(activeView) && !historyRefreshRunning && Date.now() - historyRefreshAt > 60000) void refreshHistory();
}
async function refreshHistory() {
  const requestId = ++historyRequestId;
  if (historyAbortController) historyAbortController.abort();
  const abort = new AbortController();
  historyAbortController = abort;
  historyRefreshRunning = true;
  const timeout = window.setTimeout(() => abort.abort(), 25000);
  const hours = $("history-range").value;
  historyRefreshAt = Date.now();
  setText("history-status", "Đang tải lịch sử " + hours + " giờ…");
  setText("alarm-history-status", "Đang tải sự kiện trong " + hours + " giờ…");
  try {
    const result = await fetchJson("/api/history?hours=" + encodeURIComponent(hours), {signal:abort.signal});
    if (requestId !== historyRequestId) return;
    if (!result.series || !finite(result.start_ts) || !finite(result.end_ts)) throw new Error("Dữ liệu lịch sử không hợp lệ.");
    historyData = result;
    setText("history-status", "Khoảng " + dateText(result.start_ts) + " → " + dateText(result.end_ts) + " · Giờ trình duyệt");
    setText("alarm-history-status", "Sự kiện trong " + hours + " giờ · Kết thúc lúc " + dateText(result.end_ts));
    $("export-history").disabled = !(validSeries(result.series.temperature_c).length || validSeries(result.series.temperature_setpoint_c).length);
    drawChart();
    renderAlarmHistory();
  } catch (error) {
    if (requestId !== historyRequestId) return;
    const message = (historyData ? "Chưa cập nhật; đang giữ lịch sử lần trước. " : "Không tải được lịch sử. ") + (error.name === "AbortError" ? "Yêu cầu quá thời gian chờ." : error.message);
    setText("history-status", message);
    setText("alarm-history-status", message);
  } finally {
    window.clearTimeout(timeout);
    if (requestId === historyRequestId) { historyRefreshRunning = false; historyAbortController = null; }
  }
}
function validSeries(points) {
  return Array.isArray(points) ? points.filter((point) => point && finite(point.ts) && finite(point.value)).sort((a,b) => a.ts-b.ts) : [];
}
function drawChart() {
  const canvas = $("temperature-chart"), rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const context = canvas.getContext("2d"), scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * scale); canvas.height = Math.round(rect.height * scale);
  context.scale(scale,scale); context.clearRect(0,0,rect.width,rect.height);
  const temperature = validSeries(historyData?.series?.temperature_c), setpoint = validSeries(historyData?.series?.temperature_setpoint_c);
  $("chart-empty").hidden = temperature.length + setpoint.length > 0;
  chartGeometry = null;
  if (!temperature.length && !setpoint.length) return;
  const allValues = [...temperature,...setpoint].map((point) => point.value);
  const state = latestPayload?.state || {};
  const band = isFresh() && finite(state.control_deadband_c) && finite(state.temperature_setpoint_c)
    ? [state.temperature_setpoint_c-state.control_deadband_c,state.temperature_setpoint_c+state.control_deadband_c] : null;
  if (band) allValues.push(...band);
  const low = Math.floor(Math.min(...allValues)-2), high = Math.ceil(Math.max(...allValues)+2);
  const left=55,right=rect.width-20,top=18,bottom=rect.height-43;
  const xFor = (ts) => left+(ts-historyData.start_ts)/Math.max(1,historyData.end_ts-historyData.start_ts)*(right-left);
  const yFor = (value) => bottom-(value-low)/Math.max(1,high-low)*(bottom-top);
  context.font='12px "Segoe UI", sans-serif'; context.fillStyle="#57636b"; context.textAlign="right";
  for(let i=0;i<=4;i++) {
    const value=low+(high-low)*i/4,y=yFor(value);
    context.strokeStyle="#dce1e5"; context.beginPath(); context.moveTo(left,y); context.lineTo(right,y); context.stroke();
    context.fillText(value.toFixed(0)+" °C",left-8,y+4);
  }
  for(let i=0;i<=4;i++) {
    const ts=historyData.start_ts+(historyData.end_ts-historyData.start_ts)*i/4;
    const options=historyData.hours>24?{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}:{hour:"2-digit",minute:"2-digit"};
    context.textAlign=i===0?"left":i===4?"right":"center";
    context.fillText(new Date(ts).toLocaleString("vi-VN",options),left+(right-left)*i/4,bottom+25);
  }
  context.save(); context.beginPath(); context.rect(left,top,right-left,bottom-top); context.clip();
  if(band) { context.fillStyle="#566e8212"; context.fillRect(left,yFor(band[1]),right-left,yFor(band[0])-yFor(band[1])); }
  const gap=Math.max(3000, (historyData.aggregation_interval_ms || 1000)*3);
  drawSeries(context,temperature,xFor,yFor,"#285fa5",2,false,gap);
  drawSeries(context,setpoint,xFor,yFor,"#49535a",1.5,true,gap);
  context.restore();
  chartGeometry={left,right,temperature,start:historyData.start_ts,end:historyData.end_ts};
}
function drawSeries(context,points,xFor,yFor,color,width,dashed,gap) {
  context.strokeStyle=color; context.lineWidth=width; context.setLineDash(dashed?[6,4]:[]);
  context.beginPath();
  let previous=null;
  for(const point of points) {
    if(!previous || point.ts-previous.ts>gap) context.moveTo(xFor(point.ts),yFor(point.value));
    else context.lineTo(xFor(point.ts),yFor(point.value));
    previous=point;
  }
  context.stroke(); context.setLineDash([]);
  for(const point of points) { context.beginPath(); context.arc(xFor(point.ts),yFor(point.value),2,0,Math.PI*2); context.fillStyle=color; context.fill(); }
}
function renderAlarmHistory() {
  const container=$("alarm-history"), events=Array.isArray(historyData?.alarm_events)?historyData.alarm_events:[];
  const filter=$("alarm-filter").value;
  const visible=events.filter((event)=>filter==="all" || (filter==="cleared"?event.active===false:event.active!==false)).slice(-100).reverse();
  container.replaceChildren();
  setText("alarm-history-count",visible.length+" / "+events.length+" sự kiện");
  if(!visible.length) { const row=container.insertRow(); const cell=row.insertCell();cell.colSpan=4;cell.className="empty-cell";cell.textContent="Không có sự kiện phù hợp trong khoảng đã tải.";return; }
  for(const event of visible) {
    const row=container.insertRow();
    const cleared=event.active===false;
    if(!cleared && event.severity==="CRITICAL") row.className="row-critical";
    const timestamp=cleared?event.cleared_at_ms:event.raised_at_ms;
    row.insertCell().textContent=dateText(timestamp);
    row.insertCell().textContent=event.severity==="CRITICAL"?"Nghiêm trọng":event.severity==="WARNING"?"Cảnh báo":"Thông tin";
    const description=row.insertCell();description.textContent=alarmHelp[event.code]?.[0] || event.code || "Cảnh báo";
    const detail=document.createElement("small");detail.textContent=operatorMessage(event.message);description.append(detail);
    const status=row.insertCell();status.textContent=cleared?"KẾT THÚC":"PHÁT SINH";status.className=cleared?"event-cleared":"event-active";
  }
}
function showCommandResult(message,success=null) {
  setText("command-result",message);
  $("command-result").classList.toggle("success",success===true);
  $("command-result").classList.toggle("error",success===false);
}
function renderCommandHistory() {
  const container=$("command-history"); container.replaceChildren();
  for(const entry of commandHistory) {
    const row=container.insertRow();
    row.insertCell().textContent=new Date(entry.time).toLocaleTimeString("vi-VN");
    const label=row.insertCell();label.textContent=commandLabels[entry.type] || entry.type;
    if(entry.id) { const small=document.createElement("small");small.textContent=entry.id;label.append(small); }
    const result=row.insertCell();result.textContent=entry.status;
    const detail=document.createElement("small");detail.textContent=entry.message || "";result.append(detail);
  }
}
async function sendCommand(type,value) {
  const emergency=type==="EMERGENCY_STOP";
  if(emergency?emergencyBusy:commandBusy || emergencyBusy) return null;
  if(!emergency && !isFresh()) { showCommandResult("Dữ liệu máy 1 chưa xác nhận; chưa gửi yêu cầu.",false);return null; }
  if(emergency) emergencyBusy=true;else commandBusy=true;
  const entry={type,time:Date.now(),status:"Đang gửi",message:""};
  commandHistory.unshift(entry);commandHistory.splice(20);renderCommandHistory();updateControls();
  showCommandResult("Đang gửi: "+commandLabels[type]+"…");
  const abort=new AbortController(),timeout=window.setTimeout(()=>abort.abort(),35000);
  try {
    const response=await fetch("/api/commands",{cache:"no-store",credentials:"same-origin",method:"POST",signal:abort.signal,headers:{"Content-Type":"application/json","X-CSRF-Token":csrfToken},body:JSON.stringify({command_type:type,...(value===undefined?{}:{value})})});
    const result=await response.json();
    if(response.status===401) { window.location.reload();throw new Error("Phiên đăng nhập đã hết hạn."); }
    if(![200,409].includes(response.status) || typeof result.accepted!=="boolean") throw new Error(result.error || result.message || "Phản hồi không hợp lệ.");
    entry.id=result.command_id;entry.status=result.accepted?"Chấp nhận":"Từ chối";entry.message=operatorMessage(result.message);
    showCommandResult(entry.status+": "+(entry.message || commandLabels[type]),result.accepted);
    return result;
  } catch(error) {
    entry.status="Chưa xác nhận";entry.message=error.name==="AbortError"?"Quá thời gian chờ phản hồi.":error.message;
    showCommandResult(entry.message+" Kiểm tra trạng thái máy 1 trước khi gửi lại.",false);
    return null;
  } finally {
    window.clearTimeout(timeout);
    if(emergency) emergencyBusy=false;else commandBusy=false;
    renderCommandHistory();updateControls();
    void refreshState();
  }
}
function readRecipe() {
  const values=fieldIds.map((id)=>$(id).value.trim());
  if(values.some((value)=>value==="")) throw new Error("Nhập đủ ba thông số mẻ.");
  const [setpoint,minutes,cooling]=values.map(Number);
  if(![setpoint,minutes,cooling].every(finite) || minutes<=0 || cooling<0) throw new Error("Kiểm tra nhiệt độ, thời gian giữ nhiệt và ngưỡng dừng quạt.");
  const state=latestPayload?.state || {};
  if(finite(state.minimum_setpoint_c) && setpoint<state.minimum_setpoint_c || finite(state.maximum_setpoint_c) && setpoint>state.maximum_setpoint_c) throw new Error("Điểm đặt nằm ngoài giới hạn máy 1.");
  if(finite(state.control_deadband_c) && cooling>=setpoint-state.control_deadband_c) throw new Error("Ngưỡng dừng quạt phải thấp hơn điểm đặt trừ deadband.");
  return {temperature_setpoint_c:setpoint,drying_duration_s:minutes*60,cooling_stop_temperature_c:cooling};
}
function csvCell(value) {
  let text=String(value ?? "");
  if(typeof value==="string" && /^[\s]*[=+@-]/.test(text)) text="'"+text;
  return '"'+text.replaceAll('"','""')+'"';
}
function exportHistory() {
  if(!historyData) return;
  const rows=new Map();
  for(const key of ["temperature_c","temperature_setpoint_c"]) for(const point of validSeries(historyData.series[key])) {
    if(!rows.has(point.ts)) rows.set(point.ts,{});
    rows.get(point.ts)[key]=point.value;
  }
  const content=[["timestamp_utc","temperature_c","temperature_setpoint_c"],...Array.from(rows).sort((a,b)=>a[0]-b[0]).map(([ts,values])=>[new Date(ts).toISOString(),values.temperature_c,values.temperature_setpoint_c])];
  const blob=new Blob(["\uFEFF"+content.map((row)=>row.map(csvCell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download="scada-nhiet-do.csv";link.click();window.setTimeout(()=>URL.revokeObjectURL(url),1000);
}
fieldIds.forEach((id)=>$(id).addEventListener("input",()=>{dirtyFields.add(id);$(id).classList.add("is-dirty");updateControls();}));
$("discard-settings").addEventListener("click",()=>{dirtyFields.clear();document.activeElement?.blur();renderSnapshot();showCommandResult("Đã hoàn tác bản nhập theo thông số máy 1.");});
$("settings-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if($("save-settings").disabled) return;
  try {
    const recipe=readRecipe();
    const result=await sendCommand("SET_RECIPE",recipe);
    if(result?.accepted) { pendingRecipe={values:recipe,startedAt:performance.now()};showCommandResult("Controller đã chấp nhận bộ thông số; đang chờ telemetry xác nhận.",true);renderSnapshot(); }
  } catch(error) { showCommandResult(error.message,false); }
});
$("start-button").addEventListener("click",()=>{if(!$("start-button").disabled) void sendCommand("START_DRYING");});
$("stop-button").addEventListener("click",()=>{if(!$("stop-button").disabled) void sendCommand("SET_MODE","OFF");});
$("emergency-button").addEventListener("click",()=>{
  if(window.confirm("Gửi yêu cầu dừng khẩn tới máy 1? Controller sẽ khóa FAULT và yêu cầu tải OFF. Cần reset tại HMI sau khi xử lý nguyên nhân.")) void sendCommand("EMERGENCY_STOP");
});
$("history-range").addEventListener("change",()=>{void refreshHistory();});
$("history-refresh").addEventListener("click",()=>{void refreshHistory();});
$("alarm-filter").addEventListener("change",renderAlarmHistory);
$("export-history").addEventListener("click",exportHistory);
$("temperature-chart").addEventListener("mousemove",(event)=>{
  const geometry=chartGeometry,tooltip=$("chart-tooltip");
  if(!geometry || !geometry.temperature.length) { tooltip.hidden=true;return; }
  const rect=event.currentTarget.getBoundingClientRect(),x=event.clientX-rect.left;
  if(x<geometry.left || x>geometry.right) { tooltip.hidden=true;return; }
  const ts=geometry.start+(x-geometry.left)/(geometry.right-geometry.left)*(geometry.end-geometry.start);
  const point=geometry.temperature.reduce((best,item)=>Math.abs(item.ts-ts)<Math.abs(best.ts-ts)?item:best);
  tooltip.textContent=dateText(point.ts)+"\nNhiệt độ: "+displayValue(point.value," °C");
  tooltip.style.left=Math.max(0,Math.min(rect.width-230,x+10))+"px";
  tooltip.style.top="12px";tooltip.hidden=false;
});
$("temperature-chart").addEventListener("mouseleave",()=>{$("chart-tooltip").hidden=true;});
$("logout-button").addEventListener("click",async()=>{try{await fetchJson("/logout",{method:"POST",headers:{"X-CSRF-Token":csrfToken}});}finally{window.location.assign("/");}});
window.addEventListener("hashchange",switchView);
window.addEventListener("resize",drawChart);
switchView();
renderSnapshot();
void refreshState();
window.setInterval(()=>{renderSnapshot();void refreshState();},1000);
