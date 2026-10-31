"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let latest = null;
let commandBusy = false;
let polling = false;
let interfaceAvailable = false;
const dirtySettings = new Set();

function formatDuration(seconds, roundUp = true) {
  const total = Math.max(0, roundUp ? Math.ceil(seconds) : Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function setText(selector, value) {
  $(selector).textContent = value;
}

function setConnection(name, connected) {
  setText(`#${name}-status`, connected === true ? "Đã kết nối" : connected === false ? "Mất kết nối" : "Chưa xác nhận");
  $(`#${name}-dot`).classList.toggle("online", connected === true);
  $(`#${name}-dot`).classList.toggle("offline", connected === false);
}

function showMessage(message, kind = "info") {
  const node = $("#command-message");
  node.textContent = message;
  node.dataset.kind = kind;
}

function render(payload) {
  latest = payload;
  const state = payload.state;
  const settings = payload.settings;
  const temperature = Number.isFinite(state.temperature_c) ? state.temperature_c : null;

  setText("#temperature-value", temperature === null ? "--.-" : temperature.toFixed(1));
  $("#temperature-value").classList.toggle("is-unconfirmed", temperature === null || state.sensor_connected !== true);
  setText("#setpoint-readout", settings.temperature_setpoint_c.toFixed(1));
  setText("#limit-readout", settings.high_temperature_limit_c.toFixed(1));
  const batchRunning = state.mode === "AUTO" && state.auto_phase !== "COMPLETE";
  const recipeLocked = commandBusy || batchRunning;
  const lower = settings.temperature_setpoint_c - settings.control_deadband_c;
  setText("#drying-remaining", formatDuration(state.drying_remaining_s));
  setText("#drying-elapsed", formatDuration(state.drying_elapsed_s, false));
  setText("#drying-duration", formatDuration(settings.drying_duration_s));
  let timerStatus = "Chưa bắt đầu mẻ sấy.";
  if (state.mode === "FAULT") timerStatus = "Mẻ đã hủy do lỗi; cần reset và bắt đầu lại.";
  else if (state.mode === "AUTO") {
    timerStatus = {
      IDLE: "Đang chuẩn bị mẻ.",
      HEATING: "Chưa đếm thời gian: đang chờ đạt nhiệt độ đặt.",
      HOLDING: state.hold_timer_running ? "Đang đếm thời gian giữ nhiệt." : "Tạm dừng bộ đếm: nhiệt độ ngoài vùng giữ nhiệt.",
      COOLING: "Đã đủ thời gian sấy; đang làm nguội.",
      COMPLETE: "Đã nguội đến ngưỡng. Hai tải đã tắt; có thể bắt đầu mẻ mới.",
    }[state.auto_phase];
  }
  setText("#timer-status", timerStatus);
  setText("#vent-hint", state.auto_phase === "COOLING" && state.mode === "AUTO"
    ? `Mở cửa thoát ẩm thủ công. Quạt dừng khi nhiệt độ ≤ ${settings.cooling_stop_temperature_c.toFixed(1)} °C.`
    : "Cửa thoát ẩm mở thủ công khi làm nguội.");
  $("#start-drying").disabled = commandBusy || batchRunning || state.mode === "FAULT" || dirtySettings.size > 0;
  setText("#start-drying", state.auto_phase === "COMPLETE" ? "Bắt đầu mẻ mới" : "Bắt đầu sấy");
  setText("#recipe-hint", batchRunning
    ? "Thông số đã khóa cho mẻ này. OFF hủy mẻ và xóa bộ đếm."
    : dirtySettings.size > 0 ? "Có thông số chưa lưu. Lưu từng thông số trước khi bắt đầu."
    : "Thông số HMI dùng trong phiên chạy này; khởi động lại lấy mặc định YAML.");

  $$(".mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
    button.disabled = commandBusy || state.mode === "FAULT" ||
      (button.dataset.mode === "AUTO" && (batchRunning || dirtySettings.size > 0));
  });

  const manual = state.mode === "MANUAL";
  const feedback = payload.relay_feedback || {};
  const loadsKnown = state.modbus_connected === true && state.relay_connected === true &&
    typeof feedback.heater === "boolean" && typeof feedback.process_fan === "boolean";
  renderOutput("heater", feedback.heater, manual, loadsKnown);
  renderOutput("fan", feedback.process_fan, manual, loadsKnown);
  renderEquipment(feedback.heater, feedback.process_fan, loadsKnown);
  $("#manual-hint").textContent = manual ? "ĐƯỢC PHÉP" : "CHỈ MANUAL";
  setText("#manual-help", manual ? "Điều khiển đầu ra trong MANUAL." : "Chọn MANUAL để điều khiển đầu ra.");

  const min = Math.max(settings.minimum_setpoint_c,
    Math.floor((settings.cooling_stop_temperature_c + settings.control_deadband_c) * 2) / 2 + 0.5);
  const max = settings.maximum_setpoint_c;
  for (const node of [$("#setpoint-input"), $("#setpoint-range")]) {
    node.min = min;
    node.max = max;
    if (!dirtySettings.has("SET_TEMPERATURE") && document.activeElement !== node) node.value = settings.temperature_setpoint_c;
    node.disabled = recipeLocked;
  }
  $("#apply-setpoint").disabled = recipeLocked;
  const durationInput = $("#drying-minutes-input");
  if (!dirtySettings.has("SET_DRYING_DURATION") && document.activeElement !== durationInput) durationInput.value = settings.drying_duration_s / 60;
  durationInput.disabled = recipeLocked;
  const coolingInput = $("#cooling-input");
  coolingInput.max = lower - 0.1;
  if (!dirtySettings.has("SET_COOLING_TEMPERATURE") && document.activeElement !== coolingInput) coolingInput.value = settings.cooling_stop_temperature_c;
  coolingInput.disabled = recipeLocked;
  $("#apply-duration").disabled = recipeLocked;
  $("#apply-cooling").disabled = recipeLocked;

  const alarmActive = state.alarm_code !== "NONE";
  setText("#alarm-code", state.alarm_code);
  setText("#alarm-status", alarmActive ? "FAULT" : "BÌNH THƯỜNG");
  $("#alarm-card").classList.toggle("has-alarm", alarmActive);
  $("#alarm-card").classList.toggle("is-normal", !alarmActive);
  $("#alarm-card").classList.remove("is-unconfirmed");
  setText("#alarm-message", payload.active_alarm?.message || "Hệ thống chưa ghi nhận lỗi.");
  $("#reset-alarm").disabled = commandBusy || state.mode !== "FAULT";

  setConnection("sensor", state.sensor_connected);
  setConnection("relay", state.relay_connected);
  setConnection("mqtt", state.mqtt_connected);
  $("#simulation-badge").classList.toggle("hidden", !payload.simulation);
  setText("#last-update", new Date(payload.server_time_ms).toLocaleTimeString("vi-VN"));

  const poll = $("#poll-status");
  poll.className = "connection-pill online";
  poll.innerHTML = "<span></span>HMI trực tuyến";
  $("#emergency-stop").disabled = commandBusy;
}

function renderOutput(name, enabled, permitted, known) {
  setText(`#${name}-state`, known ? enabled ? "BẬT" : "TẮT" : "CHƯA XÁC NHẬN");
  $(`#${name}-panel`).classList.toggle("active", known && enabled);
  $(`#${name}-panel`).classList.toggle("is-unknown", !known);
  $$(`[data-output="${name}"]`).forEach((button) => {
    const selected = known && enabled === (button.dataset.value === "true");
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.disabled = commandBusy || !permitted;
  });
}

function renderEquipment(heater, fan, known) {
  const drawing = $("#system-schematic");
  drawing.classList.toggle("heater-on", known && heater === true);
  drawing.classList.toggle("fan-on", known && fan === true);
  drawing.classList.toggle("unknown", !known);
  drawing.dataset.heater = known ? heater ? "on" : "off" : "unknown";
  drawing.dataset.fan = known ? fan ? "on" : "off" : "unknown";
  for (const [name, enabled] of [["heater", heater], ["fan", fan]]) {
    const status = known ? enabled ? "BẬT" : "TẮT" : "?";
    setText(`#${name}-diagram-state`, status);
    setText(`#relay-${name}-state`, status);
  }
}

function markUnavailable() {
  interfaceAvailable = false;
  const poll = $("#poll-status");
  poll.className = "connection-pill offline";
  poll.innerHTML = "<span></span>Mất kết nối HMI";
  renderEquipment(null, null, false);
  renderOutput("heater", null, false, false);
  renderOutput("fan", null, false, false);
  setText("#temperature-value", "--.-");
  $("#temperature-value").classList.add("is-unconfirmed");
  setText("#alarm-status", "CHƯA XÁC NHẬN");
  setText("#alarm-code", "—");
  setText("#alarm-message", "Mất kết nối HMI. Trạng thái controller chưa xác nhận.");
  $("#alarm-card").classList.add("has-alarm");
  $("#alarm-card").classList.remove("is-normal");
  $("#alarm-card").classList.add("is-unconfirmed");
  setText("#timer-status", "Chưa nhận được dữ liệu mới; trạng thái tải chưa xác nhận.");
  for (const name of ["sensor", "relay", "mqtt"]) setConnection(name, null);
  for (const node of $$(".mode-button, .recipe-panel button, .recipe-panel input, #reset-alarm")) node.disabled = true;
  // Vẫn cho phép yêu cầu dừng khẩn khi GET thất bại nhưng POST có thể còn hoạt động.
  $("#emergency-stop").disabled = commandBusy;
}

async function refreshState() {
  if (polling) return;
  polling = true;
  const abort = new AbortController();
  const timeout = window.setTimeout(() => abort.abort(), 5000);
  try {
    const response = await fetch("/api/state", {cache: "no-store", signal: abort.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    interfaceAvailable = true;
    render(payload);
  } catch (error) {
    markUnavailable();
    showMessage(`Không lấy được trạng thái: ${error.message}`, "error");
  } finally {
    window.clearTimeout(timeout);
    polling = false;
  }
}

async function sendCommand(commandType, value = null) {
  if (commandBusy) return;
  commandBusy = true;
  if (latest && interfaceAvailable) render(latest);
  showMessage("Đang gửi lệnh…");
  try {
    const response = await fetch("/api/commands", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({command_type: commandType, value}),
    });
    const result = await response.json();
    if (result.accepted) dirtySettings.delete(commandType);
    showMessage(result.message || "Đã xử lý lệnh.", result.accepted ? "success" : "error");
  } catch (error) {
    showMessage(`Không gửi được lệnh: ${error.message}`, "error");
  } finally {
    commandBusy = false;
    await refreshState();
  }
}

$$(".mode-button").forEach((button) => {
  button.addEventListener("click", () => sendCommand("SET_MODE", button.dataset.mode));
});

$("#setpoint-range").addEventListener("input", (event) => {
  dirtySettings.add("SET_TEMPERATURE");
  $("#setpoint-input").value = event.target.value;
  if (latest) render(latest);
});

$("#setpoint-input").addEventListener("input", (event) => {
  dirtySettings.add("SET_TEMPERATURE");
  $("#setpoint-range").value = event.target.value;
  if (latest) render(latest);
});

$("#apply-setpoint").addEventListener("click", () => {
  if (!$("#setpoint-input").value.trim()) return showMessage("Nhập nhiệt độ đặt.", "error");
  const value = Number($("#setpoint-input").value);
  if (!Number.isFinite(value)) return showMessage("Setpoint không hợp lệ.", "error");
  sendCommand("SET_TEMPERATURE", value);
});

$("#drying-minutes-input").addEventListener("input", () => {
  dirtySettings.add("SET_DRYING_DURATION");
  if (latest) render(latest);
});
$("#cooling-input").addEventListener("input", () => {
  dirtySettings.add("SET_COOLING_TEMPERATURE");
  if (latest) render(latest);
});
$("#apply-duration").addEventListener("click", () => {
  const input = $("#drying-minutes-input");
  const seconds = Number(input.value) * 60;
  if (!input.value.trim() || !Number.isFinite(seconds) || seconds <= 0)
    return showMessage("Thời gian sấy phải lớn hơn 0 phút.", "error");
  sendCommand("SET_DRYING_DURATION", seconds);
});
$("#apply-cooling").addEventListener("click", () => {
  const input = $("#cooling-input");
  const value = Number(input.value);
  if (!input.value.trim() || !Number.isFinite(value)) return showMessage("Ngưỡng nguội không hợp lệ.", "error");
  sendCommand("SET_COOLING_TEMPERATURE", value);
});
$("#start-drying").addEventListener("click", () => sendCommand("START_DRYING"));

$$(".output-command").forEach((button) => {
  button.addEventListener("click", () => {
    sendCommand(button.dataset.output === "heater" ? "SET_HEATER" : "SET_PROCESS_FAN",
      button.dataset.value === "true");
  });
});

$("#reset-alarm").addEventListener("click", () => sendCommand("RESET_ALARM"));

$("#emergency-stop").addEventListener("click", () => {
  if (window.confirm("Dừng ngay toàn bộ đầu ra và khóa hệ thống ở FAULT?")) {
    sendCommand("EMERGENCY_STOP");
  }
});

refreshState();
window.setInterval(refreshState, 1000);
